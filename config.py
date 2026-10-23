# config.py
import os
from pathlib import Path

# ===== 基础配置 =====
# 这些配置用于系统标识和日志记录，可以保留但简化
APP_NAME = "BTCE3.0"

# ===== 文件路径配置 =====
BASE_DIR = Path(__file__).parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ===== 日志配置 =====
LOG_LEVEL = "INFO"
LOG_FILE_PATH = LOG_DIR / "app.log"
LOG_FORMAT = '%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s'
LOG_DATE_FORMAT = '%Y-%m-%d %H:%M:%S'
MAX_LOG_SIZE_MB = 5
LOG_BACKUP_COUNT = 1

# ===== 直播监控配置 =====
LIVE_ROOM_ID = 82568  # 直播间房间号
LIVE_CHECK_INTERVAL = 15  # 直播检查间隔（秒）
LIVE_API_TIMEOUT = 10  # API请求超时时间（秒）
LIVE_MAX_RETRIES = 3  # 最大重试次数
LIVE_RETRY_DELAY = 5  # 重试延迟（秒）

# ===== 直播告警阈值 =====
LIVE_FAILURE_THRESHOLD = 10  # 连续失败阈值（P1告警）
LIVE_SUCCESS_RATE_THRESHOLD = 0.9  # 成功率阈值（P2告警）

# ===== 动态监控配置 =====
UP_NAME = "星瞳_Official"  # 监控目标昵称（需自行配置）
UP_UID = "401315430"  # 监控目标B站UID
PINNED_DYNAMIC_ID = ""  # 置顶动态ID（留空，API首次发现后自动填入）
PINNED_CHECK_INTERVAL = 3600  # 置顶动态更换检测间隔（秒），1小时
CHECK_INTERVAL = 6  # v4.8：页面复用后缩短检查间隔（原8s）
MAX_RETRY_ATTEMPTS = 3
RETRY_DELAY = 5  # 秒

# ===== 浏览器配置 =====
BROWSER_CONFIG = {
    "headless": True,
    "args": [
        "--disable-dev-shm-usage",
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-gpu",
        "--disable-software-rasterizer",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--memory-pressure-off",
        "--max_old_space_size=4096"
    ]
}

# ===== 监控配置 =====
BROWSER_RESTART_INTERVAL = 100  # v4.8：页面复用后减少重启频率（原10轮）
HEALTH_CHECK_INTERVAL = 15  # 每15次循环进行健康检查
TASK_TIMEOUT = 30  # 单个任务超时时间(秒)
MEMORY_THRESHOLD_MB = 1500  # 内存阈值(MB)

# ===== 状态监控配置 =====
STATUS_MONITOR_INTERVAL = 7200  # 状态检查间隔（秒），2小时
NO_UPDATE_ALERT_HOURS = 28      # 无更新提醒阈值（小时）

# ===== 性能监控配置 =====
PERFORMANCE_REPORT_CYCLE_INTERVAL = 18000  # 18000轮发送一次报告（约20h一次）

# ===== 告警阈值配置 =====
# 置顶评论监控（Playwright浏览器）
P1_TOTAL_FAILURE_THRESHOLD = 100  # 置顶评论连续失败次数阈值（P1告警）
P2_SUCCESS_RATE_THRESHOLD = 0.8   # 置顶评论成功率阈值（80%，P2告警）

# API动态列表（urllib HTTP请求，独立统计）
API_P1_FAILURE_THRESHOLD = 10     # API连续失败次数阈值（P1告警，比浏览器更快告警）
API_P2_SUCCESS_RATE_THRESHOLD = 0.9  # API成功率阈值（90%，比浏览器更严格）

# ===== 系统状态检查间隔 =====
SYSTEM_STATUS_CHECK_INTERVAL = 3600  # 系统状态检查间隔（秒）

# ===== 三通道推送模式配置 =====
# 每个通道可选 "text" 或 "screenshot"
#   text:       QQ=纯文本+alt属性+评论区图片  邮件=文字+表情+评论区图片
#   screenshot: QQ/邮件=截图内嵌
# B站特殊：screenshot=截图发布  text=跳过不发布
#
# ── 切换示例 ──
# 推荐（默认）：QQ+邮件秒推不阻塞，B站截图
#   QQ_MODE = "text"  |  EMAIL_MODE = "text"  |  BILI_MODE = "screenshot"
# 全部截图（旧版行为，QQ/邮件会等截图）：
#   QQ_MODE = "screenshot"  |  EMAIL_MODE = "screenshot"  |  BILI_MODE = "screenshot"
# 只要QQ和B站截图、邮件用文字：
#   QQ_MODE = "screenshot"  |  EMAIL_MODE = "text"  |  BILI_MODE = "screenshot"
QQ_MODE = "text"
EMAIL_MODE = "text"
BILI_MODE = "screenshot"

