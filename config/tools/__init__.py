"""
Tools 工具定义模块

拆分后结构：
  common.py    - 所有 agent 共享的基础工具 + handlers
  lead.py      - Lead agent 专用工具（task_delegate, task management, send_message）
  subagent.py  - SubAgent 专用工具（todo + common）
"""

from .common import (
    skill_loader,
    TOOL_HANDLERS,
    build_tool_handlers,
    MILESTONE_EXTRACTORS,
    COMMON_TOOLS,
)
from .lead import LEAD_AGENT_TOOLS, LEAD_MILESTONE_EXTRACTORS
from .subagent import SUB_AGENT_TOOLS, SUBAGENT_MILESTONE_EXTRACTORS

# 旧代码兼容别名
MAIN_AGENT_TOOLS = LEAD_AGENT_TOOLS
SINGLE_AGENT_TOOLS = LEAD_AGENT_TOOLS
TOOLS = COMMON_TOOLS
CHILD_TOOLS = COMMON_TOOLS

__all__ = [
    "skill_loader",
    "TOOL_HANDLERS",
    "build_tool_handlers",
    "MILESTONE_EXTRACTORS",
    "COMMON_TOOLS",
    "LEAD_AGENT_TOOLS",
    "LEAD_MILESTONE_EXTRACTORS",
    "SUB_AGENT_TOOLS",
    "SUBAGENT_MILESTONE_EXTRACTORS",
    "MAIN_AGENT_TOOLS",
    "SINGLE_AGENT_TOOLS",
    "TOOLS",
    "CHILD_TOOLS",
]
