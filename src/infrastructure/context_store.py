"""
ContextStore - 对话历史集中管理

存储层（raw_events）：只追加，永不修改/删除。
视图层（build_context）：从 raw_events 折叠为 OpenAI messages 格式，
        应用摘要 checkpoint + turn-level compression，返回可变副本。
"""

from __future__ import annotations

import copy
import json
import threading
import time
from dataclasses import dataclass, field


# ============================================================
# 1. Event 定义
# ============================================================

@dataclass
class Event:
    """raw_events 中的原子事件。只追加，永不修改。"""
    kind: str
    payload: dict
    ts: float = field(default_factory=time.time)
    turn_id: int | None = None


# ============================================================
# 2. ContextStore
# ============================================================

class ContextStore:
    """集中管理对话历史的存储层 + 视图层。

    存储层 raw_events：只 append，永不修改/删除。
    视图层 build_context：从 raw_events 折叠为 OpenAI messages 格式，
        应用摘要 checkpoint + micro_compact。
    """

    def __init__(self, keep_recent_rounds: int = 5, milestone_extractors: dict | None = None):
        self.raw_events: list[Event] = []
        self.keep_recent_rounds = keep_recent_rounds
        self._lock = threading.Lock()
        self.turn_counter = 0
        self._milestone_extractors = milestone_extractors or {}

    # ---------- 写入（存储层） ----------

    def append(self, event: Event) -> None:
        """追加一个事件到存储层。turn_id=None 表示非回合内事件（inbox injection 等）。"""
        with self._lock:
            self.raw_events.append(event)

    def append_user_message(self, text: str) -> None:
        """便捷方法：追加一条用户消息。"""
        self.append(Event(kind="user_msg", payload={"text": text}))

    def append_assistant_message(self, msg_dict: dict) -> None:
        """便捷方法：追加一条 assistant 回复（OpenAI 格式 dict）。"""
        self.append(Event(kind="assistant_msg", payload=copy.deepcopy(msg_dict)))

    def append_tool_calls(self, msg_dict: dict) -> None:
        """便捷方法：追加 assistant 的 tool_calls（OpenAI 格式 dict）。"""
        self.append(Event(kind="tool_call", payload=copy.deepcopy(msg_dict)))

    def append_tool_result(self, msg_dict: dict) -> None:
        """便捷方法：追加一条 tool 执行结果（OpenAI 格式 dict）。"""
        self.append(Event(kind="tool_result", payload=copy.deepcopy(msg_dict)))

    def append_system_note(self, text: str) -> None:
        """便捷方法：追加一条系统标注（steer, loop break 等）。"""
        self.append(Event(kind="system_note", payload={"text": text}))

    def append_summary_checkpoint(self, summary: str) -> int:
        """追加一个摘要 checkpoint。返回 checkpoint 覆盖的原始事件数量。"""
        with self._lock:
            # 移除旧的 checkpoint（只保留最新一个）
            self.raw_events = [
                e for e in self.raw_events
                if e.kind != "summary_checkpoint"
            ]
            covered = len(self.raw_events)
            self.raw_events.append(
                Event(kind="summary_checkpoint", payload={"summary": summary})
            )
            return covered

    def pending_non_turn_user_count(self) -> int:
        """返回 raw_events 末尾连续的 turn_id=None 的 user_msg 事件数量。

        用于 _commit_to_store 确定需要清理并重新 commit 的触发用户消息数量。
        """
        with self._lock:
            count = 0
            for e in reversed(self.raw_events):
                if e.kind == "user_msg" and e.turn_id is None:
                    count += 1
                else:
                    break
            return count

    def next_turn_id(self) -> int:
        """Generate a new monotonic turn ID. Thread-safe."""
        with self._lock:
            self.turn_counter += 1
            return self.turn_counter

    def commit_turn(self, new_messages: list[dict], turn_id: int,
                     *, rebind_pending_non_turn_users: bool = False) -> None:
        """将一轮 _loop_core 批次追加到存储层，所有事件标记相同的 turn_id。

        Args:
            new_messages: 本轮产生的新消息列表（可能包含 user 消息）。
            turn_id: 分配给本轮的 turn_id。
            rebind_pending_non_turn_users: 如果为 True，先从 raw_events 尾部
                提取所有连续的 turn_id=None 的 user_msg 事件（即上一轮通过
                append_user_message 写入的触发消息），赋予 turn_id 后插入到
                new_messages 之前。这样 build_context 输出中用户消息和 steer
                不会在 turn 之间错误相邻。
        """
        # 将之前 append_user_message 写入的 turn_id=None 用户消息
        # 重新绑定为当前 turn_id
        rebound_texts = []
        if rebind_pending_non_turn_users:
            with self._lock:
                # 从尾部向前收集 turn_id=None 的 user_msg
                temp = []
                while self.raw_events:
                    e = self.raw_events.pop()
                    if e.kind == "user_msg" and e.turn_id is None:
                        rebound_texts.append(e.payload["text"])
                    else:
                        # 遇到非匹配事件，停止，回退
                        temp.append(e)
                        break
                # 回退非匹配事件
                for e in reversed(temp):
                    self.raw_events.append(e)

        # rebound_texts 是倒序收集的，还原顺序
        rebound_texts.reverse()

        events = []
        # 先写入重新绑定的用户消息
        for text in rebound_texts:
            events.append(Event(kind="user_msg", payload={"text": text}, turn_id=turn_id))

        for msg in new_messages:
            role = msg.get("role", "")
            if role == "user" and isinstance(msg.get("content", ""), str):
                content = msg.get("content", "")
                if content.startswith("<summary_checkpoint>") or content.startswith("<context_summary>"):
                    continue
                events.append(Event(kind="user_msg", payload={"text": content}, turn_id=turn_id))
            elif role == "assistant":
                events.append(Event(kind="assistant_msg", payload=copy.deepcopy(msg), turn_id=turn_id))
            elif role == "tool":
                events.append(Event(kind="tool_result", payload=copy.deepcopy(msg), turn_id=turn_id))

        with self._lock:
            self.raw_events.extend(events)

    # ---------- 读取（视图层） ----------

    def build_context(self) -> list[dict]:
        """从 raw_events 折叠为 OpenAI messages 格式。

        流程：
        1. 找最新的 summary_checkpoint
        2. 按 turn_id 分组
        3. 最近 keep_recent_rounds 个 turn → 完整输出
        4. 更早的 turn → 压缩为 user + final conclusion + milestone 记录
        5. turn_id=None 的事件（inbox injection 等）按时间戳 ts 插入到对应 turn 之间
        """
        with self._lock:
            events = list(self.raw_events)

        # Step 1: Handle summary_checkpoint
        cp_idx = self._find_latest_checkpoint(events)
        if cp_idx >= 0:
            checkpoint = events[cp_idx]
            summary = checkpoint.payload["summary"]
            messages = [
                {"role": "user", "content": f"<summary_checkpoint>{summary}</summary_checkpoint>"},
                {"role": "assistant", "content": "Summary acknowledged. I will continue from here."},
            ]
            # Re-inject bridge context: last keep_recent_rounds full turns BEFORE the checkpoint
            pre_cp_events = events[:cp_idx]
            bridge_messages = self._build_bridge_context(pre_cp_events)
            messages.extend(bridge_messages)
            events_to_fold = events[cp_idx + 1:]
        else:
            messages = []
            events_to_fold = events

        # Step 2: Separate turn events from non-turn events, then merge by ts order
        non_turn_events = [e for e in events_to_fold if e.turn_id is None]
        turn_events = sorted(
            [e for e in events_to_fold if e.turn_id is not None],
            key=lambda e: (e.turn_id, e.ts),
        )

        # Step 3: Group turn events by turn_id
        turn_groups: dict[int, list[Event]] = {}
        for e in turn_events:
            turn_groups.setdefault(e.turn_id, []).append(e)

        sorted_turn_ids = sorted(turn_groups.keys())
        if not sorted_turn_ids and not non_turn_events:
            return messages

        # Step 4: Determine which turns are "recent" (keep full) vs "old" (compress)
        num_recent = self.keep_recent_rounds
        recent_ids = (
            set(sorted_turn_ids[-num_recent:])
            if sorted_turn_ids and len(sorted_turn_ids) > num_recent
            else set(sorted_turn_ids)
        )

        # Step 5: Merge turn blocks and non-turn events by timestamp order
        # Build a cursor for non-turn events
        non_turn_events_sorted = sorted(non_turn_events, key=lambda e: e.ts)
        non_turn_idx = 0

        if sorted_turn_ids:
            for turn_id in sorted_turn_ids:
                turn_evts = turn_groups[turn_id]
                # Insert non-turn events whose ts is before this turn's earliest event
                while non_turn_idx < len(non_turn_events_sorted):
                    if non_turn_events_sorted[non_turn_idx].ts <= turn_evts[0].ts:
                        messages.extend(
                            self._fold_events([non_turn_events_sorted[non_turn_idx]])
                        )
                        non_turn_idx += 1
                    else:
                        break

                # Fold or compress this turn
                if turn_id in recent_ids:
                    messages.extend(self._fold_events(turn_evts))
                else:
                    folded = self._fold_events(turn_evts)
                    compressed = self._compress_turn(folded)
                    messages.extend(compressed)

        # Append remaining non-turn events after all turns
        while non_turn_idx < len(non_turn_events_sorted):
            messages.extend(
                self._fold_events([non_turn_events_sorted[non_turn_idx]])
            )
            non_turn_idx += 1

        return messages

    # ---------- Compression helpers ----------

    def _extract_milestones(self, messages: list[dict]) -> str:
        """从一批 messages 中提取里程碑信息（方案 A：按 tool 类型自动采集）。
        返回格式化字符串，无 tool 调用时返回空字符串。
        """
        lines = []
        for msg in messages:
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    tc_id = tc.get("id", "")
                    tc_name = tc.get("function", {}).get("name", "unknown")
                    try:
                        tc_args = json.loads(
                            tc.get("function", {}).get("arguments", "{}")
                        )
                    except (json.JSONDecodeError, TypeError):
                        tc_args = {}

                    # Find matching tool result
                    tool_content = ""
                    for m2 in messages:
                        if (
                            m2.get("role") == "tool"
                            and m2.get("tool_call_id") == tc_id
                        ):
                            tool_content = m2.get("content", "")
                            break

                    extractor = self._milestone_extractors.get(tc_name)
                    if extractor:
                        lines.append(f"  - {extractor(tc_args, tool_content)}")
                    else:
                        lines.append(f"  - {tc_name} called")

        return "\n".join(lines) if lines else ""

    def _compress_turn(self, messages: list[dict]) -> list[dict]:
        """将一个完整的 turn 压缩为：
        [user] 用户原话
        [assistant] 最终回复
        [assistant] [记录] 里程碑

        messages 是 _fold_events 输出的 OpenAI 格式消息列表。
        """
        if not messages:
            return []

        # 1. 提取用户消息（本 turn 内的第一条非空 user 消息，跳过系统注入）
        user_text = None
        for msg in messages:
            if msg.get("role") == "user":
                content = msg.get("content", "")
                skip_prefixes = (
                    "<inbox>", "<loop_detector", "<reminder>",
                    "<task_reminder>", "<revision_feedback>", "<lead_message>",
                )
                if content.strip() and not content.startswith(skip_prefixes):
                    user_text = content.strip()
                    break

        # 2. 提取最终结论：本 turn 中最后一条无 tool_calls 的纯文本 assistant
        final_text = None
        for msg in reversed(messages):
            if msg.get("role") == "assistant" and msg.get("content"):
                if not msg.get("tool_calls"):
                    final_text = msg["content"].strip()
                    break

        # 3. 提取里程碑（副作用事实 + ID/路径锚点）
        milestones = self._extract_milestones(messages)

        # 5. 组装压缩后的消息
        compressed = []
        if user_text:
            truncated_user = (
                user_text[:1000] + ("..." if len(user_text) > 1000 else "")
            )
            compressed.append({"role": "user", "content": truncated_user})

        if final_text or milestones:
            assistant_lines = []
            if final_text:
                truncated_final = (
                    final_text[:1500] + ("..." if len(final_text) > 1500 else "")
                )
                assistant_lines.append(truncated_final)
            elif milestones:
                assistant_lines.append("(no final text response)")
            if milestones:
                assistant_lines.append(f"[记录] 系统里程碑：\n{milestones}")
            compressed.append({
                "role": "assistant",
                "content": "\n\n".join(assistant_lines),
            })

        return compressed

    def _build_bridge_context(self, pre_cp_events: list[Event]) -> list[dict]:
        """Extract the last keep_recent_rounds full turns from pre-checkpoint events
        and fold them as bridge context for continuity."""
        turn_events = [e for e in pre_cp_events if e.turn_id is not None]
        if not turn_events:
            return []

        turn_groups: dict[int, list[Event]] = {}
        for e in sorted(turn_events, key=lambda e: (e.turn_id, e.ts)):
            turn_groups.setdefault(e.turn_id, []).append(e)

        sorted_turn_ids = sorted(turn_groups.keys())
        num_recent = self.keep_recent_rounds
        bridge_turn_ids = (
            sorted_turn_ids[-num_recent:]
            if len(sorted_turn_ids) >= num_recent
            else sorted_turn_ids
        )

        bridge_messages = []
        for turn_id in bridge_turn_ids:
            bridge_messages.extend(self._fold_events(turn_groups[turn_id]))
        return bridge_messages

    def _find_latest_checkpoint(self, events: list[Event]) -> int:
        """返回最新的 summary_checkpoint 的索引，没有则返回 -1。"""
        idx = -1
        for i, e in enumerate(events):
            if e.kind == "summary_checkpoint":
                idx = i
        return idx

    def _fold_events(self, events: list[Event]) -> list[dict]:
        """将 raw events 折叠为 OpenAI messages 格式。"""
        messages = []
        for e in events:
            if e.kind == "user_msg":
                messages.append({
                    "role": "user",
                    "content": e.payload["text"],
                })
            elif e.kind == "assistant_msg":
                payload = e.payload
                msg = {"role": "assistant", "content": payload.get("content", "")}
                if payload.get("tool_calls"):
                    msg["tool_calls"] = payload["tool_calls"]
                messages.append(msg)
            elif e.kind == "tool_result":
                messages.append({
                    "role": "tool",
                    "tool_call_id": e.payload.get("tool_call_id", ""),
                    "content": e.payload.get("content", ""),
                })
            elif e.kind == "system_note":
                messages.append({
                    "role": "user",
                    "content": e.payload["text"],
                })
        return messages

    # ---------- 工具 ----------

    def count_events(self) -> int:
        """非 checkpoint 事件数量。"""
        with self._lock:
            return sum(1 for e in self.raw_events if e.kind != "summary_checkpoint")

    def empty(self) -> bool:
        """是否没有任何对话事件。"""
        with self._lock:
            return all(e.kind == "summary_checkpoint" for e in self.raw_events)

    def snapshot_messages(self) -> list[dict]:
        """直接返回 raw_events 的完整折叠结果（不压缩），用于调试。"""
        with self._lock:
            events = [e for e in self.raw_events if e.kind != "summary_checkpoint"]
        return self._fold_events(events)
