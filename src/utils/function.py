import os
import re
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Union, List, Optional, Callable
import openpyxl, xlrd
from openpyxl.styles import Alignment

sys.path.append(os.path.join(os.path.dirname(__file__), '../'))
from config.config import WORKDIR
from src.utils.shell_backend import get_backend


# 全局用户输入队列注册，供 _ask_permission 使用
_user_input_queue = None


def _get_user_input_queue():
    if _user_input_queue is None:
        raise LookupError("UserInputQueue not registered")
    return _user_input_queue


def _set_user_input_queue(uiq):
    global _user_input_queue
    _user_input_queue = uiq







def _validate_command(
    command: str,
    *,
    on_dangerous_command: Optional[Callable[[str, str], bool]] = None,
) -> Optional[str]:
    """
    校验用户命令是否安全。不安全则返回错误消息字符串，安全返回 None。

    策略：
    1. 敏感命令：不在白名单中的命令，向用户申请权限（若提供回调）
    2. 危险命令：sudo/shutdown/mkfs 等，向用户申请权限并明确警告（若提供回调）
    3. 白名单命令：直接放行
    4. 高风险命令参数校验：rm、chmod、chown、dd 等操作路径必须在工作目录内

    命令分级清单由当前平台的 shell 后端提供（见 shell_backend.py）。
    """
    backend = get_backend()
    dangerous_cmds = backend.dangerous_cmds
    sensitive_cmds = backend.sensitive_cmds
    safe_cmds = backend.safe_cmds

    # -- 解析命令 --
    # 1) 去除注释行，保留所有非空非注释行，拼接为单行处理
    #    同时需要跳过 heredoc 内容（<< 标记到结尾标记之间的行）
    raw_lines = command.strip().splitlines()
    lines = []
    in_heredoc = None  # 当前 heredoc 的结尾标记（如 "EOF"）
    for line in raw_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # 检查是否进入 heredoc：行包含 <<'MARKER' 或 <<"MARKER" 或 << MARKER
        if in_heredoc is None:
            m = re.search(r"<<-?\s*(['\"]?)(\w+)\1\s*(?:$|[|;&<>])", stripped)
            if m:
                in_heredoc = m.group(2)
                lines.append(stripped)
                continue
        else:
            # 在 heredoc 内部，检查是否到达结尾标记
            if stripped == in_heredoc:
                in_heredoc = None
            # heredoc 内容跳过不加入 lines
            continue
        lines.append(stripped)
    if not lines:
        return "Error: Empty command (only comments)"
    first_line = "; ".join(lines)

    # 2) 感知引号地按链操作符分割，避免把引号内的 | && || ; 当作分隔符
    def _skip_quoted_or_grouped(text: str, start: int) -> int:
        """
        从 start 位置开始，跳过引号块或 ( ) 子 shell 分组，返回跳过后的位置。
        用于 split_respecting_quotes 中避免在引号或括号内部拆分。
        """
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
                i += 1  # close quote
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
                        # 括号内还有引号，跳过整个引号块（含闭合引号）
                        quote = text[i]
                        i += 1
                        while i < n and text[i] != quote:
                            if text[i] == '\\' and i + 1 < n:
                                i += 2
                            else:
                                i += 1
                        i += 1  # skip closing quote
                        continue
                    i += 1
                continue
            return i
        return i

    def split_respecting_quotes(text: str) -> list[str]:
        """按 && || ; | |& 分割，但跳过引号和 ( ) 子 shell 分组内的内容。"""
        segments = []
        current = []
        i = 0
        n = len(text)
        while i < n:
            c = text[i]
            # 跳过引号或 ( ) 分组
            if c in ('"', "'") or c == '(':
                end = _skip_quoted_or_grouped(text, i)
                # 把跳过这段内容 append 进去（保持完整性）
                current.extend(text[i:end])
                i = end
                continue
            # 检查链操作符（长匹配优先）
            matched_op = None
            for op in ('|&', '&&', '||'):
                if text[i:i + len(op)] == op:
                    before_ok = (i == 0 or text[i-1].isspace() or current == [])
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
            # ; 分隔（但 \; 是转义的分号，不应分割）
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
            # | 分隔
            if c == '|':
                before_ok = (i == 0 or text[i-1].isspace() or current == [])
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

    def _get_first_token(text: str) -> Optional[str]:
        """用 shlex 提取第一个 token。"""
        text = text.strip()
        if not text:
            return None
        # 跳过前导重定向: 2>, >, <, >>, 2>&1 等
        while text:
            m = re.match(r'\d?[><]\S*\s+', text)
            if m:
                text = text[m.end():]
                continue
            if re.match(r'^&\s+', text):
                text = text[1:].lstrip()
                continue
            break
        if not text:
            return None
        try:
            reader = shlex.shlex(text, posix=True)
            reader.whitespace_split = False
            reader.commenters = ''
            return next(reader, None)
        except ValueError:
            toks = text.split()
            return toks[0] if toks else None

    _CASE_PATTERN_TOKEN_RE = re.compile(r'^[\w\*\?\[\] ]*\)$')

    def _extract_tokens(text: str) -> list[str]:
        """用 shlex 提取所有 tokens。"""
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

    def collect_cmd_bases(text: str, bases: list[str]):
        """递归收集一段文本中的命令名（含 $() 和 backtick 子命令）。"""
        segs = split_respecting_quotes(text)
        for seg in segs:
            seg = seg.strip()
            if not seg:
                continue
            tokens = _extract_tokens(seg)
            if not tokens:
                _collect_subshells(seg, bases)
                continue

            # 跳过前导重定向 token: 2>/dev/null, > file, < in, 2>&1 等
            # shlex 把 2>/dev/null 拆成 ['2', '>', '/', 'dev', '/', 'null']
            idx = 0
            while idx < len(tokens):
                t = tokens[idx]
                if re.match(r'\d?[><]\S*', t) or t == '&':
                    idx += 1
                    continue
                # FD 数字后紧跟 > 或 <：如 2 > ... 或 2 < ...
                if re.match(r'^\d+$', t) and idx + 1 < len(tokens) and tokens[idx + 1] in ('>', '<', '>>', '<<', '&>'):
                    idx += 1
                    continue
                break

            if idx >= len(tokens):
                _collect_subshells(seg, bases)
                continue

            token = tokens[idx]
            tb = os.path.basename(token).lower()

            # Shell 变量赋值检测：shlex 把 VAR=val 拆成 [VAR, '=', val]
            # 识别 VAR '=' value 模式并跳过
            _varname_re = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
            def _try_skip_assignments(start_idx: int) -> int:
                """从 start_idx 开始跳过所有赋值，返回第一个非赋值 token 的索引。"""
                ci = start_idx
                while ci < len(tokens):
                    maybe_var = tokens[ci]
                    if _varname_re.match(maybe_var) and ci + 1 < len(tokens) and tokens[ci + 1] == '=':
                        ci += 2  # skip VAR =
                        while ci < len(tokens):
                            vt = tokens[ci]
                            # 下一个赋值开始了
                            if _varname_re.match(vt) and ci + 1 < len(tokens) and tokens[ci + 1] == '=':
                                break
                            # 分隔符/操作符 → value 结束
                            if vt in ('&&', '||', ';', '|', ')', '>', '<', '>>', '('):
                                break
                            # value 中不应出现 $ 后跟 ( — 那表示 command substitution 开始了
                            # 也不应出现独立的 ($  — 停止消费
                            if vt == '(':
                                break
                            ci += 1
                        continue
                    break
                return ci

            real_idx = _try_skip_assignments(idx)
            if real_idx < len(tokens):
                # 跳过前导重定向后取真正的 token
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
            # 兼容 PowerShell Verb-Noun / apt-get 这类连字符命令名：
            # shlex(whitespace_split=False) 会把 Get-Location 拆成 ['Get', '-', 'Location']
            # 仅当原始文本中该词确实连写（中间无空白）时才拼接，避免把 'ls -la' 误拼成 'ls-la'
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

            # 跳过 shell 关键字
            if tb in {
                "for", "while", "until", "if", "then", "else", "elif", "fi",
                "case", "esac", "do", "done", "in", "select", "function",
                "return", "exit", "break", "continue", "shift", "exec",
                "local", "declare", "readonly", "typeset", "{", "}",
            }:
                _collect_subshells(seg, bases)
                # do/then/else 后面的 token 才是实际命令
                if tb in ("do", "then", "else", "elif"):
                    _skip = _try_skip_assignments(real_idx + 1)
                    ti = _skip
                    while ti < len(tokens):
                        tt = tokens[ti]
                        # 跳过重定向 FD 数字: 2>/dev/null → '2'
                        if re.match(r'^\d+$', tt) and ti + 1 < len(tokens) and tokens[ti + 1] in ('>', '<', '>>', '<<', '&>'):
                            ti += 1
                            continue
                        # 跳过重定向操作符及其目标
                        if tt in ('>', '<', '>>', '<<', '&>', '=', '|', '&') or re.match(r'^\d?[><]', tt):
                            ti += 1
                            # 跳过目标路径（可能由 shlex 拆成多个 fragment: / dev / null）
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
                        # 处理 ( ... ) 子 shell — 找到匹配的 ) 后递归内部，然后继续找下一个命令
                        if tt == '(':
                            depth = 0
                            sub_start = ti
                            while ti < len(tokens):
                                if tokens[ti] == '(':
                                    depth += 1
                                elif tokens[ti] == ')':
                                    depth -= 1
                                    if depth == 0:
                                        inner_tokens = tokens[sub_start + 1:ti]
                                        # 递归解析子 shell 内部的命令
                                        inner_text = ' '.join(tokens[sub_start:ti + 1])
                                        collect_cmd_bases(inner_text, bases)
                                        ti += 1
                                        break
                                ti += 1
                            continue
                        bases.append(ttb)
                        break
                continue

            # 子 shell 分组 ( ... )
            if tb == '(':
                stripped = seg.strip()
                if stripped.startswith('(') and stripped.endswith(')'):
                    inner = stripped[1:-1]
                    collect_cmd_bases(inner, bases)
                _collect_subshells(seg, bases)
                continue

            # case pattern: token 后面紧跟 ) → case pattern
            # shell 中 *) echo → tokens: ['*', ')', 'echo']
            # shell 中 1) echo → tokens: ['1', ')', 'echo']
            # But [ "$var" = "val" ] is a test command, NOT a case pattern
            next_is_paren = (real_idx + 1 < len(tokens) and tokens[real_idx + 1] == ')')
            is_case_pat = (next_is_paren and (
                           token in ('*', '?',) or
                           token.startswith('[') or
                           _CASE_PATTERN_TOKEN_RE.match(token)))
            if is_case_pat:
                # 跳过 pattern 和紧随的 ) ，取后面的 token
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

    def _collect_subshells(text: str, bases: list[str]):
        """从 text 中提取不在引号内的 $(...), `...`, 和 (... ) 子 shell，递归校验。"""
        i = 0
        n = len(text)
        while i < n:
            c = text[i]
            # 跳过引号内内容
            if c in ('"', "'"):
                quote = c
                i += 1
                while i < n and text[i] != quote:
                    if text[i] == '\\' and i + 1 < n:
                        i += 2
                    else:
                        i += 1
                i += 1  # skip closing quote
                continue
            # $(
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
            # (...) subshell group — 前后有空白或边界
            if c == '(':
                before_ok = (i == 0 or text[i-1].isspace() or
                            text[:i].strip() == '' or
                            i > 0 and _is_operator_char(text[i-1]))
                if before_ok:
                    depth = 1
                    start = i
                    j = i + 1
                    while j < n and depth > 0:
                        if text[j] == '(' and (j == 0 or text[j-1].isspace()):
                            depth += 1
                        elif text[j] == ')' and (j + 1 >= n or text[j+1].isspace() or text[j+1] in ('&', ';', '|')):
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
            # backtick
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

    all_cmd_bases = []
    collect_cmd_bases(first_line, all_cmd_bases)

    if not all_cmd_bases:
        return "Error: Empty command"

    cmd_base = backend.normalize_cmd(all_cmd_bases[0])

    # Collect all rejection reasons first, then ask user once (not per-command)
    # Group by category: dangerous, sensitive, not in whitelist
    dangerous_found = []
    sensitive_found = []
    unwhitelisted_found = []
    for cb in all_cmd_bases:
        if not cb:
            continue
        cb = backend.normalize_cmd(cb)
        if cb in dangerous_cmds:
            dangerous_found.append(cb)
        elif cb in sensitive_cmds:
            sensitive_found.append(cb)
        elif cb not in safe_cmds:
            unwhitelisted_found.append(cb)

    # Collect all rejection reasons: command-level + path-level, then ask once.
    # Command-level reasons
    command_reason_parts = []
    if dangerous_found:
        command_reason_parts.append(f"Dangerous command: {';'.join(dict.fromkeys(dangerous_found))}")
    if sensitive_found:
        command_reason_parts.append(f"Sensitive command: {';'.join(dict.fromkeys(sensitive_found))}")
    if unwhitelisted_found:
        command_reason_parts.append(f"Command not in whitelist: {';'.join(dict.fromkeys(unwhitelisted_found))}")

    # Path-level reasons (extract once, check all risky paths)
    path_reason_parts = []
    first_seg = split_respecting_quotes(first_line)[0]
    try:
        seg_tokens = shlex.split(first_seg)
    except ValueError:
        seg_tokens = first_seg.split()

    risky_cmds = backend.risky_cmds
    if cmd_base in risky_cmds:
        # 绝对路径判定：Unix 根路径、相对上级、home、Windows 盘符、UNC
        path_re = re.compile(r'^(/|\.{1,3}/|~|[a-zA-Z]:[\\/]|\\\\)')
        for token in seg_tokens[1:]:
            if token.startswith("-"):
                continue
            expanded = os.path.expanduser(token)
            if path_re.match(token) or os.path.isabs(expanded):
                resolved = os.path.realpath(expanded)
                if not _is_within_workspace(resolved):
                    path_reason_parts.append(f"Path outside workspace: {token}")

    # Deduplicate path reasons
    path_reason_parts = list(dict.fromkeys(path_reason_parts))

    all_reasons = command_reason_parts + path_reason_parts
    if all_reasons:
        combined_reason = "\n".join(f"- {r}" for r in all_reasons)
        if on_dangerous_command and on_dangerous_command(combined_reason, command):
            pass  # user approved
        else:
            return f"Error: {combined_reason} — permission denied"

    return None


