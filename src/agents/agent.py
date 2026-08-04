from __future__ import annotations
import sys, os
sys.path.append(os.path.join(os.path.dirname(__file__),'../../'))
from src.infrastructure.clients.llm_clients import LLMClient
from config.config import (APP_ROOT, KEEP_RECENT_ROUNDS, CONTEXT_WINDOW,
                            AUTO_COMPACT_ENABLED, AUTO_COMPACT_THRESHOLD_RATIO, WORKDIR,
                            TASK_NAG_ROUNDS,
                            LOOP_DETECT_ENABLED, LOOP_DETECT_WINDOW, LOOP_REPEAT_THRESHOLD,
                            LEAD_AGENT_LOG_DIR)
from config.prompts.single import MAIN_AGENT, SKILL_SECTION
from config.prompts.subagent import SUBAGENT_BASE, SUBAGENT_SKILL_SECTION
from config.prompts.compact import (AUTO_COMPACT_SYSTEM, AUTO_COMPACT_SUMMARY_PROMPT,
     AUTO_COMPACT_SUMMARY_WRAPPER, AUTO_COMPACT_ASSISTANT_RESPONSE)
from config.tools import LEAD_AGENT_TOOLS as MAIN_AGENT_TOOLS, TOOL_HANDLERS
from src.utils.managers import SkillLoader
from src.infrastructure.message_bus import bus
from src.infrastructure.task_store import tasks as task_store
from src.utils.stdio_redirect import LeadAgentLogger
from src.infrastructure.context_store import ContextStore
from src.utils.function import _get_user_input_queue
from src.utils.shell_backend import get_backend
import subprocess
import json
import re
import threading
import uuid
from datetime import datetime
from collections import deque


class PermissionInterrupt(Exception):
    """Signal interrupt: permission_request needs immediate handling."""
    pass


