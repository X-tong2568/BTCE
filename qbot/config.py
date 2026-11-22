# config.py
# 通用默认配置。真实密钥、群 openid、服务器 IP 一律放在 config_custom.py，
# config_custom.py 已加入 .gitignore，不会被提交或覆盖。
import json
import os
from pathlib import Path

# ===== 路径配置 =====
BASE_DIR = Path(__file__).parent
LOG_DIR = BASE_DIR / "logs"
DATA_DIR = BASE_DIR / "data"
ASSETS_DIR = BASE_DIR / "assets"
FIXTURE_IMAGE = ASSETS_DIR / "sample_card.png"

# ===== 双通道开关 =====
# 测试期两个都开；NapCat 失效时只关 NapCat，回滚也只关官方通道。
# 本项目只实现官方通道，NapCat 配置保留给 BTCE 接入时使用。
QQ_NAPCAT_ENABLED = True
QQ_OFFICIAL_ENABLED = True

# ===== 官方 Bot 配置 =====
# 一个进程只跑一个环境：sandbox 或 prod。
# botpy 的 is_sandbox 是进程级配置，同一进程不能混用沙箱和正式域名。
QBOT_ENV = os.getenv("QBOT_ENV", "sandbox")
QBOT_APP_ID = os.getenv("QBOT_APP_ID", "")
QBOT_APP_SECRET = os.getenv("QBOT_APP_SECRET", "")

# 每个环境对应一个测试群 openid。后续要支持多群时，再改成列表或别名映射。
_sandbox_group = os.getenv("QBOT_SANDBOX_GROUP_OPENID", "")
_prod_group = os.getenv("QBOT_PROD_GROUP_OPENID", "")

# 支持一个环境多个群：“群别名 -> openid”；旧单字符串配置也兼容。
GROUP_OPENIDS = {
    "sandbox": {"default": _sandbox_group} if _sandbox_group else {},
    "prod": {"default": _prod_group} if _prod_group else {},
}

# 官方群管理员 member_openid（不是 QQ 号）；空列表表示谁都不回复。
# 管理员 @ 机器人一次后，从 logs/qbot.log 的 member_openid 字段获取。
QBOT_ADMIN_OPENIDS = []

# 机器人在群里的 openid（@ 消息里的 <@...>）；只处理真正 @ 机器人的消息。
QBOT_BOT_OPENID = os.getenv("QBOT_BOT_OPENID", "")

# ===== 本地 HTTP 契约 =====
QBOT_SEND_HOST = "127.0.0.1"
QBOT_SEND_PORT = int(os.getenv("QBOT_SEND_PORT", "8089"))
QBOT_SEND_PATH = "/official_send"
# 本地共享口令，留空表示不校验。BTCE 与 qbot 在同一台机器时建议填一个随机值。
QBOT_SEND_TOKEN = os.getenv("QBOT_SEND_TOKEN", "")

# ===== 消息与队列 =====
MAX_MESSAGE_LENGTH = 2000
SEND_QUEUE_MAX = 100
SEND_QUEUE_TTL_SECONDS = 600
RETRY_MAX_ATTEMPTS = 3
RETRY_BASE_DELAY_SECONDS = 2
RETRY_MAX_DELAY_SECONDS = 30

# ===== 频控 =====
# 官方群主动消息：单群 20 qpm、1000 条/日；Bot 维度未认证 30 qpm。
# 认证类型不同可调高 RATE_LIMIT_BOT_QPM，但单群仍应按 20 qpm 保守执行。
RATE_LIMIT_GROUP_QPM = 20
RATE_LIMIT_BOT_QPM = 30
RATE_LIMIT_DAILY_PER_GROUP = 1000

# ===== HTTP / 媒体 =====
QBOT_HTTP_TIMEOUT = 15
PROBE_WAIT_TIMEOUT = 60
IMAGE_DOWNLOAD_TIMEOUT = 20
MEDIA_BINARY_MAX_BYTES = 5 * 1024 * 1024

# ===== NapCat 热备（本项目不实现，仅保留配置位） =====
NAPCAT_URL = "http://127.0.0.1:8080"
NAPCAT_TOKEN = ""

# ===== 测试与调试 =====
# QBOT_MOCK=1 时不连 QQ，只跑本地 HTTP/队列/错误分类，供离线自测。
QBOT_MOCK = os.getenv("QBOT_MOCK", "0") == "1"
# 是否允许请求里带 mock_error 注入错误码；只在 mock 模式下生效。
QBOT_ALLOW_MOCK_ERROR = os.getenv("QBOT_ALLOW_MOCK_ERROR", "0") == "1"
# 群内 @ 机器人发 ping，机器人被动回复 pong，用于验证 WebSocket 收事件。
QBOT_ECHO_ENABLED = os.getenv("QBOT_ECHO_ENABLED", "0") == "1"
# 测试用指令回显：只回复“已收到”，不操作 BTCE；正式接入后关掉。
QBOT_TEST_COMMANDS = os.getenv("QBOT_TEST_COMMANDS", "0") == "1"
# 运行中把机器人拉进新群时，自动加入推送列表并分配 auto_N 别名。
QBOT_AUTO_ADD_GROUPS = os.getenv("QBOT_AUTO_ADD_GROUPS", "1") == "1"
# 官方事件转发地址，预留给 BTCE 的 /official/callback。
QBOT_CALLBACK_FORWARD_URL = os.getenv("QBOT_CALLBACK_FORWARD_URL", "")
QBOT_FORWARD_TIMEOUT = 10
# 可选：转发事件时带 X-QBOT-Forward-Token，BTCE 侧校验。
QBOT_FORWARD_TOKEN = os.getenv("QBOT_FORWARD_TOKEN", "")

# ===== 公网 IP 白名单自检 =====
# 在 config_custom.py 填服务器公网 IP；留空表示只打印当前 IP，不做比对。
EXPECTED_PUBLIC_IP = ""

# ===== 文案占位 =====
UP_NAME = "测试UP主"
QQ_PINNED_UPDATE_TEXT = "置顶评论更新"
QQ_SINGLE_LINK_LABEL = "链接:"
QQ_SINGLE_TIME_LABEL = "时间:"

# ===== 日志 =====
LOG_LEVEL = "INFO"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 2

# ===== 本机配置覆盖（config_custom.py 不存在则用上方默认值） =====
try:
    from config_custom import *  # noqa: F401,F403
except ImportError:
    pass

# 探针 mock 模式专用：允许测试子进程覆盖环境和群映射，避免污染真实配置。
_test_env = os.getenv("QBOT_TEST_ENV", "")
if _test_env:
    QBOT_ENV = _test_env

_test_group_map = os.getenv("QBOT_TEST_GROUP_MAP_JSON", "")
if _test_group_map:
    try:
        GROUP_OPENIDS = {QBOT_ENV: json.loads(_test_group_map)}
    except ValueError:
        pass

# 创建运行目录。所有 mkdir 都放在配置加载完成后，避免模块导入阶段产生副作用日志。
LOG_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)
