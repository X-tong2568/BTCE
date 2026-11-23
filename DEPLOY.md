# BTCE 部署指南

本文说明如何从仓库部署一套可运行的 BTCE。适合 Linux 服务器，也适用于本地 Linux/WSL。
Windows 本机可直接运行，但 pm2/字体等系统步骤按 Linux 说明处理。

## 组成

| 组件 | 位置 | 作用 |
|---|---|---|
| BTCE 主程序 | 仓库根目录 | 动态/直播监控、邮件/B站/归档、QQ 调度 |
| qbot | `qbot/` | QQ 官方 Bot 常驻进程；BTCE 通过本地 HTTP 调用 |
| NapCat（可选） | 仓库外 | 旧 QQ 机器人通道；默认关闭，只用于回滚或不用官方 Bot 的场景 |

qbot 是独立进程，不要合并进 BTCE 主进程。BTCE 的官方 QQ 通道依赖
`qbot/`（或服务器上已有的同类 qbot 部署）。

## 1. 系统环境

- Linux（Rocky/Alma/Debian/Ubuntu 均可）
- Python 3.9+
- Node.js + pm2（后台常驻）
- Playwright Chromium 所需系统库
- 中文字体与 emoji 字体（卡片/截图里的中文和表情需要）

Rocky / RHEL 系：

```bash
dnf install -y python39 python39-pip nodejs npm \
  google-noto-sans-cjk-fonts google-noto-emoji-color-fonts
# 如果系统没有 python39 包，用发行版对应的 Python 3.9+ 即可
```

Debian / Ubuntu：

```bash
apt update
apt install -y python3 python3-pip python3-venv nodejs npm \
  fonts-noto-cjk fonts-noto-color-emoji
```

Playwright 系统依赖：

```bash
python3 -m playwright install-deps chromium
```

> 没有 emoji 字体时，无头截图中的 B站表情会显示成方块或空白。

## 2. 获取代码与依赖

```bash
git clone <你的 BTCE 仓库地址> BTCE
cd BTCE
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
```

qbot 使用独立虚拟环境，避免 botpy 和 BTCE 依赖互相影响：

```bash
cd qbot
python3 -m venv .venv39
.venv39/bin/pip install -r requirements.txt
cd ..
```

## 3. 配置 BTCE

仓库只提供模板，真实配置不进 git：

```bash
cp config_email.example.py config_email.py
cp config_qq.example.py config_qq.py
cp config_custom.example.py config_custom.py
cp config_github.example.py config_github.py
```

### 3.1 必填

`config.py` / `config_custom.py`：

- `UP_UID`：监控的 B站 UID
- `UP_NAME`：展示名称
- `PINNED_DYNAMIC_ID`：首次可留空，置顶自动发现后会自动维护
- 个性化文案、检查间隔等按需在 `config_custom.py` 覆盖

### 3.2 B站 Cookie

```bash
python get_cookies.py
```

按提示登录后生成 `cookies.json`。也可以从浏览器导出 Cookie 列表保存为同样结构。
Cookie 会过期，过期后置顶评论抓取和 API 会失败，需要重新获取。

### 3.3 邮件

`config_email.py` 填写 SMTP 服务器、端口、发件账号、授权码/密码和收件人。
QQ 邮箱、企业邮箱等按各自 SMTP 要求配置。

### 3.4 GitHub 归档（可选）

`config_github.py` 填写私有归档仓库 owner/repo、分支、PAT、归档开关。
不需要归档时把 `GITHUB_ARCHIVE_ENABLED` 设为 `False`。

### 3.5 QQ 官方通道

`config_qq.py`：

```python
QQ_PUSH_ENABLED = True
QQ_NAPCAT_ENABLED = False
QQ_OFFICIAL_ENABLED = True
QQ_OFFICIAL_GROUPS = ["test1"]                 # qbot 群别名，不是 QQ 群号
QQBOT_SEND_URL = "http://127.0.0.1:8089/official_send"
QQBOT_REPLY_URL = "http://127.0.0.1:8089/official_reply"
QQBOT_SEND_TOKEN = ""                            # 与 qbot 的 QBOT_SEND_TOKEN 一致
QQ_OFFICIAL_ADMIN_OPENIDS = []                   # member_openid，不是 QQ 号
QQ_OFFICIAL_FORWARD_TOKEN = ""                   # 与 qbot 的 QBOT_FORWARD_TOKEN 一致
QQ_OFFICIAL_WAIT_SECONDS = 0
QQ_CARD_ENABLED = True
QQ_CARD_FALLBACK_IMAGE = ""                      # 已弃用（v5.5）：无图动态改发文本+截图
```

## 4. 配置 qbot

```bash
cd qbot
cp config_custom.example.py config_custom.py
```

至少填写：

- `QBOT_ENV = "prod"`
- `QBOT_APP_ID`、`QBOT_APP_SECRET`：QQ 开放平台机器人凭证
- `GROUP_OPENIDS["prod"]`：群别名 → `group_openid`
- `QBOT_SEND_TOKEN`：本地 HTTP 共享口令，和 BTCE 的 `QQBOT_SEND_TOKEN` 一致
- `QBOT_ADMIN_OPENIDS`：官方管理员 `member_openid`
- `QBOT_BOT_OPENID`：机器人自己的 openid（用于判断是否 @ 机器人）
- `QBOT_CALLBACK_FORWARD_URL = "http://127.0.0.1:15510/official/callback"`
- `QBOT_FORWARD_TOKEN`：可选，和 BTCE 的 `QQ_OFFICIAL_FORWARD_TOKEN` 一致

