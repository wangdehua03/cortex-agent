"""
Agent 日志工具：每个 agent 独立写日志文件，互不干扰。
"""

import sys
import threading
from datetime import datetime
from pathlib import Path


class AgentLogger:
    """
    独立的 agent 日志写入器。不依赖 sys.stdout 重定向，
    直接写文件，避免多 agent 并发时的 stdout 竞争问题。

    Args:
        tag: 日志前缀标签（如 "Lead" 或 subagent_id）
        log_file: 日志文件路径
        thread_safe: 是否需要线程锁（sub agent 在独立 thread 中运行，需要锁）
    """

    def __init__(self, tag: str, log_file: Path, thread_safe: bool = False):
        self._tag = tag
        self._log_file = Path(log_file)
        self._log_file.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self._log_file, "a", encoding="utf-8")
        self._closed = False
        self._lock = threading.Lock() if thread_safe else None

    def log(self, *args):
        """写入一行日志，带时间戳和标签前缀"""
        prefix = f"[{datetime.now().strftime('%H:%M:%S')}] [{self._tag}]"
        line = f"{prefix} {' '.join(str(a) for a in args)}\n"
        if self._lock:
            with self._lock:
                if not self._closed:
                    self._file.write(line)
                    self._file.flush()
        else:
            if not self._closed:
                self._file.write(line)
                self._file.flush()

    def close(self):
        if self._lock:
            with self._lock:
                self._do_close()
        else:
            self._do_close()

    def _do_close(self):
        if not self._closed:
            self._closed = True
            self._file.flush()
            self._file.close()

    @staticmethod
    def print_to_terminal(*args, **kwargs):
        """直接打印到原始终端 stdout"""
        print(*args, file=sys.__stdout__, **kwargs)


# Backwards-compatible aliases so existing imports don't break
SubAgentLogger = AgentLogger
LeadAgentLogger = AgentLogger