def _ask_permission(reason: str, command: str) -> bool:
    """默认的交互式权限申请回调：打印警告，向用户请求确认。

    尝试通过全局注册的用户输入队列获取确认（队列化模式）；
    未注册时回退到直接 input()。
    """
    try:
        _uiq = _get_user_input_queue()
    except LookupError:
        _uiq = None

    for line in reason.split("\n"):
        print(f"\033[33m⚠  {line}\033[0m")
    print(f"   命令: {command}")
    if _uiq is not None:
        ans = _uiq.prompt_and_wait("   允许执行吗? (y/N): ")
        return ans is not None and ans.strip().lower() in ("y", "yes")
    else:
        ans = input("   允许执行吗? (y/N): ")
        return ans.strip().lower() in ("y", "yes")


def _is_interactive_command(command: str) -> bool:
    """判断命令是否需要交互式输入（如密码、确认等）。清单由平台后端提供。"""
    backend = get_backend()
    try:
        tokens = shlex.split(command.strip().splitlines()[0])
        if tokens:
            cmd = backend.normalize_cmd(tokens[0])
            if cmd in backend.interactive_cmds:
                return True
            # Check for compound commands like 'docker login'
            cmd_full = ' '.join(tokens[:2]) if len(tokens) > 1 else cmd
            if cmd_full in backend.interactive_cmds:
                return True
    except (ValueError, IndexError):
        pass

    # 也检查命令中是否包含常见交互式参数模式
    if re.search(r'(ssh|scp|sftp|ftp|telnet)\s+\S+@', command):
        return True
    return False


