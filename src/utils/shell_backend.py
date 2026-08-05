"""跨平台 shell 后端：屏蔽 Linux/macOS/Windows 的 shell 方言与进程管理差异。

对上层（Agent、工具 schema、prompt）只暴露统一接口：
- run()               非交互执行命令，超时抛 subprocess.TimeoutExpired
- run_interactive()   交互式执行（需要伪终端，Windows 暂不支持，返回明确错误）
- parse_command()     解析命令字符串，提取所有命令名与首个命令段 tokens
- 命令分级清单        dangerous / sensitive / safe / risky / interactive（按平台各配一份）

平台选择：get_backend() 按 sys.platform 返回进程级单例。
- Linux / macOS -> PosixBackend（bash + setsid + pty + killpg）
- Windows       -> WindowsBackend（PowerShell + CREATE_NEW_PROCESS_GROUP）
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, List, Optional


@dataclass
class CommandParseResult:
    """命令解析结果：所有需要校验的命令名 + 首个命令段的 tokens（用于路径沙箱校验）。"""
    command_names: List[str]
    first_segment_tokens: List[str]


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
    def parse_command(self, command: str) -> CommandParseResult:
        """解析命令字符串，返回所有需要校验的命令名以及首个命令段的 tokens。

        平台差异（bash 控制流、PowerShell Verb-Noun/参数/反斜杠路径等）
        由各后端自行实现，function.py 只负责按返回结果做统一分类校验。
        """

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

    # ------------------------------------------------------------------
    # parse_command implementation (moved from function.py)
    # ------------------------------------------------------------------

    def parse_command(self, command: str) -> CommandParseResult:
        """POSIX/bash 命令解析器：提取所有命令名（含子 shell）以及首个命令段 tokens。"""

        # -- 1) 去除注释行，跳过 heredoc 内容 --
        raw_lines = command.strip().splitlines()
        lines: list[str] = []
        in_heredoc: str | None = None
        for line in raw_lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if in_heredoc is None:
                m = re.search(r"<<-?\s*(['\"]?)\w+\1\s*(?:$|[|;&<>])", stripped)
                if m:
                    in_heredoc = m.group(2)
                    lines.append(stripped)
                    continue
            else:
                if stripped == in_heredoc:
                    in_heredoc = None
                continue
            lines.append(stripped)
        if not lines:
            return CommandParseResult(command_names=[], first_segment_tokens=[])
        first_line = "; ".join(lines)

        # -- helpers --
        def _skip_quoted_or_grouped(text: str, start: int) -> int:
            n = len(text)
            i = start
            while i < n:
                c = text[i]
                if c in ('"', "'"):
                    quote = c
                    i += 1
                    while i < n and text[i] != quote:
                        if text[i] == '\\' and i + 1 < n:
                            i += 2
                        else:
                            i += 1
                    i += 1
                    continue
                if c == '(':
                    depth = 1
                    i += 1
                    while i < n and depth > 0:
                        if text[i] == '(':
                            depth += 1
                        elif text[i] == ')':
                            depth -= 1
                        elif text[i] in ('"', "'"):
                            quote = text[i]
                            i += 1
                            while i < n and text[i] != quote:
                                if text[i] == '\\' and i + 1 < n:
                                    i += 2
                                else:
                                    i += 1
                            i += 1
                            continue
                        i += 1
                    continue
                return i
            return i

        def split_respecting_quotes(text: str) -> list[str]:
            """按 && || ; | |& 分割，但跳过引号和 ( ) 子 shell 分组内的内容。"""
            segments: list[str] = []
            current: list[str] = []
            i = 0
            n = len(text)
            while i < n:
                c = text[i]
                if c in ('"', "'") or c == '(':
                    end = _skip_quoted_or_grouped(text, i)
                    current.extend(text[i:end])
                    i = end
                    continue
                matched_op = None
                for op in ('|&', '&&', '||'):
                    if text[i:i + len(op)] == op:
                        before_ok = (i == 0 or text[i - 1].isspace() or current == [])
                        j = i + len(op)
                        after_ok = (j >= n or text[j].isspace())
                        if before_ok and after_ok:
                            matched_op = op
                            break
                if matched_op:
                    segments.append(''.join(current))
                    current = []
                    i += len(matched_op)
                    continue
                if c == ';':
                    escaped = (i > 0 and text[i - 1] == '\\')
                    if i + 1 < n and text[i + 1] == ';':
                        current.append(c)
                        i += 1
                        continue
                    if not escaped:
                        segments.append(''.join(current))
                        current = []
                    i += 1
                    continue
                if c == '|':
                    before_ok = (i == 0 or text[i - 1].isspace() or current == [])
                    if i + 1 < n and text[i + 1] == '|':
                        current.append(c)
                        i += 1
                        continue
                    after_ok = (i + 1 >= n or text[i + 1].isspace())
                    if before_ok and after_ok:
                        segments.append(''.join(current))
                        current = []
                        i += 1
                        continue
                    else:
                        current.append(c)
                        i += 1
                        continue
                current.append(c)
                i += 1
            segments.append(''.join(current))
            return segments

        _CASE_PATTERN_TOKEN_RE = re.compile(r'^[\w\*\?\[\] ]*\)$')

        def _extract_tokens(text: str) -> list[str]:
            text = text.strip()
            if not text:
                return []
            try:
                reader = shlex.shlex(text, posix=True)
                reader.whitespace_split = False
                reader.commenters = ''
                return list(reader)
            except ValueError:
                return text.split()

        def collect_cmd_bases(text: str, bases: list[str]) -> None:
            segs = split_respecting_quotes(text)
            for seg in segs:
                seg = seg.strip()
                if not seg:
                    continue
                tokens = _extract_tokens(seg)
                if not tokens:
                    _collect_subshells(seg, bases)
                    continue

                idx = 0
                while idx < len(tokens):
                    t = tokens[idx]
                    if re.match(r'\d?[><]\S*', t) or t == '&':
                        idx += 1
                        continue
                    if re.match(r'^\d+$', t) and idx + 1 < len(tokens) and tokens[idx + 1] in ('>', '<', '>>', '<<', '&>'):
                        idx += 1
                        continue
                    break

                if idx >= len(tokens):
                    _collect_subshells(seg, bases)
                    continue

                token = tokens[idx]
                tb = os.path.basename(token).lower()

                _varname_re = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')

                def _try_skip_assignments(start_idx: int) -> int:
                    ci = start_idx
                    while ci < len(tokens):
                        maybe_var = tokens[ci]
                        if _varname_re.match(maybe_var) and ci + 1 < len(tokens) and tokens[ci + 1] == '=':
                            ci += 2
                            while ci < len(tokens):
                                vt = tokens[ci]
                                if _varname_re.match(vt) and ci + 1 < len(tokens) and tokens[ci + 1] == '=':
                                    break
                                if vt in ('&&', '||', ';', '|', ')', '>', '<', '>>', '('):
                                    break
                                if vt == '(':
                                    break
                                ci += 1
                            continue
                        break
                    return ci

                real_idx = _try_skip_assignments(idx)
                if real_idx < len(tokens):
                    while real_idx < len(tokens):
                        maybe_t = tokens[real_idx]
                        if re.match(r'\d?[><]\S*', maybe_t) or maybe_t == '&':
                            real_idx += 1
                            continue
                        break
                if real_idx >= len(tokens):
                    _collect_subshells(seg, bases)
                    continue

                token = tokens[real_idx]
                word = token
                j = real_idx + 1
                while j + 1 < len(tokens) and tokens[j] == '-':
                    candidate = word + '-' + tokens[j + 1]
                    if candidate in seg:
                        word = candidate
                        j += 2
                    else:
                        break
                token = word
                tb = os.path.basename(token).lower()

                if tb in {
                    "for", "while", "until", "if", "then", "else", "elif", "fi",
                    "case", "esac", "do", "done", "in", "select", "function",
                    "return", "exit", "break", "continue", "shift", "exec",
                    "local", "declare", "readonly", "typeset", "{", "}",
                }:
                    _collect_subshells(seg, bases)
                    if tb in ("do", "then", "else", "elif"):
                        _skip = _try_skip_assignments(real_idx + 1)
                        ti = _skip
                        while ti < len(tokens):
                            tt = tokens[ti]
                            if re.match(r'^\d+$', tt) and ti + 1 < len(tokens) and tokens[ti + 1] in ('>', '<', '>>', '<<', '&>'):
                                ti += 1
                                continue
                            if tt in ('>', '<', '>>', '<<', '&>', '=', '|', '&') or re.match(r'^\d?[><]', tt):
                                ti += 1
                                while ti < len(tokens) and re.match(r'^[/\w\$\{\}\.\-\*~]', tokens[ti]):
                                    if tokens[ti] in {'do', 'done', 'then', 'else', 'fi', 'esac', ';'}:
                                        break
                                    ti += 1
                                continue
                            ttb = os.path.basename(tt).lower()
                            if ttb in {"do", "then", "else", "elif", "done", "fi", "esac",
                                       "for", "while", "until", "if", "case", "}", "{"}:
                                ti += 1
                                continue
                            if tt == '(':
                                depth = 0
                                sub_start = ti
                                while ti < len(tokens):
                                    if tokens[ti] == '(':
                                        depth += 1
                                    elif tokens[ti] == ')':
                                        depth -= 1
                                        if depth == 0:
                                            inner_text = ' '.join(tokens[sub_start:ti + 1])
                                            collect_cmd_bases(inner_text, bases)
                                            ti += 1
                                            break
                                    ti += 1
                                continue
                            bases.append(ttb)
                            break
                    continue

                if tb == '(':
                    stripped = seg.strip()
                    if stripped.startswith('(') and stripped.endswith(')'):
                        inner = stripped[1:-1]
                        collect_cmd_bases(inner, bases)
                    _collect_subshells(seg, bases)
                    continue

                next_is_paren = (real_idx + 1 < len(tokens) and tokens[real_idx + 1] == ')')
                is_case_pat = (next_is_paren and (
                               token in ('*', '?',) or
                               token.startswith('[') or
                               _CASE_PATTERN_TOKEN_RE.match(token)))
                if is_case_pat:
                    j = real_idx + 1
                    if j < len(tokens) and tokens[j] == ')':
                        j += 1
                    if j < len(tokens):
                        sub = os.path.basename(tokens[j]).lower()
                        bases.append(sub)
                    _collect_subshells(seg, bases)
                    continue

                bases.append(tb)
                _collect_subshells(seg, bases)

        def _is_operator_char(ch: str) -> bool:
            return ch in ('&', ';', '|', '(', ')', '<', '>')

        def _collect_subshells(text: str, bases: list[str]) -> None:
            i = 0
            n = len(text)
            while i < n:
                c = text[i]
                if c in ('"', "'"):
                    quote = c
                    i += 1
                    while i < n and text[i] != quote:
                        if text[i] == '\\' and i + 1 < n:
                            i += 2
                        else:
                            i += 1
                    i += 1
                    continue
                if text[i:i + 2] == '$(':
                    depth = 0
                    start = i
                    j = i
                    while j < n:
                        if text[j:j + 2] == '$(':
                            depth += 1
                            j += 2
                        elif text[j] == ')':
                            depth -= 1
                            if depth == 0:
                                inner = text[start + 2:j]
                                collect_cmd_bases(inner, bases)
                                break
                            j += 1
                        else:
                            j += 1
                    i = j + 1 if j < n else j
                    continue
                if c == '(':
                    before_ok = (i == 0 or text[i - 1].isspace() or
                                 text[:i].strip() == '' or
                                 (i > 0 and _is_operator_char(text[i - 1])))
                    if before_ok:
                        depth = 1
                        start = i
                        j = i + 1
                        while j < n and depth > 0:
                            if text[j] == '(' and (j == 0 or text[j - 1].isspace()):
                                depth += 1
                            elif text[j] == ')' and (j + 1 >= n or text[j + 1].isspace() or text[j + 1] in ('&', ';', '|')):
                                depth -= 1
                                if depth == 0:
                                    inner = text[start + 1:j]
                                    collect_cmd_bases(inner, bases)
                                    break
                            elif text[j] in ('"', "'"):
                                q = text[j]
                                j += 1
                                while j < n and text[j] != q:
                                    if text[j] == '\\' and j + 1 < n:
                                        j += 2
                                    else:
                                        j += 1
                            j += 1
                        i = j + 1 if j < n else j
                        continue
                if c == '`':
                    j = i + 1
                    while j < n and text[j] != '`':
                        if text[j] == '\\' and j + 1 < n:
                            j += 2
                        else:
                            j += 1
                    if j < n:
                        inner = text[i + 1:j]
                        collect_cmd_bases(inner, bases)
                        i = j + 1
                        continue
                i += 1

        all_cmd_bases: list[str] = []
        collect_cmd_bases(first_line, all_cmd_bases)

        # first segment tokens for risky-path check
        first_seg = split_respecting_quotes(first_line)[0] if first_line else ""
        try:
            seg_tokens = shlex.split(first_seg)
        except ValueError:
            seg_tokens = first_seg.split()

        return CommandParseResult(
            command_names=all_cmd_bases,
            first_segment_tokens=seg_tokens,
        )


class WindowsBackend(ShellBackend):
    """Windows 后端：PowerShell + CREATE_NEW_PROCESS_GROUP。

    说明：
    - 进程组：subprocess 在 Windows 上无 setsid，用 CREATE_NEW_PROCESS_GROUP 创建进程组
    - 超时：subprocess.run 超时会 kill shell 进程本身（子进程树清理待后续用 taskkill /T 完善）
    - 交互式命令：Windows 无伪终端，暂不支持（后续可引入 pywinpty）
    - parse_command：提供 PowerShell 语法解析，支持 Verb-Noun cmdlet 拼接、$() 子表达式、反斜杠路径保留
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

    # ------------------------------------------------------------------
    # parse_command implementation (PowerShell-aware)
    # ------------------------------------------------------------------

    def parse_command(self, command: str) -> CommandParseResult:
        """PowerShell 命令解析器：提取所有命令名（含 $() 子表达式）以及首个命令段 tokens。

        支持：
        - 按管道 | 和分号 ; 分割命令段（PowerShell 5.1 不支持 &&/||）
        - 单/双引号及反引号 `` ` `` 转义
        - Verb-Noun cmdlet 拼接（如 Get-ChildItem）
        - $() 子表达式递归提取
        - 保留反斜杠 Windows 路径
        """

        def _tokenize_ps(text: str) -> list[str]:
            """PowerShell 简单 tokenizer：尊重引号与反引号转义。"""
            tokens: list[str] = []
            i = 0
            n = len(text)
            while i < n:
                # 跳过空白
                while i < n and text[i].isspace():
                    i += 1
                if i >= n:
                    break
                c = text[i]
                # 单引号字符串
                if c == "'":
                    j = i + 1
                    while j < n and text[j] != "'":
                        j += 1
                    tokens.append(text[i:j + 1])
                    i = j + 1
                    continue
                # 双引号字符串（反引号转义）
                if c == '"':
                    j = i + 1
                    while j < n:
                        if text[j] == '`' and j + 1 < n:
                            j += 2
                        elif text[j] == '"':
                            break
                        else:
                            j += 1
                    tokens.append(text[i:j + 1])
                    i = j + 1
                    continue
                # 普通 token：到下一个空白或特殊字符
                j = i
                while j < n and not text[j].isspace():
                    if text[j] in ('|', ';', '(', ')', '{', '}', '&', '$'):
                        # 作为独立 token 或保留在 token 内（如 $( ）
                        if j == i:
                            j += 1
                        break
                    j += 1
                if j > i:
                    tokens.append(text[i:j])
                i = j
            return tokens

        def _reconstruct_cmdlets(tokens: list[str]) -> list[str]:
            """将 shlex-like 拆分后断裂的 Verb-Noun 重新拼接，如 ['Get', '-', 'ChildItem'] -> ['Get-ChildItem']。"""
            out: list[str] = []
            i = 0
            while i < len(tokens):
                t = tokens[i]
                # 检测 Verb-Noun 模式：Word - Word
                if i + 2 < len(tokens) and tokens[i + 1] == '-' and tokens[i + 2].isalpha():
                    combined = t + '-' + tokens[i + 2]
                    out.append(combined)
                    i += 3
                    continue
                out.append(t)
                i += 1
            return out

        def _extract_commands_from_tokens(tokens: list[str]) -> list[str]:
            """从 token 列表中提取命令名（首个非参数/非变量 token）。"""
            names: list[str] = []
            skip_next = False
            for idx, t in enumerate(tokens):
                if skip_next:
                    skip_next = False
                    continue
                # 变量 / 参数 / 操作符
                if t.startswith('$') or t.startswith('-') or t in ('|', ';', '(', ')', '{', '}', '&'):
                    continue
                # Hashtable 键名 / 属性赋值，如 @{Name='...'; Expression={...}} 中的 Name= / Expression=
                if t.endswith('=') or t == '@':
                    continue
                # 处理 $( ... ) 子表达式
                if t == '$(':
                    # 找到匹配的 )
                    depth = 1
                    sub_tokens: list[str] = []
                    j = idx + 1
                    while j < len(tokens) and depth > 0:
                        if tokens[j] == '$(':
                            depth += 1
                        elif tokens[j] == ')':
                            depth -= 1
                            if depth == 0:
                                break
                        if depth > 0:
                            sub_tokens.append(tokens[j])
                        j += 1
                    skip_next = True  # 跳过后面的 )
                    names.extend(_extract_commands_from_tokens(sub_tokens))
                    continue
                # 首个非特殊 token 视为命令名
                names.append(self.normalize_cmd(t))
                # 只取第一个命令名后停止（单段内）
                break
            return names

        def _collect_all_commands(text: str) -> list[str]:
            """递归收集文本中所有命令名（含子表达式）。"""
            all_names: list[str] = []
            # 先提取本层
            tokens = _tokenize_ps(text)
            tokens = _reconstruct_cmdlets(tokens)
            names = _extract_commands_from_tokens(tokens)
            all_names.extend(names)
            # 递归处理 $( ... ) 子表达式（已经由 _extract_commands_from_tokens 处理，
            # 但这里再扫描一遍纯文本以防遗漏未 tokenize 的嵌套）
            i = 0
            n = len(text)
            while i < n:
                if text[i] == '$' and i + 1 < n and text[i + 1] == '(':
                    depth = 1
                    start = i + 2
                    j = start
                    while j < n and depth > 0:
                        if text[j] == '$' and j + 1 < n and text[j + 1] == '(':
                            depth += 1
                            j += 2
                        elif text[j] == ')':
                            depth -= 1
                            if depth == 0:
                                inner = text[start:j]
                                all_names.extend(_collect_all_commands(inner))
                                break
                            j += 1
                        else:
                            j += 1
                    i = j + 1 if j < n else j
                    continue
                i += 1
            return all_names

        # 分割命令段：按管道 | 和分号 ;（跳过引号、圆括号、花括号内的分隔符）
        def _split_segments(text: str) -> list[str]:
            segments: list[str] = []
            current: list[str] = []
            i = 0
            n = len(text)
            while i < n:
                c = text[i]
                # 跳过引号块
                if c == "'":
                    j = i + 1
                    while j < n and text[j] != "'":
                        j += 1
                    current.extend(text[i:j + 1])
                    i = j + 1
                    continue
                if c == '"':
                    j = i + 1
                    while j < n:
                        if text[j] == '`' and j + 1 < n:
                            j += 2
                        elif text[j] == '"':
                            break
                        else:
                            j += 1
                    current.extend(text[i:j + 1])
                    i = j + 1
                    continue
                # 跳过 (...) 和 {...} 分组，避免 hashtable/scriptblock 内部的 ; 被误判为段边界
                if c in ('(', '{'):
                    depth = 1
                    close = ')' if c == '(' else '}'
                    current.append(c)
                    i += 1
                    while i < n and depth > 0:
                        ch = text[i]
                        if ch == "'":
                            j = i + 1
                            while j < n and text[j] != "'":
                                j += 1
                            current.extend(text[i:j + 1])
                            i = j + 1
                            continue
                        if ch == '"':
                            j = i + 1
                            while j < n:
                                if text[j] == '`' and j + 1 < n:
                                    j += 2
                                elif text[j] == '"':
                                    break
                                else:
                                    j += 1
                            current.extend(text[i:j + 1])
                            i = j + 1
                            continue
                        if ch == c:
                            depth += 1
                        elif ch == close:
                            depth -= 1
                        current.append(ch)
                        i += 1
                    continue
                if c == '|':
                    segments.append(''.join(current))
                    current = []
                    i += 1
                    continue
                if c == ';':
                    segments.append(''.join(current))
                    current = []
                    i += 1
                    continue
                current.append(c)
                i += 1
            segments.append(''.join(current))
            return [s.strip() for s in segments if s.strip()]

        raw = command.strip()
        if not raw:
            return CommandParseResult(command_names=[], first_segment_tokens=[])

        segments = _split_segments(raw)
        all_names: list[str] = []
        for seg in segments:
            all_names.extend(_collect_all_commands(seg))

        # 去重并保持顺序
        seen: set[str] = set()
        deduped: list[str] = []
        for name in all_names:
            if name and name not in seen:
                seen.add(name)
                deduped.append(name)

        first_seg_tokens: list[str] = []
        if segments:
            first_seg_tokens = _reconstruct_cmdlets(_tokenize_ps(segments[0]))

        return CommandParseResult(
            command_names=deduped,
            first_segment_tokens=first_seg_tokens,
        )


_backend: Optional[ShellBackend] = None


def get_backend() -> ShellBackend:
    """按当前平台返回 shell 后端单例。macOS(darwin) 与 Linux 共用 PosixBackend。"""
    global _backend
    if _backend is None:
        _backend = WindowsBackend() if sys.platform == "win32" else PosixBackend()
    return _backend