拿群 openid：

```bash
.venv39/bin/python qbot.py --discover-group
```

把机器人拉进群或在群里 @ 它，日志会打印 `group_openid`，再填进 `GROUP_OPENIDS`。
群别名可以自己起（例如测试群用 `test1`），后续也可以在管理群用管理指令改名。

鉴权与启动：

```bash
.venv39/bin/python qbot.py --check-auth
.venv39/bin/python qbot.py
```

qbot 的配置、队列和别名持久化都在 `qbot/config_custom.py`、`qbot/data/`、`qbot/logs/`，
均已 gitignore，不要提交，也不要在部署时覆盖。

## 5. 启动 BTCE

前台试跑：

```bash
.venv/bin/python main.py
```

pm2：

```bash
pm2 start main.py --name bili-monitor --interpreter .venv/bin/python
pm2 start qbot/qbot.py --name ttkj-qbot --interpreter qbot/.venv39/bin/python
pm2 save
pm2 startup   # 按提示执行输出的命令，让 pm2 开机自启
```

> `pm2 start qbot/qbot.py` 时，Python 会把 `qbot/` 所在目录加入模块搜索路径，
> `import config` 会用 `qbot/config.py`，不会和 BTCE 根目录的 `config.py` 冲突。
> 也可以在 `qbot/` 目录内启动：`cd qbot && .venv39/bin/python qbot.py`。

## 6. NapCat（可选）

NapCat 不在本仓库内，默认关闭。只有需要旧 QQ 通道或回滚时才部署。
大体步骤：

1. 按 NapCat 官方文档安装并登录一个 QQ 小号/机器人号。
2. 在 BTCE `config_qq.py` 设置：
   ```python
   QQ_NAPCAT_ENABLED = True
   QQ_BOT_API_URL = "http://127.0.0.1:8080"
   QQ_BOT_ACCESS_TOKEN = "..."     # 与 NapCat HTTP 配置一致
   QQ_GROUP_IDS = ["..." ]          # QQ 群号
   QQBOT_SEND_TOKEN = ""
   ```
3. 把 NapCat 的事件回调指向 BTCE：
   `http://127.0.0.1:15510/napcat/callback`
4. 重启 BTCE，确认日志没有 8080 连接失败。

QQ 客户端大版本会被腾讯强制更新，版本过低时会拒绝登录；升级 NapCat/QQ 后需要重新扫码登录。
NapCat 使用 Xvfb 时还要确认无头显示和字体正常。

## 7. 验证清单

1. `pm2 list`：`bili-monitor`、`ttkj-qbot` 均 online。
2. qbot 健康：`curl http://127.0.0.1:8089/health` 返回 `code=0`，群别名正确。
3. BTCE 日志：启动无 `TypeError`，浏览器初始化成功，第一轮置顶抓取成功。
4. 官方群发 `@bot 状态`，确认被动回复和群别名。
5. 管理群发 `@bot 修改别名 <当前别名> <新别名>`，确认改名后 `@bot 官方列表` 同步。
6. 向测试群发一条测试卡片（只发测试群），确认标题、描述、图片、底部站点名正常；
   再把 `pic_url` 换成不可用地址，确认 3 次重试后自动补发纯文本。
7. 邮件测试：配置好后改一次置顶评论或发测试邮件，确认 `sent_emails/` 有备份。
8. GitHub 归档：先 `GITHUB_ARCHIVE_DRY_RUN=True` 验证，再关闭 dry-run。
9. B站自动发布：确认 `AUTO_PUBLISH_ENABLED`、话题 ID/名称和 Cookie 有效。

## 8. 运维

- 修改配置后重启：`pm2 restart bili-monitor`、`pm2 restart ttkj-qbot`。
- 日志：`pm2 logs bili-monitor`、`pm2 logs ttkj-qbot`；BTCE 应用日志在根目录 `logs/`。
- 归档队列：`data/` 下的归档队列会在进程启动时补处理；不要手动删除未完成队列。
- 部署只覆盖代码文件，不要覆盖 `config_qq.py`、`config_custom.py`、`config_email.py`、
  `config_github.py`、`cookies.json`、`qbot/config_custom.py`、`data/`、`logs/`。
- 备份：升级前备份上述配置和 `qbot/data/`。

## 9. 已知限制

- Playwright 常驻内存较高，2C/2G 小机器建议加 swap，并关注 `dmesg | grep -i oom`。
- B站 Cookie 会过期；置顶评论、动态 API、自动发布都依赖有效 Cookie。
- 官方图文卡片的描述在部分客户端会视觉截断，新动态/直播只放短模板；
  置顶评论仍走完整文本+图片。
- B站 App 的粉色分享卡片（Ark 分享模板）无法用 `msg_type=8` 复刻，卡片底部只显示站点名。
- `QQ_OFFICIAL_WAIT_SECONDS=0` 时，qbot 队列内的异步发送失败由 qbot 重试；
  v5.5 起卡片重试后仍失败会用 `fallback_text` 补发纯文本。
  无配图的动态/直播不再发卡片，直接走文本（动态另带本地截图）。
- NapCat 不是官方支持通道，QQ 版本升级可能导致掉线，需要预留回滚时间。
