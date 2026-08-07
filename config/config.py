from pathlib import Path
import os

# Agent 执行 bash / 文件操作的工作目录（路径沙箱限制在此目录内）
WORKDIR = Path.cwd()
APP_ROOT = Path.cwd()


# ============================================================
# LLM 配置
# ------------------------------------------------------------
# 本平台通过 OpenAI 兼容协议与任意大模型服务通信：
#   - 第三方 API（Moonshot、OpenAI、DeepSeek 等）
#   - 本地部署（vLLM、Ollama、xinference、lmdeploy 等）
#
# 推荐通过环境变量或 .env 文件配置，避免在代码中硬编码 API 密钥。
# 环境变量优先级（从高到低）：
#   LLM_* 变量 > 旧版 KIMI_* 变量 > config.py 默认值
#
# 示例：
#   export LLM_BASE_URL=https://api.moonshot.cn/v1
#   export LLM_API_KEY=your_api_key
#   export LLM_MODEL=kimi-for-coding
#   export TOKENIZER_PATH=/path/to/local/tokenizer
# ============================================================

# API 基础地址
# 示例：
#   Moonshot:    https://api.moonshot.cn/v1
#   OpenAI:      https://api.openai.com/v1
#   本地 vLLM:    http://localhost:8000/v1
#   本地 Ollama:  http://localhost:11434/v1
_BASE_URL_DEFAULT = 'https://api.moonshot.cn/v1'
BASE_URL = os.environ.get('LLM_BASE_URL', os.environ.get('KIMI_BASE_URL', _BASE_URL_DEFAULT))

# API 密钥；强烈建议通过环境变量注入，不要在代码中保留真实密钥。
API_KEY = os.environ.get('LLM_API_KEY', os.environ.get('KIMI_API_KEY', ''))

# 模型名称，按所用服务填写。
MODEL = os.environ.get('LLM_MODEL', os.environ.get('KIMI_MODEL', 'kimi-for-coding'))

# 根据模型自动匹配上下文窗口；可通过 LLM_CONTEXT_WINDOW 强制覆盖。
_CONTEXT_WINDOW_MAP = {
    'kimi-for-coding': 262_144,
    'kimi-for-coding-highspeed': 262_144,
    'k3-256k': 262_144,
    'k3': 262_144,
    'kimi-k2.6': 262_144,
}
CONTEXT_WINDOW = int(
    os.environ.get('LLM_CONTEXT_WINDOW', _CONTEXT_WINDOW_MAP.get(MODEL, 262_144))
)

# 模型默认温度参数映射
# 说明：部分模型（如 Kimi K3）对 temperature=0 支持不佳，这里按模型给出默认温度。
# 可通过环境变量 LLM_TEMPERATURE / KIMI_TEMPERATURE 强制覆盖。
_DEFAULT_TEMPERATURE_MAP = {
    'kimi-for-coding': 1.0,
    'kimi-for-coding-highspeed': 0.0,
    'k3-256k': 1.0,
    'k3': 1.0,
    'kimi-k2.6': 1.0,
}
DEFAULT_TEMPERATURE = float(
    os.environ.get(
        'LLM_TEMPERATURE',
        os.environ.get(
            'KIMI_TEMPERATURE',
            _DEFAULT_TEMPERATURE_MAP.get(MODEL, 0.0),
        ),
    )
)


# Tokenizer 配置
# ------------------------------------------------------------
# 留空则自动使用 tiktoken (cl100k_base) 做近似 token 统计；
# 若使用本地大模型且需要更精确的统计，请设置为对应的 HuggingFace tokenizer 目录。
# 仅在路径不存在或加载失败时才会输出警告。
TOKENIZER_PATH = os.environ.get('TOKENIZER_PATH', '')


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


def check_config():
    """
    校验运行所需的最小配置是否已填写。
    缺少关键配置时打印清晰的配置说明并退出进程。
    """
    missing = []
    if not BASE_URL:
        missing.append("LLM_BASE_URL / KIMI_BASE_URL")
    if not API_KEY:
        missing.append("LLM_API_KEY / KIMI_API_KEY")
    if not MODEL:
        missing.append("LLM_MODEL / KIMI_MODEL")

    if missing:
        print("\033[31m[配置错误] 缺少以下必要的 LLM 连接配置：\033[0m")
        for item in missing:
            print(f"  - {item}")

        print("\n可通过以下任一方式配置（优先级：环境变量 > config/config.py）：")
        print("  1. 复制 .env.example 为 .env 并填写；")
        print("  2. 运行前 export 到当前 shell，例如：")
        print("     export LLM_API_KEY=your_api_key")
        print("     export LLM_BASE_URL=https://api.moonshot.cn/v1")
        print("     export LLM_MODEL=kimi-for-coding")
        print("  3. 直接修改 config/config.py 中的 BASE_URL / API_KEY / MODEL。")

        print("\n本地部署示例：")
        print("  export LLM_BASE_URL=http://localhost:8000/v1")
        print("  export LLM_MODEL=Qwen/Qwen3-32B")
        print("  export TOKENIZER_PATH=/path/to/local/tokenizer")

        raise SystemExit(1)