def run_bash(command: str, permission_callback: Optional[Callable[[str, str], bool]] = None) -> str:
    """Execute a shell command via the platform backend (bash on Linux/macOS,
    PowerShell on Windows).  May raise subprocess.TimeoutExpired if the command
    takes longer than 120s — the caller (_process_single_tool) is responsible
    for deciding whether to promote to background."""
    callback = permission_callback if permission_callback is not None else _ask_permission
    err = _validate_command(command, on_dangerous_command=callback)
    if err:
        return err

    backend = get_backend()
    if _is_interactive_command(command):
        return backend.run_interactive(command, str(WORKDIR), _get_user_input_queue)

    try:
        r = backend.run(command, str(WORKDIR), timeout=120)
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    suffix = ""
    if r.returncode != 0:
        suffix = f"\n(return code: {r.returncode})"
    return (out[:50000] if out else "(no output)") + suffix


def _is_within_workspace(resolved: str) -> bool:
    """判断 resolved 绝对路径是否位于 WORKDIR 内（Windows 下大小写不敏感比较）。"""
    root = os.path.normcase(os.path.realpath(str(WORKDIR)))
    target = os.path.normcase(resolved)
    return target == root or target.startswith(root + os.sep)



def safe_path(p: str) -> Path:
    """防止操作目录逃逸（Windows 下大小写不敏感比较）"""
    root = WORKDIR.resolve()
    path = (root / p).resolve()
    root_s = os.path.normcase(str(root))
    path_s = os.path.normcase(str(path))
    if path_s != root_s and not path_s.startswith(root_s + os.sep):
        raise ValueError(f"Path escapes workspace: {p}")
    return path

