"""
AsyncSubAgent - 异步子 agent

与现有 SubAgent（同步委托，阻塞等待）不同，AsyncSubAgent：
  - 在独立 thread 中运行
  - 不阻塞主 agent
  - 完成后通过 MessageBus 将结果发送给 lead
  - 进入 awaiting_review 状态，等待 lead 的 shutdown/revision 指令
  - bash 危险命令通过 MessageBus 向 lead 请求权限确认
"""

import json
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from src.agents.agent import BaseAgent
from src.infrastructure.clients.llm_clients import LLMClient
from src.infrastructure.message_bus import bus, build_inbox_drain_fn
from config.prompts.subagent import SUBAGENT_BASE
from config.tools import SUB_AGENT_TOOLS, build_tool_handlers
from config.tools.common import register_send_message_handler
from config.tools.subagent import TODO_TOOL
from config.config import CONTEXT_WINDOW, SUBAGENT_LOG_DIR, SUBAGENT_REVIEW_TIMEOUT_ROUNDS, SUBAGENT_REVIEW_SLEEP_INTERVAL
from src.utils.managers import TodoManager
from src.utils.stdio_redirect import SubAgentLogger


class AsyncSubAgent(BaseAgent):
    """异步子 agent，在独立 thread 中运行"""

    def __init__(self, subagent_id: str, llm: LLMClient, prompt: str,
                 sys_prompt: str | None = None,
                 context_window: int = CONTEXT_WINDOW):
        self.subagent_id = subagent_id
        self._agent_name = subagent_id
        self._prompt = prompt
        self._lead_name = "lead"
        self._status = "running"  # running | awaiting_review | shutdown
        self._log_round = 0

        if sys_prompt is None:
            sys_prompt = SUBAGENT_BASE

        permission_callback = self._make_permission_callback()
        handlers = build_tool_handlers(permission_callback=permission_callback)

        super().__init__(
            llm=llm,
            sys_prompt=sys_prompt,
            tools=SUB_AGENT_TOOLS,
            context_window=context_window,
            tool_handler=handlers,
        )
        register_send_message_handler(self.tool_handler, subagent_id)
        self.todo_manager = TodoManager()
        self._todo_active = False

        # Log-friendly streaming: buffer chunks, flush after stream completes
        self._use_stream_buffer = True

        # Independent logger -- writes directly to own file, no stdout hijacking
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        log_file = SUBAGENT_LOG_DIR / f"{subagent_id}_{ts}.log"
        self.log_path = str(log_file)
        self._logger = SubAgentLogger(subagent_id, log_file, thread_safe=True)

        bus.register_agent(subagent_id)

    # ---- Log helpers (use self._logger, not global functions) ----

    def _log(self, *args):
        """Write to subagent log file"""
        self._logger.log(*args)

    @staticmethod
    def _tprint(*args, **kwargs):
        """Print to original terminal stdout"""
        SubAgentLogger.print_to_terminal(*args, **kwargs)

    # ---- Lifecycle ----

    def _make_permission_callback(self):
        def _request_permission(reason: str, command: str) -> bool:
            req_id = str(uuid.uuid4())[:8]

            bus.send(
                self.subagent_id,
                self._lead_name,
                "",
                msg_type="permission_request",
                extra={
                    "request_id": req_id,
                    "subagent_id": self.subagent_id,
                    "reason": reason,
                    "command": command,
                },
            )
            self._log(f"Permission request sent: {reason}\n command:{command}")
            self._tprint(f"\033[33m[{self.subagent_id}] Permission request: {reason}\n command:{command}\033[0m")

            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                inbox = bus.peek_inbox(self.subagent_id)
                for msg in inbox:
                    if msg.get("type") == "permission_response":
                        resp_extra = msg.get("extra", {})
                        if resp_extra.get("request_id") == req_id:
                            granted = resp_extra.get("granted", False)
                            status = "Permission granted" if granted else "Permission denied"
                            self._log(status)
                            return granted
                time.sleep(0.5)

            self._log("Permission request timeout (60s), denied")
            return False

        return _request_permission

    def run(self):
        """在 thread 中执行的入口"""
        self._tprint(f"\033[33m[{self.subagent_id}] Started | log: {self.log_path}\033[0m \n")
        self._log("SubAgent started")

        try:
            self._execute()
        except KeyboardInterrupt:
            pass
        except Exception as e:
            self._log(f"Unexpected error: {e}")
            import traceback
            # Write traceback to log file
            tb = traceback.format_exc()
            self._log(tb)
            self._tprint(f"\033[31m[{self.subagent_id}] Unexpected error: {e}\033[0m")
        finally:
            self._status = "shutdown"
            self._log("SubAgent shut down")
            self._logger.close()
            bus.unregister_agent(self.subagent_id)

        self._tprint(f"\033[33m[{self.subagent_id}] Finished | log: {self.log_path}\033[0m")

    def _execute(self):
        self._store.append_user_message(f"Task: {self._prompt}")

        def on_shutdown(msg):
            self._log("Shutdown request received, aborting")
            self._tprint(f"\033[31m[{self.subagent_id}] Shutdown request received, aborting\033[0m")
            self._status = "shutdown"
            raise KeyboardInterrupt("shutdown_request received")

        inbox_fn = build_inbox_drain_fn(self.subagent_id, on_shutdown=on_shutdown)

        try:
            while True:
                self._loop_core(None, on_round_start=inbox_fn, on_loop_exit=self._on_loop_exit)

                # loop_core exited — check why
                self._background_bash_tasks = {
                    tid: ev for tid, ev in self._background_bash_tasks.items() if not ev.is_set()
                }
                if not self._background_bash_tasks:
                    # No running bash tasks → loop_core finished normally
                    break

                # Background bash running — poll inbox every 60s until all done
                self._log("[Bash BG] Waiting for background tasks (60s poll)")
                print(f"\033[33m[{self.subagent_id}] [Bash BG] Waiting for background tasks\033[0m")
                while self._background_bash_tasks:
                    inbox = bus.read_inbox(self.subagent_id)
                    for msg in inbox:
                        if msg.get("type") == "shutdown_request":
                            on_shutdown(msg)
                            return
                        content = msg.get("content", "")
                        self._store.append_user_message(f"<inbox>{content}</inbox>")
                    # Remove completed tasks
                    self._background_bash_tasks = {
                        tid: ev for tid, ev in self._background_bash_tasks.items() if not ev.is_set()
                    }
                    if self._background_bash_tasks:
                        time.sleep(60)

                # All done — re-enter _loop_core for next round
        except KeyboardInterrupt:
            return
        except Exception as e:
            self._log(f"Execution failed: {e}")
            import traceback
            tb = traceback.format_exc()
            self._log(tb)
            self._tprint(f"\033[31m[{self.subagent_id}] Execution failed: {e}\033[0m")
            self._send_result(f"Error: {e}")

    # ---- Loop hooks ----

    def _print_round_prefix(self) -> None:
        self._log_round += 1
        self._log("=" * 60)
        self._log(f"Round {self._log_round} - assistant >>")

    def _flush_stream_buffer(self) -> str:
        text = self._stream_buffer
        self._stream_buffer = ""
        text = re.sub(r'\033\[[0-9;]*m', '', text)
        if text.strip():
            for line in text.split('\n'):
                self._log(f"  {line}")
        return text

    def _print_content_suffix(self, has_content: bool) -> None:
        if has_content:
            self._log("")

    # ---- Tool call handling ----

    def _process_single_tool(self, tool_name: str, args: dict) -> str:
        if tool_name == 'todo':
            try:
                return self.todo_manager.update(**args)
            except Exception as e:
                return f"[Todo Error] {e}"
        return super()._process_single_tool(tool_name, args)

    def _process_tool_calls(self, assistant_message) -> list:
        tool_results = []
        used_todo = False

        if not assistant_message.tool_calls:
            return tool_results

        for tool_call in assistant_message.tool_calls:
            tool_name = tool_call.function.name
            try:
                args = json.loads(tool_call.function.arguments)
            except json.JSONDecodeError:
                args = {}

            self._log(f"  [Tool] {tool_name}({json.dumps(args, ensure_ascii=False)[:200]})")

            try:
                output = self._process_single_tool(tool_name, args)
            except subprocess.TimeoutExpired:
                if tool_name != "bash":
                    raise
                output = self._promote_bash_to_background(args.get("command", ""))

            if tool_name == 'todo':
                used_todo = True
                all_completed = all(item["status"] == "completed" for item in self.todo_manager.items)
                self._update_todo_state(used_todo, all_completed)

            out_str = str(output)[:500]
            self._log(f"  [Result] {out_str}")
            tool_results.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": output if output is not None else ""
            })

        if self._check_todo_nag(used_todo):
            tool_results.append({
                "role": "user",
                "content": "<reminder> You are in active tracking mode. Update your todos immediately. </reminder>"
            })

        return tool_results

    def _should_break_loop(self, assistant_message) -> bool:
        self._background_bash_tasks = {
            tid: ev for tid, ev in self._background_bash_tasks.items() if not ev.is_set()
        }
        if self._background_bash_tasks:
            self._log("[Bash BG] Breaking loop — waiting for background tasks in outer loop")
            print(f"\033[33m[{self.subagent_id}] [Bash BG] Breaking loop — waiting for background tasks\033[0m")
            return True
        return False

    # ---- Result & review ----

    def _on_loop_exit(self, messages):
        self._send_result(self._extract_final_text(messages))
        self._await_review()

    def _send_result(self, result: str):
        bus.send(
            self.subagent_id,
            self._lead_name,
            result,
            msg_type="subagent_done",
            extra={"subagent_id": self.subagent_id, "request_id": self.subagent_id},
        )
        self._log("Result sent to lead")
        self._tprint(f"\033[32m[{self.subagent_id}] Result sent to lead\033[0m")

    def _await_review(self):
        self._status = "awaiting_review"
        self._log("Awaiting lead review...")
        self._tprint(f"\033[33m[{self.subagent_id}] Awaiting lead review...\033[0m")

        for _ in range(SUBAGENT_REVIEW_TIMEOUT_ROUNDS):
            inbox = bus.read_inbox(self.subagent_id)
            if inbox:
                for msg in inbox:
                    msg_type = msg.get("type", "")
                    if msg_type == "shutdown_request":
                        self._log("Shutdown confirmed, exiting")
                        self._tprint(f"\033[32m[{self.subagent_id}] Shutdown confirmed, exiting\033[0m")
                        self._status = "shutdown"
                        return
                    elif msg_type == "revision_request":
                        feedback = msg.get("content", "Please revise your work.")
                        self._log("Received revision request")
                        self._tprint(f"\033[33m[{self.subagent_id}] Received revision request, continuing work\033[0m")
                        self._status = "running"
                        self._store.append_user_message(f"<revision_feedback>\n{feedback}\n</revision_feedback>")
                        self._continue_work()
                        return
                    else:
                        content = msg.get("content", "")
                        self._log("Received message from lead")
                        self._tprint(f"\033[33m[{self.subagent_id}] Received message from lead, continuing work\033[0m")
                        self._status = "running"
                        self._store.append_user_message(f"<lead_message>\n{content}\n</lead_message>")
                        self._continue_work()
                        return

            time.sleep(SUBAGENT_REVIEW_SLEEP_INTERVAL)

        self._log("Review timeout, auto-shutdown")
        self._tprint(f"\033[33m[{self.subagent_id}] Review timeout, auto-shutdown\033[0m")
        self._status = "shutdown"

    def _continue_work(self):
        try:
            self._loop_core(None, on_loop_exit=lambda msgs: (
                self._send_result(self._extract_final_text(msgs)),
                self._await_review(),
            ))
        except Exception as e:
            self._send_result(f"Error during revision: {e}")

    # ---- Helpers ----

    def _extract_final_text(self, messages: list) -> str:
        for m in reversed(messages):
            if m.get("role") == "assistant" and m.get("content"):
                text = m["content"].strip()
                return text if text else "(no output)"
        return "(no output)"

    def _get_milestone_extractors(self) -> dict:
        from config.tools import SUBAGENT_MILESTONE_EXTRACTORS
        return dict(SUBAGENT_MILESTONE_EXTRACTORS)

    def _get_todo_status_for_compact(self) -> str:
        if self.todo_manager.items:
            return f"\n\n## Current Todo Status:\n{self.todo_manager.render()}"
        return ""

    def _update_todo_state(self, used_todo: bool, all_completed: bool):
        if used_todo:
            if not self._todo_active:
                self._todo_active = True
            elif all_completed:
                self._todo_active = False
                self.todo_manager.clear()

    def _check_todo_nag(self, used_todo: bool) -> bool:
        return self._todo_active and not used_todo


def spawn_subagent(lead_name: str, llm: LLMClient, prompt: str,
                   sys_prompt: str | None = None,
                   context_window: int = CONTEXT_WINDOW) -> str:
    """
    工厂函数：创建并启动一个异步 SubAgent。

    Args:
        lead_name: lead agent 名称
        llm: LLM 客户端
        prompt: 任务描述
        sys_prompt: 可选的系统 prompt，不传则使用 SUBAGENT_BASE
        context_window: 上下文窗口大小

    Returns: subagent_id
    """
    subagent_id = f"sub_{str(uuid.uuid4())[:8]}"
    agent = AsyncSubAgent(
        subagent_id=subagent_id,
        llm=llm,
        prompt=prompt,
        sys_prompt=sys_prompt,
        context_window=context_window,
    )
    agent._lead_name = lead_name

    thread = threading.Thread(target=agent.run, daemon=True)
    thread.start()

    return subagent_id