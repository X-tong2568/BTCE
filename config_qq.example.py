# config_qq.example.py
# 复制为 config_qq.py 并填入真实配置

# ===== NapCat 推送列表 =====
QQ_BOT_API_URL = "http://127.0.0.1:8080"
QQ_BOT_ACCESS_TOKEN = "your_token"
# NapCat 推送群（保持现状，不动）
QQ_GROUP_IDS = [
    "123456789",
]

# NapCat 管理群列表（只接收@机器人指令，不接收监控推送）
QQ_MANAGEMENT_GROUP_IDS = [
    "",
]

MAX_MESSAGE_LENGTH = 2500
QQ_PUSH_ENABLED = True

# ===== 双通道开关 =====
QQ_NAPCAT_ENABLED = True
# 官方渠道默认关闭；测试/切换官方时改 True
QQ_OFFICIAL_ENABLED = False

# 官方推送列表：qbot 群别名（不是 QQ 群号），与 NapCat 的 QQ_GROUP_IDS 分开维护。
# 不填就不往任何群推官方；把 bot 拉进群后，将 qbot 的 auto_N 或固定别名加到这里。
QQ_OFFICIAL_GROUPS = []

# ===== 官方 qbot 本地 HTTP =====
QQBOT_SEND_URL = "http://127.0.0.1:8089/official_send"
QQBOT_REPLY_URL = "http://127.0.0.1:8089/official_reply"
QQBOT_SEND_TOKEN = ""

# 官方管理员 member_openid（不是 QQ 号）；只有这些账号的指令会回复。
QQ_OFFICIAL_ADMIN_OPENIDS = []

# 0 = 不等待 qbot 结果（双通道测试期推荐）
# >0 = 等待秒数（官方单通道建议 65+，要大于 qbot 的等待超时）
QQ_OFFICIAL_WAIT_SECONDS = 0

# qbot 转发事件到 BTCE 时的可选口令；与 qbot 的 QBOT_FORWARD_TOKEN 一致。
QQ_OFFICIAL_FORWARD_TOKEN = ""

# ===== NapCat 回调服务器配置（v4.4） =====
QQ_BOT_QQ_ID = ""  # 机器人自身QQ号（识别@提及用，必填）
QQ_CALLBACK_ENABLED = True  # 是否启用回调服务器
QQ_CALLBACK_PORT = 15510  # 回调服务器端口
QQ_ADMIN_USERS = [
    "",  # 授权用户QQ号
]
