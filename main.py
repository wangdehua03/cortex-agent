"""
Agent Platform 统一入口
用法:
    python main.py
"""

import sys
import os

os.environ["TOKENIZERS_PARALLELISM"] = "false"

sys.path.append(os.path.join(os.path.dirname(__file__), '.'))

from src.infrastructure.clients.llm_clients import LLMClient
from src.infrastructure.message_bus import bus, UserInputQueue
from src.infrastructure.session import SessionManager
from src.infrastructure.conversation import Conversation
from src.infrastructure.context_store import ContextStore
from config.config import BASE_URL, API_KEY, CONTEXT_WINDOW, KEEP_RECENT_ROUNDS
from config.tools import LEAD_MILESTONE_EXTRACTORS
from config.tools import (
    LEAD_AGENT_TOOLS, TOOL_HANDLERS,
)
from config.tools.common import register_send_message_handler


def _resolve_permission(msg: dict, uiq=None, log_fn=None):
    """
    Lead 处理来自 SubAgent 的 permission_request，发送回复并返回摘要文本。

    Args:
        msg: permission_request 消息 dict
        uiq: UserInputQueue 实例，用于从队列中安全地读取用户 y/n 输入。
             如果未提供，退化到直接 input()（不推荐）。
        log_fn: optional logging callback (e.g., agent._log)
    """
    extra = msg.get("extra", {})
    subagent_id = extra.get("subagent_id", msg.get("sender", ""))
    request_id = extra.get("request_id", "")
    reason = extra.get("reason", "")
    command = extra.get("command", "")

    is_dangerous = reason.lower().startswith("dangerous") or reason.lower().startswith("sensitive")

    if is_dangerous:
        print(f"\n\033[33m[SubAgent {subagent_id}] 请求执行危险命令:\033[0m")
        print(f"   原因: {reason}")
        print(f"   命令: {command}")
        if log_fn:
            log_fn(f"[Permission] SubAgent {subagent_id} request: {reason} (command: {command})")
        if uiq is not None:
            ans = uiq.prompt_and_wait("\033[36m user >> \033[0m")
            granted = ans is not None and ans.strip().lower() in ("y", "yes")
        else:
            try:
                ans = input("\033[36m user >> \033[0m")
                granted = ans.strip().lower() in ("y", "yes")
            except (EOFError, KeyboardInterrupt):
                granted = False
    else:
        granted = True
        print(f"\033[33m[Lead] Auto-approved for {subagent_id}: {reason}\033[0m")

    if log_fn:
        status = "approved" if granted else "denied"
        log_fn(f"[Permission] {status} for {subagent_id}: {reason} (command: {command})")

    bus.send(
        "lead",
        subagent_id,
        "",
        msg_type="permission_response",
        extra={"request_id": request_id, "subagent_id": subagent_id, "granted": granted},
    )

    status = "approved" if granted else "denied"
    return f"[permission_{status}] from {subagent_id}: {reason} (command: {command})"


def _format_inbox_messages(inbox: list, *, print_preview: bool = False, wrap_tag: str = "inbox", uiq=None, log_fn=None) -> list:
    """
    统一处理 inbox 消息：resolve permission，格式化其他消息为 user message。

    Args:
        inbox: bus.read_inbox 返回的消息列表
        print_preview: 是否打印预览（/inbox 命令需要）
        wrap_tag: 包裹标签名
        uiq: UserInputQueue 实例，传递给 _resolve_permission
        log_fn: optional logging callback

    Returns:
        格式化后的 user message 列表（仅非 permission 消息）
    """
    result = []
    for msg in inbox:
        msg_type = msg.get("type", "")
        if msg_type == "permission_request":
            _resolve_permission(msg, uiq=uiq, log_fn=log_fn)
        else:
            if print_preview:
                sender = msg.get("sender", "")
                content = msg.get("content", "")[:200]
                print(f"\033[33m[{msg_type}] from {sender}:{content}\033[0m")
            content_str = f"[{msg_type}] from {msg.get('sender', '')}: {msg.get('content', '')}"
            result.append({
                "role": "user",
                "content": f"<{wrap_tag}>{content_str}</{wrap_tag}>",
            })
    return result


def _idle_drain_inbox(max_msgs: int = 3, *, uiq=None, log_fn=None):
    """Drain lead's inbox for subagent_done and similar messages when idle.

    At most *max_msgs* non-permission messages are consumed per call.
    Remaining messages are re-enqueued for the next idle-drain cycle.

    permission_request messages are resolved directly with user prompt,
    so subagent gets a timely response even when lead is not looping.

    Returns:
        list of a single formatted user message, or None if inbox has no processable messages.
    """
    inbox = bus.read_inbox("lead")
    if not inbox:
        return None

    parts = []
    deferred = []
    has_permission = False
    for msg in inbox:
        msg_type = msg.get("type", "")
        if msg_type == "permission_request":
            _resolve_permission(msg, uiq=uiq, log_fn=log_fn)
            has_permission = True
            continue

        if len(parts) >= max_msgs:
            deferred.append(msg)
            continue

        sender = msg.get("sender", "")
        content = msg.get("content", "")
        content_preview = content[:200] if len(content) > 200 else content
        print(f"\033[33m[Idle] [{msg_type}] from {sender}: {content_preview}\033[0m")
        if log_fn:
            log_fn(f"[Idle] [{msg_type}] from {sender}: {content_preview}")
        parts.append(f"[{len(parts)+1}] [{msg_type}] from {sender}: {content}")

    for msg in deferred:
        bus.send(msg.get("sender", "system"), "lead", msg.get("content", ""),
                  msg_type=msg.get("type", "message"),
                  extra=msg.get("extra", {}))

    if parts or has_permission:
        if parts:
            return [{"role": "user", "content": "<inbox_drain>\n" + "\n".join(parts) + "\n</inbox_drain>"}]
        return None
    return None


