"""
MessageBus - Agent 间消息通信的基础设施

默认使用内存队列实现（单进程多线程）。通过接口抽象，后续可切换为
JSONL 文件、Redis 等其他实现。

协议约定（通过 msg_type + extra 字段实现）：
  - msg_type: 消息类型，如 "message", "broadcast", "subagent_done",
              "shutdown_request", "revision_request"
  - extra: 附加字段，如 {"request_id": "xxx", "subagent_id": "xxx"}
"""

import queue
import select
import sys
import threading
import time
import uuid


# 允许的消息类型，后续可动态扩展
VALID_MSG_TYPES = frozenset({
    "message",
    "broadcast",
    "subagent_done",
    "shutdown_request",
    "revision_request",
    "permission_request",
    "permission_response",
})


class BusMessage:
    """单条消息"""

    def __init__(self, sender: str, to: str, content: str,
                 msg_type: str = "message", extra: dict | None = None):
        self.id = str(uuid.uuid4())[:8]
        self.sender = sender
        self.to = to
        self.content = content
        self.msg_type = msg_type
        self.extra = extra or {}
        self.timestamp = time.time()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "sender": self.sender,
            "to": self.to,
            "content": self.content,
            "type": self.msg_type,
            "extra": self.extra,
            "timestamp": self.timestamp,
        }


class MemoryMessageBus:
    """
    内存队列实现。每个 agent 有独立的 list 作为 inbox，通过 threading.Lock
    保证线程安全。send 是非阻塞操作，read_inbox 是 drain 模式（读取并清空）。

    支持 completion listener：注册回调，当指定 subagent 发送 subagent_done 时自动触发。
    """

    def __init__(self):
        self._inboxes: dict[str, list] = {}
        self._listeners: dict[str, list] = {}
        self._lock = threading.Lock()

    def send(self, sender: str, to: str, content: str,
             msg_type: str = "message", extra: dict | None = None) -> str:
        """
        发送消息到指定 agent。线程安全，非阻塞。

        Args:
            sender: 发送者 agent 名称
            to: 接收者 agent 名称
            content: 消息内容
            msg_type: 消息类型，必须在 VALID_MSG_TYPES 中
            extra: 附加字段（如 request_id, subagent_id）

        Returns: message_id

        Raises:
            ValueError: msg_type 不在允许列表中
        """
        if msg_type not in VALID_MSG_TYPES:
            raise ValueError(
                f"Invalid msg_type '{msg_type}'. Valid types: {sorted(VALID_MSG_TYPES)}"
            )

        msg = BusMessage(sender, to, content, msg_type, extra)
        listeners: list = []
        with self._lock:
            if to not in self._inboxes:
                self._inboxes[to] = []
            self._inboxes[to].append(msg.to_dict())

            # 触发 completion listener 的两种情况：
            # 1. lead 发送 shutdown_request 给 subagent：lead 主动关闭，生命周期结束
            # 2. subagent 发送 subagent_done 给 lead：subagent 自主完成并上报结果，生命周期结束
            if msg_type == "shutdown_request":
                listeners = self._listeners.pop(to, [])
            elif msg_type == "subagent_done":
                listeners = self._listeners.pop(sender, [])
        # 在锁外执行 listener 回调，避免死锁
        for cb in listeners:
            try:
                cb(msg.to_dict())
            except Exception as e:
                print(f"\033[31m[MessageBus] Listener error for {to}: {e}\033[0m")
        return msg.id

    def register_completion_listener(self, subagent_id: str, callback: callable):
        """注册 completion listener：当 subagent 发送 subagent_done 时自动触发。

        Args:
            subagent_id: 要监听的 subagent ID
            callback: 接收 msg dict 的回调函数
        """
        with self._lock:
            self._listeners.setdefault(subagent_id, []).append(callback)

    def unregister_completion_listener(self, subagent_id: str):
        """注销 subagent 的所有 completion listener"""
        with self._lock:
            self._listeners.pop(subagent_id, None)

    def peek_inbox(self, name: str) -> list[dict]:
        """
        只读查看指定 agent 的 inbox，不清空。

        Returns: 消息列表（副本）
        """
        with self._lock:
            return list(self._inboxes.get(name, []))

    def read_inbox(self, name: str) -> list[dict]:
        """
        读取并清空指定 agent 的 inbox（drain 模式）。

        Returns: 消息列表
        """
        with self._lock:
            messages = self._inboxes.get(name, [])
            self._inboxes[name] = []
            return messages

    def broadcast(self, sender: str, content: str,
                  msg_type: str = "broadcast",
                  exclude: str | None = None,
                  targets: list[str] | None = None) -> int:
        """
        广播消息给所有注册的 agent（排除发送者自己）。

        Args:
            targets: 可选，指定目标列表。如果不指定，广播给所有已注册的 inbox。

        Returns: 接收者数量
        """
        with self._lock:
            if targets is None:
                targets = list(self._inboxes.keys())

        count = 0
        for name in targets:
            if name != sender and name != exclude:
                self.send(sender, name, content, msg_type)
                count += 1
        return count

    def register_agent(self, name: str):
        """预注册一个 agent 的 inbox（可选，send 时会自动创建）"""
        with self._lock:
            if name not in self._inboxes:
                self._inboxes[name] = []

    def unregister_agent(self, name: str):
        """注销一个 agent 的 inbox"""
        with self._lock:
            if name in self._inboxes:
                self._inboxes[name].clear()
                del self._inboxes[name]

    def get_registered(self) -> list[str]:
        """获取所有已注册的 agent 名称"""
        with self._lock:
            return list(self._inboxes.keys())


