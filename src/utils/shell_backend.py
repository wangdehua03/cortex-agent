"""跨平台 shell 后端：屏蔽 Linux/macOS/Windows 的 shell 方言与进程管理差异。

对上层（Agent、工具 schema、prompt）只暴露统一接口：
- run()               非交互执行命令，超时抛 subprocess.TimeoutExpired
- run_interactive()   交互式执行（需要伪终端，Windows 暂不支持，返回明确错误）
- 命令分级清单        dangerous / sensitive / safe / risky / interactive（按平台各配一份）

平台选择：get_backend() 按 sys.platform 返回进程级单例。
- Linux / macOS -> PosixBackend（bash + setsid + pty + killpg）
- Windows       -> WindowsBackend（PowerShell + CREATE_NEW_PROCESS_GROUP）
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from typing import Callable, Optional


class ShellBackend(ABC):
    """统一的 shell 执行后端接口。平台差异全部收敛在本模块。"""

    name: str = "abstract"
    shell_note: str = "shell"          # 注入工具描述/prompt 的平台说明
    syntax_guidance: str = "Use syntax appropriate for this shell."
    supports_interactive: bool = False

    # 命令分级清单（子类按平台覆盖）
    dangerous_cmds: frozenset = frozenset()   # 危险命令：明确警告后需用户确认
    sensitive_cmds: frozenset = frozenset()   # 敏感命令：需用户确认
    safe_cmds: frozenset = frozenset()        # 白名单命令：直接放行
    risky_cmds: frozenset = frozenset()       # 高风险文件操作：绝对路径目标必须在 WORKDIR 内
    interactive_cmds: frozenset = frozenset() # 需要交互输入的命令

    def normalize_cmd(self, name: str) -> str:
        """归一化命令名用于清单匹配。解析器已做 basename+lower，POSIX 下无需再处理。"""
        return name

    @abstractmethod
    def run(self, command: str, cwd: str, timeout: Optional[int]) -> subprocess.CompletedProcess:
        """非交互执行命令；timeout 为 None 表示不限时；超时抛 subprocess.TimeoutExpired。"""

    def run_interactive(self, command: str, cwd: str, get_uiq: Callable) -> str:
        """交互式执行（需要伪终端）。默认平台不支持时返回明确错误。"""
        return (f"Error: 当前平台（{self.shell_note}）暂不支持交互式命令，"
                "请改用非交互方式（如 ssh 密钥免密）或在终端手动执行。")


class PosixBackend(ShellBackend):
    """Linux / macOS 后端：bash + setsid + pty + killpg。"""

    name = "posix"
    shell_note = "bash"
    syntax_guidance = "Use bash syntax."
    supports_interactive = True

    def normalize_cmd(self, name: str) -> str:
        """POSIX：去掉路径前缀（解析器产出已做小写处理）。"""
        return name.split('/')[-1]

    dangerous_cmds = frozenset({
        "sudo", "su", "shutdown", "reboot", "halt", "poweroff",
        "mkfs", "fdisk", "dd", "mount", "umount", "insmod", "rmmod",
        "visudo", "at", "systemctl", "service",
        "iptables", "firewall-cmd",
    })

    sensitive_cmds = frozenset({
        # 网络/下载类
        "wget", "pip", "pip3", "npm", "npx",
        # 进程相关
        "kill", "killall",
        # 邮件
        "mail", "mailx", "sendmail", "mutt", "msmtp",
    })

    safe_cmds = frozenset({
        # 文件操作
        "ls", "cat", "head", "tail", "less", "more", "wc", "stat",
        "file", "touch", "mkdir", "cp", "mv", "rm", "rmdir",
        "find", "locate", "which", "whereis", "type",
        # 文本/搜索
        "grep", "sed", "awk", "cut", "sort", "uniq", "diff", "patch",
        "tr", "tee", "xargs", "env", "printenv", "echo", "printf",
        "basename", "dirname", "realpath", "readlink", "du", "df",
        # 压缩归档
        "tar", "gzip", "gunzip", "bzip2", "zip", "unzip",
        "zcat", "bzcat", "base64", "xxd", "od", "uuencode", "uudecode",
        # 编程/工具
        "python", "python3",
        "node", "npx",
        "java", "javac", "jar",
        "gcc", "g++", "make", "cmake",
        "git", "hg", "svn",
        "sqlite3", "kubectl", "helm", "docker", "docker-compose",
        "perl", "ruby", "php", "go", "rustc",
        "jq", "yq", "bc", "expr", "time", "timeout", "seq",
        "test", "[",
        # Shell 解释器与内置命令
        "bash", "sh", "zsh", "dash", "source", ".", "eval",
        "true", "false", "yes", "test", "type", "hash", "help",
        "read", "unset", "local", "pushd", "popd", "dirs",
        "command", "enable", "logout", "trap", "wait", "bg", "fg", "jobs",
        # Shell 关键字/控制结构
        "for", "while", "until", "if", "then", "else", "elif", "fi",
        "case", "esac", "do", "done", "in", "select", "function",
        "return", "exit", "break", "continue", "shift", "exec",
        "local", "declare", "readonly", "typeset", "export",
        # 其他
        "date", "whoami", "id", "pwd", "hostname", "uname", "ip", "ping", "traceroute", "curl",
        "ssh", "scp", "rsync", "nc", "ncat", "netcat", "sftp", "lftp",
        "telnet", "ftp", "socat", "nmap", "arp", "ifconfig",
        "tree", "watch", "yes", "sleep",
        "lsof", "ps", "top", "df", "free", "vmstat", "iostat", "ss", "netstat",
        "who", "w", "last", "lastlog", "id", "crontab",
        "ln", "alias", "unalias", "export", "unset", "set", "shopt",
        "cd",
        # 高风险文件操作（通过 risky_cmds 路径校验限制范围）
        "rm", "mv", "chmod", "chown", "chgrp", "dd",
    })

    risky_cmds = frozenset({"rm", "mv", "chmod", "chown", "chgrp", "dd"})

    interactive_cmds = frozenset({
        "ssh", "scp", "sftp", "ftp", "telnet", "passwd", "sudo", "su",
        "mount", "umount", "visudo", "crontab", "mysql", "psql", "mongosh",
        "docker login", "kubectl", "gpg", "openssl",
    })

    def run(self, command: str, cwd: str, timeout: Optional[int]) -> subprocess.CompletedProcess:
        return subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            preexec_fn=os.setsid,
        )

    def run_interactive(self, command: str, cwd: str, get_uiq: Callable) -> str:
        """使用 pty 运行交互式命令，使子进程能直接获取终端输入。

        执行期间会暂停 UserInputQueue 的 feed_loop，让子进程独占 stdin。
        120s 超时后进入无限等待模式（不 kill），直到子进程退出或 stdin 关闭。
        """
        import pty
        import select
        import signal

        uiq = None
        try:
            uiq = get_uiq()
        except LookupError:
            pass

        master_fd = None
        stdin_fd = None
        if hasattr(sys.stdin, 'fileno'):
            try:
                stdin_fd = sys.stdin.fileno()
            except (AttributeError, OSError):
                stdin_fd = None

        try:
            if uiq is not None:
                uiq.block_for_interactive()

            master_fd, slave_fd = pty.openpty()
            proc = subprocess.Popen(
                command,
                shell=True,
                cwd=cwd,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                env=dict(os.environ),
                preexec_fn=os.setsid,
            )
            os.close(slave_fd)

            output_bytes = []

            def _collect_master() -> str:
                """读取 master 剩余数据并返回完整输出。"""
                try:
                    while True:
                        data = os.read(master_fd, 4096)
                        if not data:
                            break
                        output_bytes.append(data)
                except OSError:
                    pass
                text = b''.join(output_bytes).decode('utf-8', errors='replace')
                return (text[:50000].strip() if text.strip() else "(no output)")

            watch_fds = [master_fd]
            if stdin_fd is not None:
                watch_fds.append(stdin_fd)

            deadline = time.monotonic() + 120
            in_wait = False

            try:
                while True:
                    remaining = deadline - time.monotonic()
                    timeout_arg = min(remaining, 0.5) if not in_wait else None
                    if not in_wait and timeout_arg <= 0:
                        in_wait = True
                        print("\033[33m[Interactive] 120s timeout — "
                              "waiting for command to finish.\033[0m")

                    rlist, _, _ = select.select(watch_fds, [], [], timeout_arg)

                    for fd in rlist:
                        try:
                            if fd == master_fd:
                                data = os.read(master_fd, 4096)
                                if not data:
                                    proc.wait()
                                    return _collect_master()
                                sys.stdout.write(data.decode('utf-8', errors='replace'))
                                sys.stdout.flush()
                                output_bytes.append(data)
                            elif fd == stdin_fd:
                                data = os.read(fd, 4096)
                                if data:
                                    os.write(master_fd, data)
                        except OSError:
                            break

                    if proc.poll() is not None:
                        return _collect_master()

            finally:
                if master_fd is not None:
                    try:
                        os.close(master_fd)
                    except OSError:
                        pass
                if proc.poll() is None:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                    except OSError:
                        proc.terminate()
                    proc.wait()
                if uiq is not None:
                    uiq.unblock_for_interactive()

        except (ImportError, OSError) as e:
            if uiq is not None:
                uiq.unblock_for_interactive()
            return f"Error: Failed to run interactive command: {e}"


class WindowsBackend(ShellBackend):
    """Windows 后端：PowerShell + CREATE_NEW_PROCESS_GROUP。

    说明：
    - 进程组：subprocess 在 Windows 上无 setsid，用 CREATE_NEW_PROCESS_GROUP 创建进程组
    - 超时：subprocess.run 超时会 kill shell 进程本身（子进程树清理待后续用 taskkill /T 完善）
    - 交互式命令：Windows 无伪终端，暂不支持（后续可引入 pywinpty）
    """

    name = "windows"
    shell_note = "Windows PowerShell"
    syntax_guidance = (
        "Use PowerShell syntax, NOT bash. "
        "Avoid '&&', 'export', 'rm -rf', 'grep'. "
        "Use ';' for chaining, '$env:VAR' for environment variables, "
        "'Remove-Item -Recurse -Force' for recursive deletion, "
        "and 'Select-String' for text search."
    )
    supports_interactive = False

    dangerous_cmds = frozenset({
        "format", "diskpart", "bcdedit", "shutdown", "restart-computer",
        "stop-computer", "reg", "regedit", "netsh", "sc", "schtasks",
        "takeown", "wmic", "net", "set-executionpolicy",
    })

    sensitive_cmds = frozenset({
        # 包管理/下载
        "pip", "pip3", "npm", "npx", "winget", "choco", "scoop",
        "invoke-webrequest", "iwr", "invoke-restmethod", "irm", "wget", "bitsadmin", "certutil",
        # 进程相关
        "taskkill", "stop-process", "kill",
    })

    safe_cmds = frozenset({
        # 文件操作（cmdlet + 别名）
        "get-childitem", "ls", "dir", "gci", "get-content", "cat", "type", "gc",
        "copy-item", "copy", "cp", "move-item", "move", "mv",
        "rename-item", "ren", "rename", "new-item", "mkdir", "md", "ni",
        "set-location", "cd", "get-location", "pwd", "push-location", "pop-location",
        "pushd", "popd", "resolve-path", "test-path", "get-item",
        # 文本/搜索
        "echo", "write-output", "write-host", "select-string", "findstr", "sls",
        "select-object", "select", "sort-object", "sort", "measure-object",
        "foreach-object", "where-object", "where", "out-string", "format-table",
        "format-list", "tee-object", "tee", "compare-object", "more", "find",
        # 压缩归档
        "tar", "compress-archive", "expand-archive",
        # 编程/工具
        "python", "python3", "py", "node", "java", "javac",
        "git", "hg", "svn", "dotnet", "go", "rustc", "gcc", "g++", "make", "cmake",
        "jq", "curl",
        # 系统信息/网络
        "get-process", "ps", "get-service", "get-date", "date",
        "whoami", "hostname", "systeminfo", "ver", "set",
        "ping", "tracert", "ipconfig", "nslookup", "ssh", "scp",
        # 其他
        "get-command", "gcm", "get-help", "help", "tree", "get-volume",
        # 高风险文件操作（通过 risky_cmds 路径校验限制范围）
        "remove-item", "rm", "del", "erase", "rmdir", "rd", "clear-content",
        "set-content", "out-file", "attrib", "icacls",
    })

    risky_cmds = frozenset({
        "remove-item", "rm", "del", "erase", "rmdir", "rd", "ri",
        "move-item", "mv", "clear-content", "set-content", "out-file",
        "attrib", "icacls", "rename-item", "ren",
    })

    interactive_cmds = frozenset({
        "ssh", "scp", "sftp", "ftp", "mysql", "psql",
        "docker login", "net", "gpg", "openssl", "runas",
    })

    def normalize_cmd(self, name: str) -> str:
        """Windows：去路径（两种分隔符）、去可执行后缀、小写化。"""
        name = name.replace('/', '\\').rsplit('\\', 1)[-1].lower()
        for suffix in ('.exe', '.bat', '.cmd', '.ps1', '.com'):
            if name.endswith(suffix):
                name = name[:-len(suffix)]
                break
        return name

    def run(self, command: str, cwd: str, timeout: Optional[int]) -> subprocess.CompletedProcess:
        # 强制控制台输出 UTF-8，避免中文 Windows 下 GBK 乱码
        ps_command = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; " + command
        return subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_command],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )


_backend: Optional[ShellBackend] = None


def get_backend() -> ShellBackend:
    """按当前平台返回 shell 后端单例。macOS(darwin) 与 Linux 共用 PosixBackend。"""
    global _backend
    if _backend is None:
        _backend = WindowsBackend() if sys.platform == "win32" else PosixBackend()
    return _backend
