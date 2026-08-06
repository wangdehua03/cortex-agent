"""
Conversation - 会话级状态容器。

一个 Conversation 对应一个独立的用户对话上下文，包含：
- context_store: 对话历史存储与视图折叠
- token_tracker: 该对话的 token 统计缓存
- conversation_id: 会话唯一标识
- metadata: 会话级元数据

Agent 不再自己持有 ContextStore，而是处理外部传入的 Conversation。
这样同一个 Agent 实例可以服务多个会话，会话状态与 Agent 解耦。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from src.infrastructure.context_store import ContextStore


class TokenTracker:
    """会话级别的 token 统计缓存。

    记录最近一次 API 返回的精确 prompt_tokens 及其对应的 messages 快照，
    用于在下次调用前更准确地估算当前 prompt 的 token 数。
    属于 Conversation 的对话级状态，不与特定 Agent 实例绑定。
    """

    def __init__(self):
        self._last_api_prompt_tokens: int = 0
        self._last_messages_snapshot: list[dict] = []

    def record(self, prompt_tokens: int, messages: list[dict]) -> None:
        """记录 API 返回的精确 prompt_tokens 与对应 messages 快照。"""
        if prompt_tokens > 0:
            self._last_api_prompt_tokens = prompt_tokens
            self._last_messages_snapshot = [dict(m) for m in messages]

    def invalidate(self) -> None:
        """当 messages 被整体替换（如压缩）时，之前的快照失效。"""
        self._last_api_prompt_tokens = 0
        self._last_messages_snapshot = []

    @property
    def last_prompt_tokens(self) -> int:
        return self._last_api_prompt_tokens

    @property
    def last_messages_snapshot(self) -> list[dict]:
        return self._last_messages_snapshot


@dataclass
class Conversation:
    """一个独立会话的完整上下文容器。

    Args:
        context_store: 对话历史存储，默认自动创建。
        token_tracker: token 统计缓存，默认自动创建。
        session_id: 会话唯一 ID，由外部传入；默认自动生成 UUID。
        user_id: 所属用户 ID，可选，作为 metadata 的一部分管理。
        created_at: 创建时间戳。
        metadata: 会话级自定义元数据（如模型参数、来源等）。
    """

    context_store: ContextStore = field(default_factory=ContextStore)
    token_tracker: TokenTracker = field(default_factory=TokenTracker)
    session_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    user_id: str | None = None
    created_at: float = field(default_factory=time.time)
    metadata: dict = field(default_factory=dict)