def run_single_agent():
    """单 Agent 交互模式（含 s09 异步 subagent + s07 task system + s10 queued input + s11 steer）

    UserInputQueue 将用户输入队列化，与 inbox 消息统一通过事件轮询消费。
    agent 运行时（_agent_running=True），用户输入进入 steer_queue，在
    _loop_core 的每轮边界被检查并注入为 user 消息，实现 steer/_interrupt。
    """
    from src.agents.agent import Agent
    from src.infrastructure.task_store import tasks as task_store

    full_handlers = dict(TOOL_HANDLERS)
    register_send_message_handler(full_handlers, "lead")

    print("\033[36m=== Single Agent Mode (s09: async subagent + s10: queued input + s11: steer) ===\033[0m")
    llm = LLMClient(base_url=BASE_URL, api_key=API_KEY)

    def _create_lead_conversation(session_id: str) -> Conversation:
        return Conversation(
            session_id=session_id,
            context_store=ContextStore(
                keep_recent_rounds=KEEP_RECENT_ROUNDS,
                milestone_extractors=LEAD_MILESTONE_EXTRACTORS,
            )
        )

    session_manager = SessionManager(conversation_factory=_create_lead_conversation)
    conversation = session_manager.create("default_user")
    agent = Agent(llm=llm, context_window=CONTEXT_WINDOW)

    agent.tools = LEAD_AGENT_TOOLS
    agent.tool_handler = full_handlers
    bus.register_agent("lead")

    # 队列化的用户输入
    uiq = UserInputQueue()
    uiq.start()

    # 将 UIQ 注册给 run_shell_command 的 _ask_permission 使用
    from src.utils.function import _set_user_input_queue
    _set_user_input_queue(uiq)

    def _prompt():
        print("\033[36m user >> \033[0m", end="", flush=True)

    def _agent_done():
        _prompt()

    def _run_loop(*, steer_injected=False):
        """执行 agent.run，带 agent_running 标志切换。"""
        uiq.set_agent_running(True)
        try:
            agent.run(conversation, steer_queue=uiq)
        finally:
            uiq.set_agent_running(False)
            _agent_done()

    _prompt()

    try:
        while True:
            # ---- 检查是否有 agent 运行期间积累的 steer，需要回放 ----
            leftover_steers = uiq.get_steers()
            if leftover_steers:
                steer_text = "\n".join(
                    f"<user_steer>{s}</user_steer>" for s in leftover_steers
                )
                conversation.context_store.append_user_message(steer_text)
                agent._log(f"[Steer] Injecting user steer into next round")
                print("\033[33m[Steer] Injecting user steer into next round:\033[0m")
                for s in leftover_steers:
                    print(f"   > {s}")
                _run_loop(steer_injected=True)
                continue

            # ---- 优先处理 inbox 消息（subagent_done, permission_request 等） ----
            idle_msgs = _idle_drain_inbox(uiq=uiq, log_fn=agent._log)
            if idle_msgs:
                conversation.context_store.append_user_message(idle_msgs[0]["content"])
                agent._log(f"[Inbox] {idle_msgs[0].get('content', '')[:300]}")
                _run_loop()
                continue

            # ---- 检查后台 bash 任务完成情况 + inbox ----
            # (background bash done 信息通过 bus 发送到 lead 的 inbox，已被 inbox drain 自动处理)

            # ---- 轮询用户输入队列（非阻塞，超时 0.5s） ----
            query = uiq.get(timeout=0.5)
            if uiq.eof:
                break
            if not query:
                continue  # 无用户输入，继续循环（可能 inbox 有新消息到达）

            if query.strip().lower() in ("q", "exit", ""):
                agent._log("User exited")
                break

            if query.strip() == "/manual_compact":
                agent._log("[Manual Compact] Triggering context compression")
                print("\033[33m[Manual Compact] Triggering context compression...\033[0m")
                import re as _re
                messages = agent.auto_compact(conversation.context_store.snapshot_messages())
                summary_text = None
                for msg in messages:
                    if msg.get("role") == "user" and isinstance(msg.get("content", ""), str):
                        content = msg["content"]
                        if "<context_summary>" in content:
                            m = _re.search(r"<context_summary>.*?\n\n(.*?)</context_summary>", content, _re.DOTALL)
                            summary_text = m.group(1).strip() if m else content
                            break
                if summary_text:
                    conversation.context_store.append_summary_checkpoint(summary_text)
                    agent.token_used = 0
                    print("\033[32m[Manual Compact] Context compressed successfully!\033[0m")
                else:
                    print("\033[31m[Manual Compact] No summary generated, skipping.\033[0m")
                _prompt()
                continue

            if query.strip() == "/inbox":
                agent._log("[Command] /inbox")
                inbox = bus.read_inbox("lead")
                if inbox:
                    formatted = _format_inbox_messages(inbox, print_preview=True, uiq=uiq, log_fn=agent._log)
                    for msg in formatted:
                        conversation.context_store.append_user_message(msg["content"])
                    print("\033[32m[Inbox] Messages added to history for next round.\033[0m")
                else:
                    print("Inbox is empty.")
                _prompt()
                continue

            if query.strip() == "/task_list":
                agent._log("[Command] /task_list")
                print("\033[36m=== Task List ===\033[0m")
                print(task_store.list_all_formatted())
                _prompt()
                continue

            conversation.context_store.append_user_message(query)
            agent._log(f"user >> {query}")
            _run_loop()
    finally:
        uiq.stop()
        agent.close()


def main():
    run_single_agent()


if __name__ == "__main__":
    main()