def build_inbox_drain_fn(agent_name: str, on_shutdown=None):
    """
    构建 inbox drain 回调函数，供 _loop_core 的 on_round_start 使用。

    Args:
        agent_name: agent 名称
        on_shutdown: 可选回调，收到 shutdown_request 时调用。

    返回的函数签名：fn(messages: list) -> list[dict] | None
    """
    def on_round_start(messages):
        inbox = bus.read_inbox(agent_name)
        if not inbox:
            return None
        result = []
        for msg in inbox:
            if msg.get("type") == "shutdown_request" and on_shutdown is not None:
                on_shutdown(msg)
            content = msg.get("content", "")
            result.append({
                "role": "user",
                "content": f"<inbox>{content}</inbox>",
            })
        return result if result else None
    return on_round_start


class UserInputQueue:
    """处理终端用户输入的队列，支持非阻塞模式。

    后台线程只负责读取 stdin，不触碰 stdout。prompt 由调用方在
    合适的时机打印，从而与 agent 的流式输出完全不交织。

    当 _agent_running = True 时，用户输入会进入 steer_queue 而非普通
    输入队列，供 agent._loop 的 on_round_start 回调检查，实现 steer/interrupt。

    当 _prompting = True 时（如 bash 权限确认期间），用户输入统一进入
    _queue，供 prompt_and_wait() 消费，确保 y/n 等交互不会跑到 steer_queue。
    """

    def __init__(self):
        self._queue: queue.Queue[str] = queue.Queue()
        self._steer_queue: queue.Queue[str] = queue.Queue()
        self._running = False
        self._thread: threading.Thread | None = None
        self._eof = False
        self._agent_running = False  # agent._loop 是否正在执行
        self._prompting = False      # prompt_and_wait 是否正在等待用户 y/n
        self._interactive_blocked = False  # 交互式命令执行期间，暂停 feed_loop 读取 stdin
        self._stdin_fd = None  # 保存 stdin 的文件描述符，用于交互式命令恢复

    def start(self):
        """启动后台输入线程"""
        if self._running:
            return
        self._running = True
        self._eof = False
        self._thread = threading.Thread(target=self._feed_loop, daemon=True)
        self._thread.start()

    def stop(self):
        """停止后台输入线程"""
        self._running = False

    def get(self, timeout: float | None = None) -> str | None:
        """获取用户输入，支持超时。

        返回 None 表示队列关闭（EOF 或 Ctrl+C）。
        返回空字符串表示超时，无新输入。
        """
        try:
            val = self._queue.get(timeout=timeout)
            if val is _SENTINEL_EOF:
                return None
            return val
        except queue.Empty:
            return ""

    def prompt_and_wait(self, prompt: str = "") -> str | None:
        """在主线程中打印 prompt 并阻塞等待用户输入。

        在等待期间设置 _prompting=True，使后台 feed_loop 将用户输入路由到
        _queue 而非 _steer_queue，这样 agent 运行中的 bash 权限确认等交互
        也能正确拿到用户的 y/n。

        返回 None 表示队列关闭（EOF 或 Ctrl+C）。
        """
        print(prompt, end="", flush=True)
        try:
            self._prompting = True
            val = self._queue.get()  # 阻塞等待用户输入
            if val is _SENTINEL_EOF:
                return None
            return val
        except queue.Empty:
            return ""
        finally:
            self._prompting = False

    def get_steers(self) -> list[str]:
        """Drain 所有 pending steer 消息。"""
        result = []
        while True:
            try:
                result.append(self._steer_queue.get_nowait())
            except queue.Empty:
                break
        return result

    def set_agent_running(self, running: bool):
        """通知 UserInputQueue agent 是否正在运行。

        running=True 时，用户输入进入 steer_queue。
        running=False 时，用户输入进入普通 queue。
        """
        self._agent_running = running

    def block_for_interactive(self):
        """临时暂停 feed_loop 读取 stdin，使交互式命令(ssh/scp 等)能直接获取用户输入。
        
        调用此方法后，feed_loop 不再监控 stdin fd，将终端输入留给子进程。
        配合 unblock_for_interactive() 使用。
        """
        self._interactive_blocked = True

    def unblock_for_interactive(self):
        """恢复 feed_loop 读取 stdin。"""
        self._interactive_blocked = False

    @property
    def eof(self) -> bool:
        return self._eof

    def _feed_loop(self):
        stdin_fd = None
        if hasattr(sys.stdin, 'fileno'):
            try:
                stdin_fd = sys.stdin.fileno()
            except (AttributeError, OSError):
                pass
        # Windows 的 select 只支持套接字，对 stdin fd 调用会抛 WinError 10038，
        # 直接走 readline 阻塞读取路径
        if sys.platform == "win32":
            stdin_fd = None

        try:
            while self._running:
                # 当交互式命令正在执行时，不监控 stdin，让出控制权
                if self._interactive_blocked:
                    time.sleep(0.1)
                    continue
                if stdin_fd is None:
                    # 无法用 select，回退到 readline
                    try:
                        line = sys.stdin.readline()
                        if not line:
                            self._eof = True
                            self._queue.put(_SENTINEL_EOF)
                            break
                        text = line.rstrip("\n\r")
                        if self._prompting:
                            self._queue.put(text)
                        elif self._agent_running:
                            self._steer_queue.put(text)
                        else:
                            self._queue.put(text)
                    except KeyboardInterrupt:
                        self._eof = True
                        self._queue.put(_SENTINEL_EOF)
                        break
                    continue

                # 使用 select 非阻塞等待 stdin
                try:
                    rlist, _, _ = select.select([stdin_fd], [], [], 0.5)
                except (ValueError, OSError):
                    # select 对该 fd 不可用（如某些平台的 stdin），降级为 readline 模式
                    stdin_fd = None
                    continue

                if not rlist:
                    continue

                try:
                    line = sys.stdin.readline()
                    if not line:
                        self._eof = True
                        self._queue.put(_SENTINEL_EOF)
                        break
                    text = line.rstrip("\n\r")
                    if self._prompting:
                        self._queue.put(text)
                    elif self._agent_running:
                        self._steer_queue.put(text)
                    else:
                        self._queue.put(text)
                except KeyboardInterrupt:
                    self._eof = True
                    self._queue.put(_SENTINEL_EOF)
                    break
        finally:
            self._running = False


_SENTINEL_EOF: object = object()


# 全局单例
bus = MemoryMessageBus()
