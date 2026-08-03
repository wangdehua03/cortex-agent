from pathlib import Path
import os

# WORKDIR = Path('/data/PyProject-dev2/wangdehua')
# Agent 执行 bash / 文件操作的工作目录（路径沙箱限制在此目录内）
WORKDIR = Path.cwd()
APP_ROOT = Path.cwd()

# LLM Client - Kimi (Moonshot) OpenAI 兼容 API
# 请设置环境变量：export KIMI_API_KEY="your_api_key"
# 可选设置环境变量覆盖模型：export KIMI_MODEL="kimi-for-coding"
BASE_URL = 'https://api.kimi.com/coding/v1'
API_KEY = os.environ.get('KIMI_API_KEY', '')

# 可用模型（均为 256K 上下文、支持图片）：
#   kimi-for-coding           编码专用模型
#   kimi-for-coding-highspeed 编码专用模型（高速版）
#   k3-256k                   通用模型
#   k3                        通用模型
MODEL = os.environ.get('KIMI_MODEL', 'kimi-for-coding')

# 根据模型自动匹配上下文窗口
_CONTEXT_WINDOW_MAP = {
    'kimi-for-coding': 262_144,
    'kimi-for-coding-highspeed': 262_144,
    'k3-256k': 262_144,
    'k3': 262_144,
}
CONTEXT_WINDOW = _CONTEXT_WINDOW_MAP.get(MODEL, 262_144)

# Tokenizer
# 优先使用本地 tokenizer；找不到时 LLMClient 会自动回退到 tiktoken (cl100k_base)
TOKENIZER_PATH = os.environ.get('TOKENIZER_PATH', APP_ROOT.joinpath("rsrc/tokenizer/qwen3-32B"))



# Agent Settings
KEEP_RECENT_ROUNDS = 1  # ContextStore 保留最近N个完整回合不压缩
TASK_NAG_ROUNDS = 3  # lead agent连续N轮无task操作时触发nag提醒

# SubAgent 日志目录
SUBAGENT_LOG_DIR = APP_ROOT.joinpath("logs", "subagents")

# LeadAgent 日志目录
LEAD_AGENT_LOG_DIR = APP_ROOT.joinpath("logs", "lead_agent")

# SubAgent review timeout: number of polling rounds waiting for lead review
SUBAGENT_REVIEW_TIMEOUT_ROUNDS = 60  # polling rounds waiting for lead review
SUBAGENT_REVIEW_SLEEP_INTERVAL = 60  # seconds to sleep per round

# Auto Compact Settings
AUTO_COMPACT_ENABLED = True  # 是否启用自动压缩
AUTO_COMPACT_THRESHOLD_RATIO = 0.8  # 当 token_used 超过 CONTEXT_WINDOW 的此比例时触发压缩（80%）

# Loop Detection Settings
LOOP_DETECT_ENABLED = True  # 是否启用循环检测
LOOP_DETECT_WINDOW = 6  # 滑动窗口大小：检测最近N轮的工具调用历史
LOOP_REPEAT_THRESHOLD = 3  # 连续重复N次相同的工具调用签名时判定为循环