class UserSteerInterrupt(Exception):
    """Signal interrupt: user sent a steer message during agent processing.

    The steer text is carried in `message` attribute.  It will be injected
    as a user message into the conversation history (at round boundary) to
    gently course-correct the agent without breaking the turn structure.
    """

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class BaseAgent:
    """Agent 基类，提供公共属性和基础功能"""

    def __init__(
            self,
            llm: LLMClient,
            sys_prompt: str,
            tools: list,
            context_window: int,
            tool_handler: dict = TOOL_HANDLERS,
            keep_recent_rounds: int = KEEP_RECENT_ROUNDS,
            max_continuation_rounds: int = 3) -> None:
        self.llm = llm
        self.sys_prompt = sys_prompt
        self.tools = tools
        self.context_window = context_window
        self.tool_handler = tool_handler
        self.max_continuation_rounds = max_continuation_rounds  # 截断后最大继续次数
        self.token_used = 0 # 已经使用的token数量
        self._compact_count = 0  # 记录 auto_compact 压缩次数
        self.loop_detect_enabled = LOOP_DETECT_ENABLED  # 是否启用循环检测
        self.loop_detect_window = LOOP_DETECT_WINDOW  # 循环检测滑动窗口大小
        self.loop_repeat_threshold = LOOP_REPEAT_THRESHOLD  # 判定循环的连续重复次数
        # Streaming buffer for log-friendly output (subagent uses buffer, lead streams directly)
        self._stream_buffer = ""
        self._use_stream_buffer = False
        self._current_round = 0
        # task_id → threading.Event, set() when background bash finishes
        self._background_bash_tasks: dict[str, threading.Event] = {}
        # ContextStore: 对话历史集中管理（存储层 + 视图层）
        self._store = ContextStore(
            keep_recent_rounds=keep_recent_rounds,
            milestone_extractors=self._get_milestone_extractors(),
        )

    def _get_milestone_extractors(self) -> dict:
        """返回 milestone extractors 字典。子类可重写以使用对应的工具集配置。"""
        from config.tools import MILESTONE_EXTRACTORS
        return dict(MILESTONE_EXTRACTORS)

    def _should_break_loop(self, assistant_message) -> bool:
        """Post-tool-call exit check. Override in subclasses for custom exit conditions."""
        return False

    def _has_running_background_bash(self) -> bool:
        """Check if there are any running background bash tasks."""
        return bool(self._background_bash_tasks)


    def _compact_if_needed(self, msg_list: list, messages: list, pre_len: int) -> tuple[list, list, int]:
        """发送前用实际 token 数判断是否需要压缩，执行后返回 (new_msg_list, new_messages, new_pre_len)。

        压缩会替换 messages 的引用，因此需要同步更新 pre_len，
        确保后续 _commit_to_store 能正确提取本轮新增消息。
        """
        if not AUTO_COMPACT_ENABLED:
            return msg_list, messages, pre_len

        prompt_tokens = self.llm._count_tokens(msg_list)
        threshold = int(self.context_window * AUTO_COMPACT_THRESHOLD_RATIO)
        if prompt_tokens <= threshold:
            return msg_list, messages, pre_len

        label = self._get_loop_label()
        prefix = f"[{label}]" if label else "[Auto Compact]"
        # 压缩前先提交本轮已积累但未 commit 的消息，避免压缩后 pre_len 重置导致丢失
        if len(messages) > pre_len:
            self._commit_to_store(messages, pre_len)
        print(f"\033[33m{prefix} Token usage high (prompt_tokens: {prompt_tokens}), compressing context...\033[0m", flush=True)
        # 最多压缩 2 次，防止压缩后仍超限或压缩失败死循环
        for _ in range(2):
            messages = self.auto_compact(messages)
            self.token_used = 0
            msg_list = [{"role": "system", "content": self.sys_prompt}] + messages
            new_tokens = self.llm._count_tokens(msg_list)
            if new_tokens <= threshold:
                break
            print(f"\033[33m[Auto Compact] Still over threshold ({new_tokens}), retrying...\033[0m", flush=True)
        # 压缩后 messages 是新对象，pre_len 重置为压缩后长度
        pre_len = len(messages)
        # 压缩后重置循环检测窗口
        if hasattr(self, '_loop_sig_window'):
            self._loop_sig_window.clear()
        return msg_list, messages, pre_len

    def auto_compact(self, messages: list) -> list:
        """
        使用 LLM 对过往对话生成摘要，压缩上下文。
        
        策略：
        1. 保留 system message
        2. 对中间的对话历史调用 LLM 生成结构化摘要
        3. 保留最近的用户消息和关键的 tool 交互结果
        4. 保留 todo 状态信息
        """
        
        # 分离消息类型
        system_msg = messages[0] if messages and messages[0].get("role") == "system" else None
        non_system_msgs = messages[1:] if system_msg else messages
        
        # 找出最后一条用户消息的索引（当前任务）
        last_user_idx = -1
        for i in range(len(non_system_msgs) - 1, -1, -1):
            if non_system_msgs[i].get("role") == "user":
                last_user_idx = i
                break
        
        # 确定需要压缩的历史消息（排除最后一条用户消息及其之后的内容）
        if last_user_idx > 0:
            history_to_summarize = non_system_msgs[:last_user_idx]
            recent_messages = non_system_msgs[last_user_idx:]
        else:
            history_to_summarize = non_system_msgs
            recent_messages = []
        
        if not history_to_summarize:
            return messages
        
        # 构建需要摘要的历史文本
        history_parts = []
        for msg in history_to_summarize:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if isinstance(content, str) and len(content) > 1000:
                content = content[:1000] + "...[truncated]"
            history_parts.append(f"[{role}]: {content}")
        
        history_text = "\n\n".join(history_parts)
        
        # 构建摘要提示词
        todo_status = self._get_todo_status_for_compact()

        summary_prompt = AUTO_COMPACT_SUMMARY_PROMPT.format(
            history_text=history_text,
            todo_status=todo_status
        )
        
        # 调用 LLM 生成摘要
        try:
            summary_response = self.llm.chat(
                msg_list=[
                    {"role": "system", "content": AUTO_COMPACT_SYSTEM},
                    {"role": "user", "content": summary_prompt}
                ],
                stream=False,
                tools=None,
                max_tokens=2000
            )
            summary_text = summary_response.choices[0].message.content
        except Exception as e:
            print(f"\033[33m[Auto Compact] Summary generation failed: {e}, skipping compression\033[0m", flush=True)
            return messages  # 如果摘要失败，返回原始消息
        
        self._compact_count += 1
        print(f"\033[33m[Auto Compact] Context compressed (compact #{self._compact_count}, token_used: {self.token_used})\033[0m", flush=True)
        
        # 构建新的消息列表
        new_messages = []
        
        # 添加 system message
        if system_msg:
            new_messages.append(system_msg)
        
        # 添加摘要作为用户消息
        new_messages.append({
            "role": "user",
            "content": AUTO_COMPACT_SUMMARY_WRAPPER.format(summary_text=summary_text)
        })
        
        # 添加摘要的确认（assistant 角色）
        new_messages.append({
            "role": "assistant",
            "content": AUTO_COMPACT_ASSISTANT_RESPONSE
        })
        
        # 添加最近的消息（最后一条用户消息及其后的内容）
        new_messages.extend(recent_messages)
        
        return new_messages
    
    def _process_single_tool(self, tool_name: str, args: dict) -> str:
        """
        处理单个工具调用
        :param tool_name: 工具名称
        :param args: 工具参数
        :return: 工具执行结果
        """
        handler = self.tool_handler.get(tool_name)
        print(f"use {tool_name} with args: {args}")
        try:
            output = handler(**args) if handler else f"Unknown tool: {tool_name}"
        except KeyError as e:
            missing_param = str(e).strip("'")
            output = f"[Tool Error] {missing_param}. Please provide all required parameters for {tool_name}. Check the tool definition for required parameters."
        except subprocess.TimeoutExpired:
            raise
        except Exception as e:
            output = f"[Tool Error] {e}"
        return output

    def _process_tool_calls(self, assistant_message) -> list:
        """
        处理工具调用，返回工具结果列表。bash 命令超时时在捕获层自动放入后台线程。
        """
        tool_results = []

        if not assistant_message.tool_calls:
            return tool_results

        for tool_call in assistant_message.tool_calls:
            tool_name = tool_call.function.name
            try:
                args = json.loads(tool_call.function.arguments)
            except json.JSONDecodeError:
                args = {}

            try:
                output = self._process_single_tool(tool_name, args)
            except subprocess.TimeoutExpired:
                if tool_name != "bash":
                    raise
                output = self._promote_bash_to_background(args.get("command", ""))

            print(f" {str(output)[:200]}")
            tool_results.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": output if output is not None else ""
            })

        return tool_results

    def _promote_bash_to_background(self, command: str) -> str:
        """将超时的 bash 命令放入后台线程继续执行，完成后通过 bus 通知 agent。"""
        task_id = f"bash_{uuid.uuid4().hex[:8]}"
        event = threading.Event()
        self._background_bash_tasks[task_id] = event
        agent_name = getattr(self, "_agent_name", "agent")

        def _bg_run():
            try:
                # 通过平台后端执行（POSIX: bash+setsid；Windows: PowerShell+进程组）
                r = get_backend().run(command, str(WORKDIR), timeout=None)
                out = (r.stdout + r.stderr).strip()[:50000]
                output_text = (
                    f"[background_bash_done] task={task_id} exit_code={r.returncode}\n{out}"
                    if out else f"[background_bash_done] task={task_id} exit_code={r.returncode} (no output)"
                )
            except Exception as ex:
                output_text = f"[background_bash_done] task={task_id} error={ex}"
            finally:
                bus.send(agent_name, agent_name, output_text, msg_type="message")
                event.set()

        threading.Thread(target=_bg_run, daemon=True).start()
        return (
            f"[long-running command promoted to background]\n"
            f"  - Task ID: {task_id}\n"
            f"  - Command: {command[:200]}\n\n"
            "The command is now running in the background. You will be notified when it completes."
        )

    def _tool_call_signature(self, assistant_message) -> str | None:
        """
        提取一轮工具调用的签名，用于循环检测。
        签名由所有工具调用的 name + 参数(规范化JSON) 组成。
        无 tool_calls 时返回 None。
        """
        if not assistant_message.tool_calls:
            return None
        parts = []
        for tc in assistant_message.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments)
                args_str = json.dumps(args, sort_keys=True)
            except (json.JSONDecodeError, TypeError):
                args_str = tc.function.arguments
            parts.append(f"{name}({args_str})")
        return "|".join(parts)

    def _is_loop_detected(self, current_sig: str) -> bool:
        """
        检查当前工具调用签名是否构成循环。
        维持一个滑动窗口，如果窗口尾部连续 N 个签名相同且达到阈值，判定为循环。
        """
        if not hasattr(self, '_loop_sig_window'):
            self._loop_sig_window = deque(maxlen=self.loop_detect_window)

        self._loop_sig_window.append(current_sig)
        window = self._loop_sig_window

        if len(window) < self.loop_repeat_threshold:
            return False

        # 检查窗口尾部是否连续重复 threshold 次
        tail = list(window)[-self.loop_repeat_threshold:]
        return all(sig == tail[0] for sig in tail)

    def _compress_loop_messages(self, messages: list, repeat_sig: str) -> str | None:
        """
        从 messages 末尾往前扫描，找所有与 repeat_sig 匹配的
        assistant(+tool_calls) + tool 消息对，将它们压缩为一条摘要。

        Returns: 压缩掉的轮次数，如果没有找到可压缩的则返回 None。
        """
        if not repeat_sig:
            return None

        # 从后往前找连续的重复轮次 (每轮 = 1个assistant + N个tool)
        loop_rounds = []  # [(start_idx, end_idx), ...]，end_idx 包含
        i = len(messages) - 1
        while i >= 0:
            msg = messages[i]
            if msg.get("role") == "tool":
                # 继续向前跳过这一轮的所有 tool 消息
                i -= 1
                continue
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                # 找到 assistant，回构建这一轮的完整范围
                assistant_idx = i
                # 向后找连续的 tool 消息
                j = assistant_idx + 1
                while j < len(messages) and messages[j].get("role") == "tool":
                    j += 1
                tool_end = j - 1
                loop_rounds.append((assistant_idx, tool_end))
                i = assistant_idx - 1
            else:
                break

        if not loop_rounds:
            return None

        # 反向排序，从后往前处理
        loop_rounds.reverse()

        # 构建 tool_name + args 的规范化字符串用于比对
        def build_sig_from_dict(msg):
            """从 dict 格式的 assistant message 构建签名"""
            if not msg.get("tool_calls"):
                return None
            parts = []
            for tc in msg["tool_calls"]:
                name = tc.get("function", {}).get("name", "")
                args_raw = tc.get("function", {}).get("arguments", "")
                try:
                    args = json.loads(args_raw)
                    args_str = json.dumps(args, sort_keys=True)
                except (json.JSONDecodeError, TypeError):
                    args_str = args_raw
                parts.append(f"{name}({args_str})")
            return "|".join(parts)

        # 只压缩那些签名匹配的轮次
        matched_rounds = []
        for start, end in loop_rounds:
            sig = build_sig_from_dict(messages[start])
            if sig == repeat_sig:
                matched_rounds.append((start, end))
            else:
                break  # 不连续了，停止

        if not matched_rounds:
            return None

        # 提取工具名用于摘要
        first_match = messages[matched_rounds[0][0]]
        tool_names = []
        for tc in first_match.get("tool_calls", []):
            tool_names.append(tc.get("function", {}).get("name", "unknown"))
        tool_name_str = ", ".join(tool_names)

        repeated = len(matched_rounds)
        # 需要移除的消息索引集合
        to_remove = set()
        for start, end in matched_rounds:
            for idx in range(start, end + 1):
                to_remove.add(idx)

        removed_count = len(to_remove)
        if removed_count == 0:
            return None

        # 构建替换消息
        summary = (
            f"[Loop Detector: compressed {repeated} repeated rounds "
            f"(called {tool_name_str} with identical arguments each time]. "
            f"Results were identical — repeating the same action produces nothing new.]"
        )

        # 用摘要替换循环消息
        new_messages = []
        for idx, msg in enumerate(messages):
            if idx in to_remove:
                continue
            # 在第一个被删除的位置插入摘要
            if idx == min(to_remove) and summary not in [m.get("content", "") for m in new_messages]:
                new_messages.append({
                    "role": "user",
                    "content": summary,
                })
            new_messages.append(msg)

        # 如果整个 messages 尾部都是循环消息，摘要可能没被插入
        if summary not in [m.get("content", "") for m in new_messages]:
            new_messages.append({
                "role": "user",
                "content": summary,
            })

        # 用新列表替换原列表（原地修改语义）
        messages.clear()
        messages.extend(new_messages)

        return repeated

    def _break_loop(self, messages: list) -> None:
        """
        检测到循环时：
        1. 压缩重复的循环轮次，释放上下文空间
        2. 注入一条 user 消息打断 LLM，要求其重新思考策略
        3. 清空循环检测窗口
        """
        if hasattr(self, '_loop_sig_window'):
            window = list(self._loop_sig_window)
            # 找出重复的签名
            repeat_sig = window[-1] if window else None
            self._loop_sig_window.clear()
        else:
            repeat_sig = None

        # 压缩循环部分
        if repeat_sig:
            compressed = self._compress_loop_messages(messages, repeat_sig)
            if compressed:
                label = self._get_loop_label()
                prefix = f"[{label}]" if label else "[Loop Detector]"
                print(f"\033[33m{prefix} Compressed {compressed} repeated loop rounds from context.\033[0m")

        label = self._get_loop_label()
        prefix = f"[{label}]" if label else "[Loop Detector]"
        print(f"\033[31m{prefix} Loop detected! Breaking cycle and injecting interruption.\033[0m")

        messages.append({
            "role": "user",
            "content": (
                "<loop_detector_intervention>\n"
                "IMPORTANT: You were stuck in a repetitive loop, making the same tool "
                "calls with (near-)identical arguments. Those repeated rounds have been "
                "removed from context to save tokens.\n\n"
                "STOP repeating the same actions. Instead:\n"
                "1. Analyze why the previous approach isn't working\n"
                "2. Try a DIFFERENT strategy or tool\n"
                "3. If you've exhausted all options, summarize what you've learned and report "
                "to the user rather than continuing to loop\n"
                "</loop_detector_intervention>"
            ),
        })

    def _get_todo_status_for_compact(self) -> str:
        """用于 auto_compact 摘要时获取 todo 状态，子类可重写"""
        return ""

    def _ask_user(self, prompt: str) -> bool:
        """向用户询问 y/n 问题，返回批准与否。"""
        try:
            uiq = _get_user_input_queue()
        except LookupError:
            uiq = None
        if uiq is not None:
            ans = uiq.prompt_and_wait(prompt)
            return ans is not None and ans.strip().lower() in ("y", "yes")
        else:
            try:
                ans = input(prompt)
                return ans.strip().lower() in ("y", "yes")
            except (EOFError, KeyboardInterrupt):
                return False

    def _get_loop_label(self) -> str:
        """每轮打印的前缀标签，子类可重写"""
        return ""

    def _accumulate_tokens(self, total_tokens: int) -> int:
        """token 累计策略：返回新的 token_used 值。
        默认覆盖（Agent 行为），子类可重写为累加（SubAgent 行为）。"""
        return total_tokens

    def _print_round_prefix(self) -> None:
        """每轮开始前打印前缀，子类可重写"""
        pass

    def _print_content_suffix(self, has_content: bool) -> None:
        """assistant 输出内容后打印后缀，子类可重写"""
        if has_content:
            print()

    def _flush_stream_buffer(self) -> str:
        """Flush 流式 buffer 内容。lead agent 默认直接返回（已由流式回调直接 print），
        sub agent 重写此方法输出到日志。返回 flushed 的文本。"""
        text = self._stream_buffer
        self._stream_buffer = ""
        return text

    def _on_chunk(self, text: str) -> None:
        """流式 chunk 回调，子类可重写"""
        if self._use_stream_buffer:
            self._stream_buffer += text
        else:
            print(text, end="", flush=True)

    def _check_pending_subagents(self) -> list[str]:
        """检查是否还有 pending 的 subagent。子类可重写返回 pending ID 列表。"""
        return []

    @staticmethod
    def _make_assistant_from_dict(msg_dict: dict):
        """从 dict 构造兼容 _process_tool_calls 的 assistant 对象"""
        from types import SimpleNamespace
        tool_calls_dicts = msg_dict.get("tool_calls") or []
        tool_calls = [
            SimpleNamespace(
                id=tc.get("id", ""),
                function=SimpleNamespace(
                    name=tc.get("function", {}).get("name", ""),
                    arguments=tc.get("function", {}).get("arguments", "")
                )
            )
            for tc in tool_calls_dicts
        ]
        return SimpleNamespace(
            content=msg_dict.get("content", ""),
            tool_calls=tool_calls
        )

    @staticmethod
    def _find_last_assistant_idx(messages: list) -> int | None:
        """反向查找最后一条 assistant 消息的索引"""
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "assistant":
                return i
        return None

    @classmethod
    def _merge_into_message(cls, target_msg: dict, assistant_message) -> None:
        """将 assistant_message 的内容和 tool_calls 合并到 target_msg 中，处理截断导致的重复"""
        old_content = target_msg.get("content", "")
        new_content = assistant_message.content or ""
        # 去除新内容开头与旧内容结尾的重复文本
        if old_content and new_content:
            overlap_len = 0
            max_overlap = min(len(old_content), len(new_content))
            for l in range(1, max_overlap + 1):
                if old_content.endswith(new_content[:l]):
                    overlap_len = l
            if overlap_len:
                new_content = new_content[overlap_len:]
        target_msg["content"] = old_content + new_content

        if assistant_message.tool_calls:
            existing_calls = target_msg.get("tool_calls", [])
            existing_ids = {tc.get("id") for tc in existing_calls if tc.get("id")}
            new_calls = [
                tc.model_dump(exclude_none=True)
                for tc in assistant_message.tool_calls
                if not tc.id or tc.id not in existing_ids
            ]
            if new_calls:
                target_msg["tool_calls"] = existing_calls + new_calls

    def _handle_truncation(
        self,
        messages: list,
        assistant_message,
        continuation_count: int
    ) -> tuple:
        """
        处理模型输出被截断的情况（方案三：截断后继续）

        Args:
            messages: 消息历史
            assistant_message: 被截断的助手消息
            continuation_count: 当前已继续的次数

        Returns:
            (should_continue, new_continuation_count, merged_message) 元组
            - should_continue: 是否应该继续循环
            - new_continuation_count: 更新后的继续次数
            - merged_message: 合并后的最终 assistant 消息字典（当 should_continue=False 时有效）

        注意：
        - 如果 should_continue=True，调用者不应添加 assistant_message（因为会继续生成）
        - 如果 should_continue=False，调用者应该使用 merged_message（而不是原始的 assistant_message）
        """
        print("\n\033[33m[Warning] Response was truncated due to max_tokens limit (8000).\033[0m")

        if assistant_message.tool_calls:
            print("\033[33m[Warning] Tool calls may be incomplete due to truncation.\033[0m")

        # 达到最大继续次数，合并后退出
        if continuation_count >= self.max_continuation_rounds:
            print("\033[31m[Error] Max continuation attempts reached. Stopping.\033[0m")
            last_idx = self._find_last_assistant_idx(messages)
            if last_idx is not None:
                self._merge_into_message(messages[last_idx], assistant_message)
                return False, continuation_count, messages[last_idx]
            return False, continuation_count, assistant_message.model_dump(exclude_none=True)

        # 还能继续，递增计数并注入 continue prompt
        continuation_count += 1

        if continuation_count > 1:
            # 移除上一次的 continue prompt
            for i in range(len(messages) - 1, -1, -1):
                if messages[i].get("role") == "user" and messages[i].get("content", "").startswith("[Your previous response was truncated"):
                    messages.pop(i)
                    break
            # 合并到上一次截断的 assistant 消息
            last_idx = self._find_last_assistant_idx(messages)
            self._merge_into_message(messages[last_idx], assistant_message)
        else:
            # 第一次截断，直接追加
            messages.append(assistant_message.model_dump(exclude_none=True))

        messages.append({
            "role": "user",
            "content": "[Your previous response was truncated due to length limit. "
                       "Please continue from where you left off. "
                       "If you were in the middle of a tool call, complete it. "
                       "If you were generating text, continue the text.]"
        })
        print(f"\033[33m[Info] Asking model to continue (attempt {continuation_count}/{self.max_continuation_rounds})...\033[0m")
        return True, continuation_count, None

    def _loop_core(
        self,
        messages: list | None = None,
        *,
        max_rounds: int | None = None,
        on_round_start: callable | None = None,
        on_loop_exit: callable | None = None,
    ) -> str | None:
        """
        Agent 主循环的通用实现。

        核心循环体（on_round_start → auto_compact → chat_stream → tool_calls）
        对 Agent / SubAgent 完全一致，差异通过回调参数化。

        Args:
            messages: 对话历史列表。传 None 时从 self._store.build_context() 构建。

        on_round_start: 每轮迭代开始时调用的回调，接收 messages 参数。可以返回一个 dict，
            如果有返回值则 append 到 messages 中。用于 inbox drain、
            background notification 等扩展点。

        Returns:
            如果 max_rounds 被设置（委托模式），返回最终文本；否则返回 None。
        """
        using_store = messages is None
        messages = self._store.build_context() if using_store else list(messages)
        pre_len = len(messages)

        assistant_message = None
        round_count = 0
        continuation_count = 0  # 截断后继续的次数
        self._loop_sig_window = deque(maxlen=self.loop_detect_window)  # 初始化循环检测窗口

        try:
            while True:
                if max_rounds is not None and round_count >= max_rounds:
                    break
                round_count += 1

                # 扩展点：每轮开始时执行回调（如 inbox drain）
                # 返回 list[dict] 时逐条 extend，返回单个 dict 时兼容 append
                if on_round_start is not None:
                    try:
                        extra = on_round_start(messages)
                    except PermissionInterrupt:
                        self._handle_permission_interrupt(None, messages)
                        continue
                    except UserSteerInterrupt:
                        raise
                    if extra:
                        if isinstance(extra, list):
                            messages.extend(extra)
                        else:
                            messages.append(extra)

                msg_list = [{"role": "system", "content": self.sys_prompt}] + messages
                msg_list, messages, pre_len = self._compact_if_needed(msg_list, messages, pre_len)

                # 流式调用
                self._print_round_prefix()
                self._stream_buffer = ""
                try:
                    response = self.llm.chat_stream(
                        msg_list=msg_list,
                        tools=self.tools,
                        max_tokens=8_000,
                        on_chunk=self._on_chunk,
                    )
                except Exception as e:
                    print(f"\033[31m[Error] LLM call failed after retries: {e}\033[0m")
                    break

                assistant_message = response.choices[0].message

                # 检测截断（finish_reason == "length" 表示达到 max_tokens 被截断）
                finish_reason = getattr(response.choices[0], 'finish_reason', None)

                # token 累计（无论是否截断都要累计）
                if response.usage:
                    total_tokens = getattr(response.usage, "total_tokens", 0)
                    self.token_used = self._accumulate_tokens(total_tokens)

                # 处理截断情况：方案三 - 截断后继续
                if finish_reason == 'length':
                    should_continue, continuation_count, merged_message = self._handle_truncation(
                        messages, assistant_message, continuation_count
                    )
                    if should_continue:
                        continue

                    # 截断结束：统一构造 assistant_message，兼容后续 tool_calls 处理
                    messages.append(merged_message)
                    assistant_message = self._make_assistant_from_dict(merged_message)
                else:
                    messages.append(assistant_message.model_dump(exclude_none=True))

                # Flush buffered stream content (for subagent logging)
                self._flush_stream_buffer()

                has_content = bool(assistant_message.content)
                self._print_content_suffix(has_content)

                # No tool calls → definite end of this turn
                if not assistant_message.tool_calls:
                    break

                tool_results = self._process_tool_calls(assistant_message)
                messages.extend(tool_results)

                # Post-tool-call exit check (e.g., task_delegate → exit for pending
                # subagents).  Run AFTER tool results so spawn side-effects are applied.
                if self._should_break_loop(assistant_message):
                    break

                # 循环检测：检查是否陷入重复工具调用循环
                if self.loop_detect_enabled:
                    sig = self._tool_call_signature(assistant_message)
                    if sig and self._is_loop_detected(sig):
                        self._break_loop(messages)
        except UserSteerInterrupt:
            # 中断前仍然 commit 已积累的消息（保证 raw_events 写入顺序与 log 一致）
            if using_store:
                self._commit_to_store(messages, pre_len)
            raise

        # 循环退出回调
        if on_loop_exit is not None:
            on_loop_exit(messages)

        # 如果通过 store 获取的 messages，将本轮新消息 commit 回 store
        if using_store:
            self._commit_to_store(messages, pre_len)

        # 委托模式返回最终文本
        if max_rounds is not None:
            final_text = (
                assistant_message.content or "" if assistant_message else ""
            )
            return final_text if final_text.strip() else "(no summary)"
        return None

    def _commit_to_store(self, messages: list, pre_len: int) -> None:
        """将本轮 _loop_core 产生的新消息 commit 回 self._store。

        同时将 main.py 中通过 append_user_message 写入的 turn_id=None
        触发用户消息（以及可能的 steer）重新绑定到当前 turn_id，
        避免 build_context 输出中用户消息错误相邻。
        """
        new_messages = messages[pre_len:]
        if not new_messages:
            return

        summary_marker_idx = None
        for i, msg in enumerate(messages[:pre_len]):
            if msg.get("role") == "user" and isinstance(msg.get("content", ""), str):
                content = msg.get("content", "")
                if "<context_summary>" in content or "<summary_checkpoint>" in content:
                    summary_marker_idx = i
                    break

        if summary_marker_idx is not None:
            raw = messages[summary_marker_idx]["content"]
            m = re.search(r"<context_summary>.*?\n\n(.*?)</context_summary>", raw, re.DOTALL)
            if not m:
                m = re.search(r"<summary_checkpoint>(.*?)</summary_checkpoint>", raw, re.DOTALL)
            summary_text = m.group(1).strip() if m else raw
            self._store.append_summary_checkpoint(summary_text)
            # Only commit genuinely new messages from _loop_core.
            # The "bridge" context (recent_messages from auto_compact) already exists
            # in raw_events before the checkpoint. build_context handles re-injection.
            turn_id = self._store.next_turn_id()
            self._store.commit_turn(new_messages, turn_id,
                                    rebind_pending_non_turn_users=True)
        else:
            turn_id = self._store.next_turn_id()
            self._store.commit_turn(new_messages, turn_id,
                                    rebind_pending_non_turn_users=True)

