# config_qq.example.py
# 复制为 config_qq.py 并填入真实配置

QQ_BOT_API_URL = "http://127.0.0.1:8080"
QQ_BOT_ACCESS_TOKEN = "your_token"
QQ_GROUP_IDS = [
    "123456789",
]
MAX_MESSAGE_LENGTH = 2500
QQ_PUSH_ENABLED = True

# ===== 回调服务器配置（v4.4） =====
QQ_BOT_QQ_ID = ""  # 机器人自身QQ号（识别@提及用，必填）
QQ_CALLBACK_ENABLED = True  # 是否启用回调服务器
QQ_CALLBACK_PORT = 15510  # 回调服务器端口
QQ_ADMIN_USERS = [
    "",  # 授权用户QQ号
]
