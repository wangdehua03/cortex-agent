"""Lead Agent 专用工具定义"""

import json
from .common import COMMON_TOOLS, MILESTONE_EXTRACTORS as COMMON_MILESTONE_EXTRACTORS
from .subagent import TODO_TOOL


# --- task_delegate tool (spawns subagents) ---

TASK_DELEGATE_TOOL = {
    "type": "function",
    "function": {
        "name": "task_delegate",
        "description": (
            "Spawn a subagent with fresh context. It shares the filesystem but not conversation history. "
            "If a relevant skill exists, specify skill_name. "
            "Optionally bind to a task_create task_id so the task's owner is automatically updated. "
            "Otherwise, leave skill_name and task_id null for general-purpose delegation."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Detailed, self-contained task description with all necessary context. Workers cannot see your conversation."
                },
                "description": {
                    "type": "string",
                    "description": "Short description of the subtask"
                },
                "skill_name": {
                    "type": ["string", "null"],
                    "description": "Name of skill to load in sub-agent. Must be one of the registered skills or null for general tasks.",
                },
                "task_id": {
                    "type": ["integer", "null"],
                    "description": "Optional: the ID from task_create that this subagent will execute. When set, the task's owner is automatically updated to the subagent's ID. Use this to bind planning to execution.",
                },
            },
            "required": ["prompt"]
        }
    }
}


# --- s07 task management tools ---

TASK_CREATE_TOOL = {
    "type": "function",
    "function": {
        "name": "task_create",
                    "description": (
                        "Create a persistent task that survives context compression. "
                        "Use this to plan before delegating. Owner is typically set later via task_delegate(task_id=...) which auto-binds the task to the spawned subagent."
                    ),
        "parameters": {
            "type": "object",
            "properties": {
                "subject": {
                    "type": "string",
                    "description": "Short task title"
                },
                "description": {
                    "type": "string",
                    "description": "Detailed task description"
                },
                "owner": {
                    "type": ["string", "null"],
                    "description": "Agent responsible for this task. Usually omitted at plan time — it gets set automatically when you delegate via task_delegate(task_id=...) with value subagent_id or 'lead'."
                },
            },
            "required": ["subject", "description"]
        }
    }
}

TASK_UPDATE_TOOL = {
    "type": "function",
    "function": {
        "name": "task_update",
        "description": (
            "Update a task's status or dependencies. "
            "Setting status to 'completed' automatically removes this task "
            "from all other tasks' blockedBy lists."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "integer",
                    "description": "Task ID to update"
                },
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "completed"],
                    "description": "New status"
                },
                "add_blocked_by": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Add dependencies (task IDs this task is blocked by)"
                },
                "remove_blocked_by": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Remove dependencies"
                },
            },
            "required": ["task_id"]
        }
    }
}

TASK_LIST_TOOL = {
    "type": "function",
    "function": {
        "name": "task_list",
        "description": "List all persistent tasks with status summary. Optionally filter by owner or status.",
        "parameters": {
            "type": "object",
            "properties": {
                "owner": {
                    "type": "string",
                    "description": "Filter by task owner"
                },
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "completed"],
                    "description": "Filter by status"
                },
            }
        }
    }
}

TASK_GET_TOOL = {
    "type": "function",
    "function": {
        "name": "task_get",
        "description": "Get full details of a task by ID, including dependencies and timestamps.",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "integer",
                    "description": "Task ID"
                },
            },
            "required": ["task_id"]
        }
    }
}

TASK_TOOLS = [TASK_CREATE_TOOL, TASK_UPDATE_TOOL, TASK_LIST_TOOL, TASK_GET_TOOL]


# --- Milestone extractors for lead-specific tools ---
def _build_milestone_extractors():
    """动态构建 milestone extractors，合并 common + lead 部分。"""
    extractors = dict(COMMON_MILESTONE_EXTRACTORS)
    extractors.update({
        "task_delegate": lambda args, result: (
            f"task_delegate: {args.get('description', '')[:80]} — "
            f"subagent_id={_extract_subagent_id(result)}"
        ),
        "task_create": lambda args, result: (
            f"task_create: #{args.get('task_id', '?')} {args.get('subject', '')[:80]}"
        ),
        "task_update": lambda args, result: (
            f"task_update: #{args.get('task_id', '?')} → {args.get('status', '')}"
        ),
        "task_list": lambda args, result: (
            f"task_list: ({len(result)} chars summary)"
        ),
        "task_get": lambda args, result: (
            f"task_get: #{args.get('task_id', '?')}"
        ),
    })
    return extractors


def _extract_subagent_id(result_text: str) -> str:
    """从 task_delegate 返回结果中提取 subagent_id"""
    for line in result_text.split("\n"):
        line = line.strip()
        if line.startswith("SubAgent ID:") or line.startswith("- SubAgent ID:"):
            return line.split(":", 1)[1].strip()
    return "unknown"


LEAD_MILESTONE_EXTRACTORS = _build_milestone_extractors()


def _build_lead_agent_tools():
    """动态构建 Lead Agent 工具集，注入 skill_name enum"""
    from .common import skill_loader

    skill_names = list(skill_loader._registry.keys())
    skill_name_enum = [None] + skill_names if skill_names else [None]

    task_tool = TASK_DELEGATE_TOOL.copy()
    task_tool["function"] = task_tool["function"].copy()
    task_tool["function"]["parameters"] = task_tool["function"]["parameters"].copy()
    task_tool["function"]["parameters"]["properties"] = task_tool["function"]["parameters"]["properties"].copy()
    task_tool["function"]["parameters"]["properties"]["skill_name"] = {
        "type": ["string", "null"],
        "description": f"Name of skill to load in sub-agent. Must be one of {skill_names} or null for general tasks.",
        "enum": skill_name_enum,
    }

    return [task_tool] + TASK_TOOLS + COMMON_TOOLS


# 动态构建的完整工具集（skill_name enum 已注入）
LEAD_AGENT_TOOLS = _build_lead_agent_tools()