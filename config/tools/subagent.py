"""SubAgent 专用工具定义"""

from .common import COMMON_TOOLS, MILESTONE_EXTRACTORS as COMMON_MILESTONE_EXTRACTORS

TODO_TOOL = {
    "type": "function",
    "function": {
        "name": "todo",
        "description": "Update task list. Track progress on multi-step tasks.",
        "parameters": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "text": {"type": "string"},
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed"]
                            }
                        },
                        "required": ["id", "text", "status"]
                    }
                }
            },
            "required": ["items"]
        }
    }
}

# 子Agent工具集（todo + common）
SUB_AGENT_TOOLS = [TODO_TOOL] + COMMON_TOOLS


# --- Milestone extractors for subagent tools ---
def _build_subagent_milestone_extractors():
    """动态构建 subagent milestone extractors，合并 common + todo。"""
    extractors = dict(COMMON_MILESTONE_EXTRACTORS)
    extractors.update({
        "todo": lambda args, result: (
            f"todo: {len(args.get('items', []))} items"
        ),
    })
    return extractors


SUBAGENT_MILESTONE_EXTRACTORS = _build_subagent_milestone_extractors()