def run_read(path: str, limit: int = None) -> str:
    try:
        text = safe_path(path).read_text()
        lines = text.splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"...({len(lines) - limit} more lines)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"Error: {e}"
    
def run_write(path: str, content: str) -> str:
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"
    

def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        fp = safe_path(path)
        content = fp.read_text()
        if old_text not in content:
            return f"Error: Text not found in {path}"
        fp.write_text(content.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


def _escape_md_cell(value: str) -> str:
    """
    处理excel单元格中的特殊字符：
    1、换行符 -> <br> 标签
    2、管道符 | -> 转义为 \|
    3、首尾空格保留（用HTML实体或不换行空格）
    """
    if value is None:
        return ""
    
    # 转换为字符串
    text = str(value)
    text = text.replace("|","\\|")
    text = text.replace("\n","<br>")
    text = text.replace("\r\n","<br>")
    text = text.replace("\r","<br>")

    return text

    
def read_excel_to_md(file_path: Union[str, Path]) -> str:
    """
    读取 xls 或 xlsx 文件， 返回 Markdown 表格格式
    Args:
        file_path (Union[str, Path]): Excel 文件路径

    Returns:
        str: Markdown 格式的表格字符串
    """
    file_path = Path(file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"文件不存在: {file_path}")
    
    suffix = file_path.suffix.lower()

    if suffix == '.xlsx':
        wb = openpyxl.load_workbook(file_path, data_only=True)
        sheet = wb.active
        rows = []
        max_col = sheet.max_column
        for row in sheet.iter_rows(min_row=1, max_row=sheet.max_row,
                                   min_col=1, max_col=max_col,
                                   values_only=False):
            row_data = []
            for cell in row:
                # 获取单元格值， 处理合并单元格
                value = cell.value if cell.value is not None else ""
                row_data.append(value)
            rows.append(row_data)

    elif suffix == '.xls':
        wb = xlrd.open_workbook(file_path, formatting_info=True)
        sheet = wb.sheet_by_index(0)

        rows = []
        for row_idx in range(sheet.nrows):
            row = []
            for col_idx in range(sheet.ncols):
                cell = sheet.cell(row_idx, col_idx)
                value = cell.value if cell.value is not None else ""
                row.append(value)
            rows.append(row)
    else:
        raise ValueError(f"不支持的文件格式：{suffix}, 仅支持 .xls 和 .xlsx")
    
    if not rows:
        return ""
    
    # 转换为md表格
    md_lines = []
    # 表头
    header = [_escape_md_cell(cell) for cell in rows[0]]
    md_lines.append("|" + "|".join(header) + "|")
    # 分隔符
    md_lines.append("|"+"|".join(["---"] * len(header)) + "|")
    # 数据行
    for row in rows[1:]:
        # 补齐列数（防止某些行列数不足）
        while len(row) < len(header):
            row.append("")

        cells = [_escape_md_cell(cell) for cell in row[:len(header)]]
        md_lines.append("|" + "|".join(cells) + "|")

    return "\n".join(md_lines)


def _parse_md_table(md_content: str) -> List[List[str]]:
    """
    解析markdown表格，处理<br>标签还原为换行符
    """
    lines = [line.rstrip() for line in md_content.strip().split('\n') if line.strip()]
    rows = []
    for line in lines:
        # 跳过分隔行
        # 检查是否全是分隔符和空格
        stripped = line.replace("|","").replace("-","").replace(" ","").replace(":","")
        if not stripped:
            continue

        # 分割单元格
        # 处理转义的管道符 \|, 先替换为临时标记
        temp_line = line.replace('\\|','<<PIPE>>')
        # 按 | 分割， 过滤首尾空字符串
        cells = [cell.strip() for cell in temp_line.split('|') if cell.strip() != '']
        # 还原管道符和换行
        cells = [cell.replace('<<PIPE>>','|').replace('<br>','\n') for cell in cells]

        if cells:
            rows.append(cells)

    return rows

def write_md_to_excel(file_path: Union[str, Path], md_content: str) -> None:
    """
    将 Markdown 表格写入xlsx文件
    支持<br>标签还原为单元格内换行
    """
    file_path = Path(file_path)
    suffix = file_path.suffix.lower()
    rows = _parse_md_table(md_content)

    if not rows:
        raise ValueError("Markdown 内容为空或格式不正确")
    
    if suffix == '.xlsx':
        wb = openpyxl.Workbook()
        ws = wb.active

        for row_idx, row in enumerate(rows, 1):
            for col_idx, value in enumerate(row, 1):
                cell = ws.cell(row=row_idx, column=col_idx)
                cell.value = value
                # 如果内容包含换行， 设置自动换行对齐
                if isinstance(cell.value, str) and '\n' in cell.value:
                    cell.alignment = Alignment(wrap_text=True)

        wb.save(file_path)
    else:
        raise ValueError(f"不支持的文件格式：{suffix}")
    

        


if __name__ == "__main__":
    file_path = "/home/dev2/PyProject/wangdehua/data/智能客服测试结果.xlsx"
    output_path = "/home/dev2/PyProject/wangdehua/data/test.xlsx"
    write_md_to_excel(output_path, read_excel_to_md(file_path))