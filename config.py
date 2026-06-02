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
UP_NAME = "星瞳_Official"  # 监控目标昵称
UP_UID = "401315430"  # 监控目标B站UID
PINNED_DYNAMIC_ID = "1208161183564038147"  # 置顶动态ID（手动配置）
CHECK_INTERVAL = 8  # 秒
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
BROWSER_RESTART_INTERVAL = 10  # 每10次循环重启浏览器
HEALTH_CHECK_INTERVAL = 15  # 每15次循环进行健康检查
TASK_TIMEOUT = 30  # 单个任务超时时间(秒)
MEMORY_THRESHOLD_MB = 1500  # 内存阈值(MB)

# ===== 状态监控配置 =====
STATUS_MONITOR_INTERVAL = 7200  # 状态检查间隔（秒），2小时
NO_UPDATE_ALERT_HOURS = 28      # 无更新提醒阈值（小时）

# ===== 性能监控配置 =====
PERFORMANCE_REPORT_CYCLE_INTERVAL = 8000  # 8000轮发送一次报告

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

# ===== 动态自动发布配置 =====
AUTO_PUBLISH_ENABLED = False  # 置顶评论变更时自动发布B站动态（含截图+话题+链接）
AUTO_PUBLISH_TOPIC_ID = 66066  # 话题ID（小星星的家）
AUTO_PUBLISH_TOPIC_NAME = "小星星的家"  # 话题名称

# ===== 动态链接配置 =====
try:
    from dynamic import DYNAMIC_URLS
except ImportError:
    DYNAMIC_URLS = []
    print("⚠️ 警告: 无法从 dynamic.py 导入 DYNAMIC_URLS，使用空列表")

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