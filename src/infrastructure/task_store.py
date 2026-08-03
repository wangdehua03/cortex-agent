"""
s07 Task Store - 持久化任务管理（内存实现）

任务数据在当前实现中存于内存，接口通过 Protocol 抽象，后续可切换为
文件存储（.tasks/ JSON）、SQLite、Redis 等实现。

每个 task 字段：
  - id: int，自增
  - subject: str，任务标题
  - description: str，详细描述
  - status: "pending" | "in_progress" | "completed"
  - blockedBy: list[int]，依赖的其他 task id
  - owner: str，负责该任务的 agent（subagent_id 或 "lead"）
  - created_at: str，创建时间 'yyyy-mm-dd HH:MM:SS'
  - completed_at: str | None，完成时间 'yyyy-mm-dd HH:MM:SS'
"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Protocol


class TaskStore(Protocol):
    """任务存储接口"""

    def create(self, subject: str, description: str = "", owner: str = "") -> dict: ...
    def get(self, task_id: int) -> dict: ...
    def update(
        self,
        task_id: int,
        status: str | None = None,
        owner: str | None = None,
        add_blocked_by: list[int] | None = None,
        remove_blocked_by: list[int] | None = None,
    ) -> dict: ...
    def list_all(
        self,
        owner: str | None = None,
        status: str | None = None,
    ) -> list[dict]: ...


VALID_STATUSES = frozenset({"pending", "in_progress", "completed"})


class MemoryTaskManager:
    """内存实现的任务管理器，线程安全"""

    def __init__(self):
        self._tasks: dict[int, dict] = {}
        self._next_id: int = 1
        self._lock = threading.Lock()

    # -- TaskStore interface --

    def _copy_task(self, task: dict) -> dict:
        """Shallow copy with mutable field safety (blockedBy list)"""
        return {**task, "blockedBy": list(task["blockedBy"])}

    def create(self, subject: str, description: str = "", owner: str = "") -> dict:
        with self._lock:
            task = {
                "id": self._next_id,
                "subject": subject,
                "description": description,
                "status": "pending",
                "blockedBy": [],
                "owner": owner,
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "completed_at": None,
            }
            self._tasks[self._next_id] = task
            self._next_id += 1
            return self._copy_task(task)

    def get(self, task_id: int) -> dict:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise ValueError(f"Task {task_id} not found")
            return self._copy_task(task)

    def update(
        self,
        task_id: int,
        status: str | None = None,
        owner: str | None = None,
        add_blocked_by: list[int] | None = None,
        remove_blocked_by: list[int] | None = None,
    ) -> dict:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise ValueError(f"Task {task_id} not found")

            if owner is not None:
                task["owner"] = owner

            if status is not None:
                if status not in VALID_STATUSES:
                    raise ValueError(f"Invalid status: {status}")
                task["status"] = status
                if status == "completed":
                    task["completed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    self._clear_dependency(task_id)

            if add_blocked_by:
                existing = set(task["blockedBy"])
                task["blockedBy"] = sorted(existing | set(add_blocked_by))

            if remove_blocked_by:
                task["blockedBy"] = [
                    tid for tid in task["blockedBy"] if tid not in remove_blocked_by
                ]

            return self._copy_task(task)

    def list_all(
        self,
        owner: str | None = None,
        status: str | None = None,
    ) -> list[dict]:
        with self._lock:
            tasks = sorted(self._tasks.values(), key=lambda t: t["id"])
            if owner is not None:
                tasks = [t for t in tasks if t["owner"] == owner]
            if status is not None:
                tasks = [t for t in tasks if t["status"] == status]
            return [self._copy_task(t) for t in tasks]

    # -- internal --

    def _clear_dependency(self, completed_id: int):
        """Remove completed_id from all other tasks' blockedBy lists.

        NOTE: Caller must hold self._lock. Only called from update()."""
        for task in self._tasks.values():
            if completed_id in task["blockedBy"]:
                task["blockedBy"].remove(completed_id)

    # -- formatting --

    def list_all_formatted(
        self,
        owner: str | None = None,
        status: str | None = None,
    ) -> str:
        tasks = self.list_all(owner=owner, status=status)
        if not tasks:
            return "No tasks."
        marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}
        lines = []
        for t in tasks:
            m = marker.get(t["status"], "[?]")
            blocked = f" (blocked by: {t['blockedBy']})" if t.get("blockedBy") else ""
            owner_info = f" @{t['owner']}" if t.get("owner") else ""
            lines.append(f"{m} #{t['id']}: {t['subject']}{owner_info}{blocked}")
        return "\n".join(lines)


# 全局单例
tasks = MemoryTaskManager()
