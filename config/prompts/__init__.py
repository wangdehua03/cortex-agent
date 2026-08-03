"""
Prompts 模块

用法:
    from config.prompts.single import MAIN_AGENT_BASE
    from config.prompts.subagent import SUBAGENT_BASE
    from config.prompts.compact import AUTO_COMPACT_SUMMARY_PROMPT
"""

# 单 Agent 模式 prompt
from config.prompts.single import (
    MAIN_AGENT,
    SKILL_SECTION,
)

# SubAgent prompt（共享）
from config.prompts.subagent import (
    SUBAGENT_BASE,
    SUBAGENT_SKILL_SECTION,
)

# Auto Compact prompt（共享）
from config.prompts.compact import (
    AUTO_COMPACT_SYSTEM,
    AUTO_COMPACT_SUMMARY_PROMPT,
    AUTO_COMPACT_SUMMARY_WRAPPER,
    AUTO_COMPACT_ASSISTANT_RESPONSE,
)
