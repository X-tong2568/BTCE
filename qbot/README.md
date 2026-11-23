# qbot — QQ 官方 Bot 适配器

BTCE 的官方 QQ 通道依赖本目录的独立进程。它用 `qq-botpy` 连接 QQ 官方
WebSocket，持有 AppID/Secret 和群别名映射，负责发送队列、频控、重试、图片上传，
并通过本地 HTTP 给 BTCE 提供发送/回复/别名修改接口。

它是**独立进程**，不要和 BTCE 主进程合并运行。

## 文件

| 文件 | 作用 |
|---|---|
| `qbot.py` | 入口：WebSocket 事件、本地 HTTP 服务、鉴权检查、`--discover-group` |
| `qbot_sender.py` | 发送队列、频控、重试、图片上传、HTTP handler |
| `qbot_state.py` | `msg_seq`/每日计数持久化、限速器、part 规范化 |
| `qbot_errors.py` | 官方错误码分类 |
| `config.py` | 通用默认配置 |
| `config_custom.example.py` | 本机配置模板（复制为 `config_custom.py`） |
| `logger_config.py` | 日志初始化 |
| `requirements.txt` | qbot 依赖 |

`config_custom.py`、`data/`、`logs/` 均已加入 `.gitignore`，不要提交真实凭证和运行数据。

## 安装

```bash
cd qbot
python3 -m venv .venv39
.venv39/bin/pip install -r requirements.txt
```

Windows：

```bat
cd qbot
E:\python3.9\python.exe -m venv .venv39
.venv39\Scripts\python.exe -m pip install -r requirements.txt
```

## 配置

```bash
cp config_custom.example.py config_custom.py
```

至少填写：

- `QBOT_ENV = "prod"`
- `QBOT_APP_ID`、`QBOT_APP_SECRET`：QQ 开放平台机器人凭证
- `GROUP_OPENIDS["prod"]`：群别名 → `group_openid`（不是 QQ 群号）
- `QBOT_SEND_TOKEN`：本地 HTTP 共享口令（建议随机值，与 BTCE 的 `QQBOT_SEND_TOKEN` 一致）
- `QBOT_ADMIN_OPENIDS`：官方管理员 `member_openid`（不是 QQ 号）
- `QBOT_FORWARD_TOKEN`：可选，与 BTCE 的 `QQ_OFFICIAL_FORWARD_TOKEN` 一致
- `QBOT_CALLBACK_FORWARD_URL`：BTCE 回调地址，例如 `http://127.0.0.1:15510/official/callback`

拿 `group_openid`：

```bash
.venv39/bin/python qbot.py --discover-group
```

把机器人拉进测试群或在群里 @ 它，日志会打印 `group_openid`。只能先拿到 openid，
再填进 `GROUP_OPENIDS`。

## 运行

```bash
# 先做鉴权自检
.venv39/bin/python qbot.py --check-auth

# 前台运行
.venv39/bin/python qbot.py

# 只跑本地 HTTP 契约（不连 QQ）
.venv39/bin/python qbot.py --mock
```

pm2：

```bash
pm2 start qbot.py --name ttkj-qbot --interpreter .venv39/bin/python
pm2 save
```

## 本地 HTTP 接口

默认监听 `127.0.0.1:8089`，可用 `QBOT_SEND_HOST` / `QBOT_SEND_PORT` 覆盖。

### POST `/official_send`

```json
{
  "group": "test1",
  "parts": [
    {"type": "text", "text": "文字"},
    {"type": "image", "source": "/path/to/a.png"},
    {"type": "card", "title": "标题", "description": "描述", "url": "https://...", "pic_url": "https://...", "fallback_text": "卡片失败时补发的纯文本"}
  ],
  "wait": false,
  "request_id": "可选"
}
```

- `group` 是群别名，不是 QQ 群号。
- `wait=false` 时入队后立即返回；`wait=true` 时等待最终发送结果。
- `card` 对应 `msg_type=8` 图文卡片；`title` ≤50 字、`description` ≤200 字，
  `url`/`pic_url` 必须是 http(s) 地址。
- `fallback_text` 可选（≤2000 字）：卡片重试后仍失败时自动改发纯文本，
  避免图片转存失败导致整条推送被吞。
- 也兼容旧的 `{"type": "text", "content": "..."}` 单条格式。

### POST `/official_reply`

用入站消息的 `msg_id` 做被动回复，不占主动消息频次：

```json
{"group": "test1", "msg_id": "...", "content": "回复内容", "msg_seq": 1}
```

### POST `/official_alias`

修改群别名并持久化到 `data/alias_overrides.json`：

```json
{"old": "auto_1", "new": "新别名"}
```

### GET `/health`

返回 WebSocket 状态、群别名列表（含 openid 尾号）、队列长度和发送计数。

## 与 BTCE 的对应配置

BTCE 的 `config_qq.py`：

```python
QQ_OFFICIAL_ENABLED = True
QQ_OFFICIAL_GROUPS = ["test1"]                 # qbot 群别名
QQBOT_SEND_URL = "http://127.0.0.1:8089/official_send"
QQBOT_REPLY_URL = "http://127.0.0.1:8089/official_reply"
QQBOT_SEND_TOKEN = ""                            # 与 qbot 的 QBOT_SEND_TOKEN 一致
QQ_OFFICIAL_ADMIN_OPENIDS = []                   # member_openid
QQ_OFFICIAL_FORWARD_TOKEN = ""                   # 与 qbot 的 QBOT_FORWARD_TOKEN 一致
```

BTCE 只通过本地 HTTP 调用 qbot；qbot 挂掉只影响官方 QQ 通道，不影响邮件、B站发布和归档。
