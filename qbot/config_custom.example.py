# config_custom.example.py
# 复制为 config_custom.py 后填入真实值。
# 注意：空赋值也会覆盖 config.py 默认值；不需要覆盖的项请注释掉。
# config_custom.py 已加入 .gitignore，不会被提交，也不会被部署覆盖。

# 官方 Bot 环境：sandbox 或 prod。一个进程只跑一个环境。
QBOT_ENV = "sandbox"

# QQ 开放平台机器人凭证，不要提交到 Git。
QBOT_APP_ID = ""
QBOT_APP_SECRET = ""

# 群 openid：支持一个环境多个群，“群别名 -> openid”；不是 QQ 群号。
# 别名可以自己起，比如 test1/test2；/send 的 group 字段用它选群。
GROUP_OPENIDS = {
    "sandbox": {},
    "prod": {
        "test1": "",
        "test2": "",
    },
}

# 官方群管理员 member_openid（不是 QQ 号）；管理员 @ 机器人一次后从日志获取。
# 空列表表示所有 @ 都不回复。
QBOT_ADMIN_OPENIDS = []

# 机器人在群里的 openid（@ 消息里的 <@...>）；从一条 @ 机器人的日志获取。
QBOT_BOT_OPENID = ""

# 新群自动加入推送列表（默认开）；关闭后只记录 openid，不自动推送。
QBOT_AUTO_ADD_GROUPS = True

# 本地 HTTP 共享口令，建议填随机字符串；BTCE 调用时放进 Authorization: Bearer。
QBOT_SEND_TOKEN = ""

# 事件转发到 BTCE 时的可选口令（与 BTCE 的 QQ_OFFICIAL_FORWARD_TOKEN 一致）。
QBOT_FORWARD_TOKEN = ""

# NapCat 热备配置，本项目暂不使用。
NAPCAT_URL = "http://127.0.0.1:8080"
NAPCAT_TOKEN = ""

# 服务器公网 IP，用于 check_ip.py 自检；留空则只打印当前 IP。
EXPECTED_PUBLIC_IP = ""
