"""
SessionManager - 会话管理器。

负责按 session_id 创建、查找和销毁 Conversation。
一个 user 可以拥有多个 session，session_id 是主键，user_id 作为 Conversation 的 metadata 保留。
Conversation 的创建工厂由调用方提供，SessionManager 本身不感知 Agent 类型或 milestone extractors。
"""

from __future__ import annotations

import uuid

from src.infrastructure.conversation import Conversation


class SessionManager:
    """简单的内存会话管理器。

    Args:
        conversation_factory: 创建新 Conversation 的可调用对象。
            接收 session_id 参数，返回 Conversation 实例。默认创建一个空 Conversation。
    """

    def __init__(self, conversation_factory=None):
        self._sessions: dict[str, Conversation] = {}
        self._conversation_factory = conversation_factory or (lambda session_id: Conversation(session_id=session_id))

    def create(self, user_id: str) -> Conversation:
        """为指定用户创建一个新的 session，返回 Conversation。"""
        session_id = str(uuid.uuid4())[:8]
        conversation = self._conversation_factory(session_id)
        conversation.session_id = session_id
        conversation.user_id = user_id
        self._sessions[session_id] = conversation
        return conversation

    def get(self, session_id: str) -> Conversation | None:
        """按 session_id 获取已存在的 Conversation，不存在则返回 None。"""
        return self._sessions.get(session_id)

    def remove(self, session_id: str) -> None:
        """按 session_id 移除会话。"""
        self._sessions.pop(session_id, None)

    def list_by_user(self, user_id: str) -> list[Conversation]:
        """列出指定用户的所有会话。"""
        return [
            conv for conv in self._sessions.values()
            if conv.user_id == user_id
        ]

    def list_session_ids(self) -> list[str]:
        """列出所有已存在的 session_id。"""
        return list(self._sessions.keys())