# ===== 置顶评论卡片渲染配置 =====
# screenshot 模式改为渲染统一卡片，不再直接截 B站页面元素
CARD_WIDTH = 1080                  # 卡片宽度（px）
CARD_MIN_HEIGHT = 700              # 最小高度（v4.17 由 890 调小，减少短内容的空白）
CARD_PADDING = 69                  # 卡片内边距
CARD_AVATAR_SIZE = 140             # 头像尺寸
CARD_NAME_FONT_SIZE = 43           # 名字字号（v4.17 曾调到 48，偏大，已调回）
CARD_META_FONT_SIZE = 24           # 时间/IP 字号
CARD_TEXT_FONT_SIZE = 46           # 常规正文字号（v4.17 由 37 调大，避免缩略图里看不清）
CARD_SHORT_TEXT_FONT_SIZE = 54     # 短评论正文字号（无图片且文字较少时自动使用）
CARD_SHORT_TEXT_MAX_CHARS = 60     # 短评论判定阈值：纯文本字符数不超过该值
CARD_COMMENT_IMAGE_WIDTH = 300     # 评论图片宽度，约内容区 1/3；0=铺满内容区
CARD_SIGNATURE_WIDTH = 202         # 签名图片宽度
CARD_SIGNATURE_PATH = "assets/signature_example.png"  # 示例签名；真实签名在 config_custom.py 覆盖
CARD_TIME_SOURCE = "comment"       # comment=评论 ctime；detect=检测时间
CARD_SCALE_FACTOR = 1              # 1=1080宽；1.5=1620宽；2=2160宽（更清晰，文件更大）
CARD_PROFILE_CACHE_TTL = 86400     # 头像/头像框缓存有效期（秒）
CARD_ASSET_CACHE_DIR = BASE_DIR / "data" / "card_assets"
CARD_DYNAMIC_CACHE_FILE = BASE_DIR / "data" / "card_dynamic_cache.json"

# ===== 动态自动发布配置 =====
AUTO_PUBLISH_ENABLED = True  # 置顶评论变更时自动发布B站动态（含截图+话题+链接，dry-run模式见auto_publish.py）
AUTO_PUBLISH_TOPIC_ID = 66066  # 话题ID（小星星的家）
AUTO_PUBLISH_TOPIC_NAME = "小星星的家"  # 话题名称

# ===== 直播B站发布配置 =====
LIVE_BILI_PUBLISH_ENABLED = True  # 直播间标题更新时自动发布B站动态（封面+标题+时间+链接+状态标签）

# ===== 动态类型过滤 =====
# B站会自动生成一些"非用户主动发布"的动态类型，监测到后跳过不推送
# DYNAMIC_TYPE_LIVE_RCMD：开播后B站自动生成的直播推荐动态（下播后自动消失）
# 完整类型列表参考：https://github.com/SocialSisterYi/bilibili-API-collect
DYNAMIC_SKIP_TYPES = {
    "DYNAMIC_TYPE_LIVE_RCMD",  # 直播开播自动动态（下播消失）
}

# ===== 文件路径配置 =====
COOKIE_FILE = BASE_DIR / "cookies.json"
HISTORY_FILE = BASE_DIR / "bili_pinned_comment.json"
MAIL_SAVE_DIR = BASE_DIR / "sent_emails"

# 创建必要目录
for dir_path in [MAIL_SAVE_DIR, LOG_DIR]:
    dir_path.mkdir(parents=True, exist_ok=True)

# ===== 邮件配置 =====
# 直接从独立的配置文件导入，不设置默认值
from config_email import SMTP_SERVER, SMTP_PORT, EMAIL_USER, EMAIL_PASSWORD, TO_EMAILS, STATUS_MONITOR_EMAILS

# ===== QQ推送配置 =====
# 直接从独立的配置文件导入，不设置默认值
from config_qq import QQ_GROUP_IDS, QQ_PUSH_ENABLED

# ===== 个性化覆盖（config_custom.py 不存在则用上方默认值）=====
# 任何变量在 config_custom.py 中定义后都会覆盖这里的默认值（包括配置值和文案）
# config_custom.py 已加入 .gitignore，不会被 Git 提交也不会被部署覆盖
QQ_SINGLE_LINK_LABEL = "🔗"
QQ_SINGLE_TIME_LABEL = "📅"
QQ_BATCH_LINK_LABEL = "🔗"
QQ_BATCH_TIME_LABEL = "📅"
EMAIL_PINNED_SUBJECT = "置顶评论更新"
QQ_PINNED_UPDATE_TEXT = "置顶评论更新"       # QQ消息置顶评论标题
BILI_PUBLISH_TEXT = "置顶评论更新~"          # B站发布动态正文
BILI_PC_LINK_LABEL = "电脑链接"             # B站发布PC跳转链接文字
BILI_MOBILE_LINK_LABEL = "手机链接"         # B站发布手机跳转链接文字
EMAIL_HEADER_TITLE = "动态置顶评论更新通知"   # 置顶评论邮件标题

try:
    from config_custom import *  # noqa: F403 覆盖上方所有默认值
except ImportError:
    pass