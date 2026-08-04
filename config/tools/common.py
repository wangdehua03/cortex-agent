"""公共工具：所有 Agent 模式共享的基础工具定义"""

from src.utils.function import *
from src.utils.managers import *
from src.utils.shell_backend import get_backend
from config.config import APP_ROOT

skill_loader = SkillLoader(skills_dir=APP_ROOT.joinpath('skills'))


def _validate_and_call(handler, required_params, **kw):
    """Validate required parameters before calling the handler"""
    missing = [p for p in required_params if p not in kw or kw[p] is None or (isinstance(kw[p], str) and not kw[p].strip())]
    if missing:
        raise KeyError(f"Missing required parameter(s): {', '.join(missing)}")
    import inspect
    sig = inspect.signature(handler)
    params = {name: kw[name] for name in sig.parameters if name in kw}
    return handler(**params)


def _make_bash_handler(permission_callback=None):
    """创建 bash handler，支持注入自定义 permission_callback"""
    def _bash(**kw):
        cmd = kw.get("command", "")
        return run_bash(cmd, permission_callback=permission_callback)
    return _bash


def build_tool_handlers(permission_callback=None):
    """构建 TOOL_HANDLERS dict，支持注入 bash 的 permission_callback"""
    return {
        "bash":          _make_bash_handler(permission_callback),
        "read_file":     lambda **kw: _validate_and_call(run_read, ["path"], **kw),
        "write_file":    lambda **kw: _validate_and_call(run_write, ["path", "content"], **kw),
        "edit_file":     lambda **kw: _validate_and_call(run_edit, ["path", "old_text", "new_text"], **kw),
        "read_excel":    lambda **kw: _validate_and_call(read_excel_to_md, ["file_path"], **kw),
        "write_excel":   lambda **kw: _validate_and_call(write_md_to_excel, ["file_path", "md_content"], **kw),
    }


TOOL_HANDLERS = build_tool_handlers()

def register_send_message_handler(handlers: dict, sender: str):
    """注册 send_message 工具 handler，支持 lead 和 subagent 之间双向发送消息。

    extra 字段统一使用：
    - sender_agent: 发送方 ID
    - receiver_agent: 接收方 ID
    """
    from src.infrastructure.message_bus import bus

    def _send_message(**kw):
        to = kw.get("to", "")
        content = kw.get("content", "")
        msg_type = kw.get("msg_type", "message")
        if not to:
            return "Error: 'to' is required"
        extra = {"sender_agent": sender, "receiver_agent": to}
        msg_id = bus.send(sender, to, content, msg_type, extra=extra)
        return f"Sent {msg_type} to {to} (msg_id={msg_id})"

    handlers["send_message"] = _send_message

# ---------- Milestone extractors (for turn-level compression) ----------
# 定义在工具 handler 旁边，新增工具时同步补充 milestone 摘要逻辑。
# 键为 tool name，值为 (args: dict, result: str) -> str
# 原则：只记录"动作 + 锚点（路径、命令）"，不记录工具返回内容。
MILESTONE_EXTRACTORS = {
    "bash": lambda args, result: f"bash: {args.get('command', '')[:200]}",
    "read_file": lambda args, result: f"read_file: {args.get('path', '')}",
    "write_file": lambda args, result: f"write_file: {args.get('path', '')}",
    "edit_file": lambda args, result: f"edit_file: {args.get('path', '')}",
    "read_excel": lambda args, result: f"read_excel: {args.get('file_path', '')}",
    "write_excel": lambda args, result: f"write_excel: {args.get('file_path', '')}",
    "send_message": lambda args, result: (
        f"send_message → {args.get('to', '')} [{args.get('msg_type', 'message')}]"
    ),
}


# ---------- 通用工具（bash, 文件读写, excel, todo）----------

COMMON_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": f"Run a shell command in {get_backend().shell_note}. Use syntax appropriate for this shell.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string", "description": "command to be run"}},
                "required": ["command"],
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read file contents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "The path to the file to be read."},
                    "limit": {"type": "integer", "description": "The maximum number of lines to read from the file. If not specified, the entire file will be read."},
                },
                "required": ["path"],
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "The file path where the content should be written."},
                    "content": {"type": "string", "description": "The content to be written to the file."},
                },
                "required": ["path", "content"],
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace exact text in file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "The file path where the text will be replaced."},
                    "old_text": {"type": "string", "description": "The exact text to be replaced in the file."},
                    "new_text": {"type": "string", "description": "The new text to replace the old_text in the file."},
                },
                "required": ["path", "old_text", "new_text"],
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_excel",
            "description": "Reads an Excel file (.xls or .xlsx) and converts its content into a Markdown table format.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "The path to the Excel file to read. Must be a valid .xls or .xlsx file."
                    }
                },
                "required": ["file_path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_excel",
            "description": "Writes a Markdown table to an Excel file (.xlsx).",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "The destination path for the output Excel file. Must end with .xlsx"
                    },
                    "md_content": {
                        "type": "string",
                        "description": "A valid Markdown table string containing headers and data rows. Must include a header row and at least one data row."
                    }
                },
                "required": ["file_path", "md_content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "send_message",
            "description": (
                "Send a message to a specific agent. "
                "Use msg_type 'shutdown_request' to confirm a subagent is done, "
                "or 'revision_request' to ask a subagent to revise its work. "
                "Subagents use this to send results back to their lead agent."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {
                        "type": "string",
                        "description": "Target agent ID or name"
                    },
                    "content": {
                        "type": "string",
                        "description": "Message content. For shutdown_request this can be empty. For revision_request, provide feedback."
                    },
                    "msg_type": {
                        "type": "string",
                        "enum": ["message", "shutdown_request", "revision_request"],
                        "default": "message",
                        "description": "Message type"
                    },
                },
                "required": ["to"]
            }
        }
    },
]