class Agent(BaseAgent):

    def __init__(
            self, 
            llm: LLMClient,
            context_window: int,
            tools: list = MAIN_AGENT_TOOLS,
            tool_handler: dict = TOOL_HANDLERS) -> None:
        self._agent_name = "lead"
        # 先不设置 sys_prompt，等 skill_loader 初始化后再构建
        super().__init__(llm=llm, context_window=context_window, sys_prompt="", tools=tools, tool_handler=tool_handler)
        self.skill_loader = SkillLoader(skills_dir=APP_ROOT.joinpath('skills'))
        self.sys_prompt = self._build_sys_prompt()
        # Pending subagent 跟踪：spawn 时加入，subagent_done 时移除
        self._pending_subagents: set = set()
        self._pending_lock = threading.Lock()
        # Task plan nag tracking
        self._task_plan_active = False  # 是否有活跃的 task 计划
        self._task_nag_rounds = 0  # 连续未操作 task 的轮数
        # Lead agent log file
        self._log_round = 0
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        log_file = LEAD_AGENT_LOG_DIR / f"lead_{ts}.log"
        self.log_path = str(log_file)
        self._logger = LeadAgentLogger("Lead", log_file)
        self._log("LeadAgent started")

    def _get_milestone_extractors(self) -> dict:
        from config.tools import LEAD_MILESTONE_EXTRACTORS
        return dict(LEAD_MILESTONE_EXTRACTORS)

    def _process_single_tool(self, tool_name: str, args: dict) -> str:
        """
        重写父类方法，添加 Agent 特有的 task_delegate 工具处理
        """
        self._log(f"  [Tool] {tool_name}({json.dumps(args, ensure_ascii=False)[:200]})")
        if tool_name == "task_delegate":
            desc = args.get("description", "subtask")
            prompt = args.get("prompt", "")
            skill = args.get("skill_name", None)
            task_id = args.get("task_id", None)
            print(f"> task_delegate ({desc}): {prompt}")
            if skill:
                print(f"> With Skill: {skill}")
            if task_id:
                print(f"> Bound to task #{task_id}")
            subagent_id = self.delegate(prompt, skill)
            if task_id:
                task_store.update(task_id, owner=subagent_id)
                print(f"> Task #{task_id} owner updated to {subagent_id}")

            lines = [
                "Task delegated successfully.",
                f"  - SubAgent ID: {subagent_id}",
                f"  - Status: running (background)",
                f"  - Description: {desc}",
            ]
            if task_id:
                lines.append(f"  - Bound to task: {task_id}")
            if skill:
                lines.append(f"  - Skill: {skill}")
            lines.append("")
            lines.append("Note: Results will arrive asynchronously via inbox. You can use task_list to check progress, proceed with other work, or wait for the SubAgent to report back.")
            result = "\n".join(lines)
            self._log(f"  [Result] {result[:500]}")
            return result
        if tool_name == "task_create":
            print(f"use {tool_name} with args: {args}")
            result = task_store.create(
                args.get("subject", ""),
                args.get("description", ""),
                args.get("owner", ""),
            )
            self._task_plan_active = True
            self._task_nag_rounds = 0
            res_str = json.dumps(result, indent=2, ensure_ascii=False)
            self._log(f"  [Result] {res_str[:500]}")
            return res_str
        if tool_name == "task_update":
            print(f"use {tool_name} with args: {args}")
            result = task_store.update(
                args.get("task_id"),
                status=args.get("status"),
                add_blocked_by=args.get("add_blocked_by"),
                remove_blocked_by=args.get("remove_blocked_by"),
            )
            self._task_nag_rounds = 0
            # If all tasks are completed, deactivate the plan
            all_tasks = task_store.list_all()
            if all_tasks and all(t["status"] == "completed" for t in all_tasks):
                self._task_plan_active = False
            res_str = json.dumps(result, indent=2, ensure_ascii=False)
            self._log(f"  [Result] {res_str[:500]}")
            return res_str
        if tool_name == "task_list":
            print(f"use {tool_name} with args: {args}")
            result = task_store.list_all_formatted(
                owner=args.get("owner"),
                status=args.get("status"),
            )
            self._log(f"  [Result] {str(result)[:500]}")
            return result
        if tool_name == "task_get":
            print(f"use {tool_name} with args: {args}")
            result = task_store.get(args.get("task_id"))
            res_str = json.dumps(result, indent=2, ensure_ascii=False)
            self._log(f"  [Result] {res_str[:500]}")
            return res_str
        result = super()._process_single_tool(tool_name, args)
        self._log(f"  [Result] {str(result)[:500]}")
        return result

    def _process_tool_calls(self, assistant_message) -> list:
        """Override to inject task progress nag after tool processing."""
        tool_results = super()._process_tool_calls(assistant_message)

        if self._task_plan_active:
            # Check if this round used task management tools
            did_task_action = False
            if assistant_message.tool_calls:
                for tc in assistant_message.tool_calls:
                    name = tc.function.name
                    if name in ("task_create", "task_update", "task_delegate", "task_list"):
                        did_task_action = True
                        break

            if not did_task_action:
                self._task_nag_rounds += 1
                if self._task_nag_rounds >= TASK_NAG_ROUNDS:
                    tool_results.append({
                        "role": "user",
                        "content": "<task_reminder> You have an active task plan. Use task_update to mark progress, task_delegate to assign work to subagents, or task_list to check status. After creating tasks, decide the next step: delegate, execute, or update dependencies.</task_reminder>"
                    })
            else:
                self._task_nag_rounds = 0

        return tool_results

    def _build_sys_prompt(self):
        skill_prompt = self.skill_loader.build_registry_prompt()
        prompt = MAIN_AGENT.replace("{WORKDIR}", str(WORKDIR))
        if skill_prompt:
            skill_block = SKILL_SECTION.replace("{skill_registry}", skill_prompt)
            prompt = prompt.replace("{SKILL_SECTION}", skill_block)
        else:
            prompt = prompt.replace("{SKILL_SECTION}", "").replace("\n\n\n", "\n\n")
        return prompt

    def delegate(self, query: str, skill_name: str | None = None) -> str:
        """委托任务给 Async Sub Agent（异步，非阻塞）。返回 subagent_id。"""
        from src.agents.async_subagent import spawn_subagent

        sys_prompt = None
        if skill_name:
            skill_md = self.skill_loader.build_full_skill_prompt(skill_name)
            if skill_md:
                sys_prompt = SUBAGENT_BASE + SUBAGENT_SKILL_SECTION.replace("{skill_name}", skill_name).replace("{skill_md}", skill_md)
            else:
                print(f"skill {skill_name} doesn't exist.")

        subagent_id = spawn_subagent(
            lead_name="lead",
            llm=self.llm,
            prompt=query,
            sys_prompt=sys_prompt,
            context_window=self.context_window,
        )

        def on_completion(msg):
            with self._pending_lock:
                self._pending_subagents.discard(subagent_id)

        self._pending_subagents.add(subagent_id)
        bus.register_completion_listener(subagent_id, on_completion)

        return subagent_id

    def _log(self, *args):
        """Write to lead agent log file"""
        self._logger.log(*args)

    def _on_chunk(self, text: str) -> None:
        """Override: print to terminal and buffer for log flush"""
        print(text, end="", flush=True)
        self._stream_buffer += text

    def _print_round_prefix(self) -> None:
        self._log_round += 1
        print("\033[36m assistant >> \033[0m ", end="", flush=True)
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
            print()
            self._log("")

    def _get_loop_label(self) -> str:
        return "LeadAgent"

    def _check_pending_subagents(self) -> list[str]:
        with self._pending_lock:
            return list(self._pending_subagents)

    def _check_permission_signal(self):
        """Peek inbox for permission_request. Returns the message dict or None."""
        inbox = bus.peek_inbox("lead")
        for msg in inbox:
            if msg.get("type") == "permission_request":
                return msg
        return None

    def _handle_permission_interrupt(self, perm_msg: dict, messages: list) -> bool:
        """Drain and process a permission_request from inbox via signal interrupt.

        Any non-permission messages in the same batch are re-enqueued so the
        next idle-drain or loop iteration can pick them up.
        """
        inbox = bus.read_inbox("lead")
        handled = False
        for msg in inbox:
            if msg.get("type") == "permission_request":
                self._reply_permission(msg)
                handled = True
            else:
                bus.send("system", "lead", msg.get("content", ""),
                                    msg_type=msg.get("type", "message"),
                                    extra=msg.get("extra", {}))
        return handled

    def _reply_permission(self, msg: dict):
        """Reply to a permission_request from a subagent. Non-blocking: auto-approve
        non-dangerous commands, deny dangerous ones (signal path has no user prompt)."""
        extra = msg.get("extra", {})
        subagent_id = extra.get("subagent_id", msg.get("sender", ""))
        request_id = extra.get("request_id", "")
        reason = extra.get("reason", "")
        command = extra.get("command", "")

        first_line = reason.split("\n")[0].lower()
        is_dangerous = first_line.startswith("- dangerous") or first_line.startswith("dangerous") or first_line.startswith("- sensitive") or first_line.startswith("sensitive")

        if is_dangerous:
            print(f"\n\033[33m[SubAgent {subagent_id}] 请求执行危险命令:\033[0m")
            print(f"   原因: {reason}")
            print(f"   命令: {command}")
            self._log(f"[Permission] SubAgent {subagent_id} request: {reason} (command: {command})")
            granted = self._ask_user("   允许执行吗? (y/N): ")
            self._log(f"[Permission] User response: {'approved' if granted else 'denied'}")
        elif not is_dangerous:
            granted = True
            self._log(f"[Permission] Auto-approved for {subagent_id}: {reason} (command: {command})")

        bus.send(
            "lead", subagent_id, "",
            msg_type="permission_response",
            extra={"request_id": request_id, "subagent_id": subagent_id, "granted": granted},
        )

        status = "approved" if granted else "denied"
        print(f"\033[33m[Lead] Permission {status} for {subagent_id}: {reason} (command: {command})\033[0m")
        self._log(f"[Lead] Permission {status} for {subagent_id}: {reason} (command: {command})")

    def _should_break_loop(self, assistant_message) -> bool:
        """Exit loop after tool calls if task_delegate spawned pending subagents,
        lead sent a revision_request to a subagent, or a long-running bash
        command was promoted to background execution."""
        has_delegate = any(
            tc.function.name == "task_delegate"
            for tc in assistant_message.tool_calls
        )
        if has_delegate:
            pending = self._check_pending_subagents()
            if pending:
                return True

        has_revision_request = False
        for tc in assistant_message.tool_calls:
            if tc.function.name == "send_message":
                try:
                    args = json.loads(tc.function.arguments)
                    if args.get("msg_type") == "revision_request":
                        has_revision_request = True
                        break
                except (json.JSONDecodeError, TypeError):
                    pass
        if has_revision_request:
            return True

        # Clean up completed background bash tasks, keep running ones
        done_ids = [tid for tid, ev in self._background_bash_tasks.items() if ev.is_set()]
        for tid in done_ids:
            del self._background_bash_tasks[tid]
        if self._background_bash_tasks:
            return True

        return False

    def _loop(
        self,
        on_round_start: callable | None = None,
        on_loop_exit: callable | None = None,
        steer_queue=None,
    ):
        """Run the agent loop.  Permission interrupts and user steer messages
        are handled internally: permission is auto-resolved and _loop_core
        resumes automatically; steer messages inject user guidance at round
        boundary without breaking turn structure.

        messages 参数已移除，默认使用 self._store 作为上下文源。
        """
        wrapped_on_round_start = on_round_start

        def signal_check_on_round_start(msgs):
            # 1. Check permission signal
            perm_msg = self._check_permission_signal()
            if perm_msg:
                raise PermissionInterrupt(perm_msg)

            # 2. Check user steer messages
            if steer_queue is not None:
                steers = steer_queue.get_steers()
                if steers:
                    # Check for interrupt command in any steer
                    for s in steers:
                        if s.strip().lower() in ("/interrupt", "/stop"):
                            raise UserSteerInterrupt(None)  # None signals hard break
                    # Inject all non-interrupt steers as user messages
                    steer_text = "\n".join(
                        f"<user_steer>{s}</user_steer>" for s in steers
                    )
                    self._log(f"[Steer] {steer_text}")
                    # steer 通过 on_round_start 的返回值注入 messages，
                    # _commit_to_store 会将其与正确的 turn_id 一起 commit，
                    # 不再需要单独 append_user_message。
                    return {
                        "role": "user",
                        "content": steer_text,
                    }

            if wrapped_on_round_start is not None:
                return wrapped_on_round_start(msgs)
            return None

        try:
            self._loop_core(None, on_round_start=signal_check_on_round_start, on_loop_exit=on_loop_exit)
        except UserSteerInterrupt as exc:
            if exc.message is None:
                print("\033[33m[Lead] User interrupted the agent loop.\033[0m")
                self._log("User interrupted the agent loop")

    def close(self):
        """Close the lead agent logger."""
        self._log("LeadAgent shut down")
        self._logger.close()


