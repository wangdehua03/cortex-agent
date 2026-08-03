from pathlib import Path

# WORKDIR = Path.cwd()
WORKDIR = Path('/data/PyProject-dev2/wangdehua')
APP_ROOT = Path.cwd()

# LLM Client

# #Qwen3
# API_KEY = 'NULL'
# BASE_URL = 'http://172.55.209.32:8888/v1'
# MODEL = "/model_cache/qwen/Qwen3-32B"

#qwen3.6-27b
BASE_URL = 'http://172.16.1.8:8899/v1'
MODEL = 'qwen3p6_27b'
API_KEY = 'NULL'
CONTEXT_WINDOW = 128_000

#Tokenizer 
TOKENIZER_PATH = APP_ROOT.joinpath("rsrc/tokenizer/qwen3-32B")



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
