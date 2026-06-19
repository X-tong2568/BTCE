# BTCE 4.7 — B站动态/直播监控系统

基于 Python + Playwright 的 Bilibili UP 主动态和直播自动化监控系统，支持多通道实时通知及自动发布动态。

![BTCE 架构图](mermaid-diagram.png)

## 版本演进

| 版本 | 主要变更 |
|------|---------|
| v1.0 | 初始版本：Playwright 抓取置顶评论 + 邮件/QQ 通知 |
| v2.0 | 云端部署版（BTCE2.0） |
| v3.0 | 本地重构版 |
| v4.0 | 架构升级：API 动态列表 + 手动配置置顶 ID + 新动态卡片截图 + 双通道推送 |
| v4.1 | 置顶评论截图推送：高DPI `#comment` 元素截图，替换文字+表情+图片，失败兜底旧格式 |
| v4.2 | 自动发布B站动态：置顶评论变更时自动发图文动态（话题+截图+跳转链接），config开关控制 |
| v4.3 | 三通道推送模式可配置：QQ/邮件/B站各自可选 text/screenshot 模式，截图延迟到B站发布不阻塞通知 |
| **v4.4** | QQ群 @机器人 指令实时更换置顶动态ID：NapCat HTTP回调 + 权限校验 + 持久化+内存即时生效 |
| **v4.5** | 修复 B站旧版 API 下线（404），更换 polymer 新版接口；API 独立健康统计+P1/P2告警+日报双通道 |
| **v4.6** | 直播通知附加房间状态标签：room_init API 补充 encrypted/锁房/隐藏/付费/拜年纪，消息中标记 🔒密码保护 等 |
| **v4.7** | 管理群独立 + Cookie远程更新（@机器人指令→邮箱二维码→扫码→自动保存）+ 测试指令增强（轮次/成功率展示） |

## v4.7 更新

- **管理群独立**：新增 `QQ_MANAGEMENT_GROUP_IDS` 配置，管理群只接收 `@机器人` 指令，不接收监控推送。推送群+管理群双列表控制命令权限
- **Cookie 远程更新**：`@机器人 更新凭证` → B站 QR 登录 API 生成二维码 → 邮件内嵌二维码 → 管理员扫码 → 自动保存 `cookies.json` + 热重启浏览器加载新凭证。全程 HTTP API，无需本地浏览器
- **测试指令增强**：`@机器人 测试/状态` 回复增加当前轮次、已运行时间、置顶抓取成功率、API 成功率、上次凭证更新时间
- **新增 `cookie_renewer.py`**：独立模块，B站 `generate`/`poll` 双 API + `qrcode` 库 + MIME 邮件内嵌图片，`asyncio.to_thread` 隔离阻塞操作
- **`monitor.py` 新增 `restart_browser()`**：无条件重启浏览器，凭证更新后热加载新 cookie
- **`qq_callback_server.py` 扩展**：`更新凭证/测试/状态` 三条指令 + `_ALLOWED_CALLBACK_GROUPS` 推送群+管理群联合白名单 + 后台 `asyncio.create_task` 执行凭证更新
- **QQ 测试群号规范**：btce-dev skill 约定测试消息统一发 `1073571216`（管理群），禁止使用生产群

### XTong 的贡献
- **需求设计**：Cookie 远程更新全流程设计（QQ指令→邮件二维码→手机扫码→自动保存）、管理群独立方案、测试指令信息展示
- **交互评审**：邮件 vs QQ 群发二维码的方案选择、管理群隔离逻辑确认、凭证更新时间展示

### Claude (AI Assistant) 的贡献
- **cookie_renewer.py**：B站 QR 登录双 API 集成、`urllib` + `http.cookiejar` 自动捕获 Set-Cookie、`qrcode` 库本地生成二维码、`MIMEMultipart(related)` 邮件内嵌图片、旧 cookie 中设备指纹保留（buvid3/bili_ticket 等合并写回）
- **qq_callback_server.py**：`更新凭证/测试/状态` 三条指令、`asyncio.create_task` 后台异步执行、`restart_browser()` 调用热加载
- **monitor.py**：`restart_browser()` 无条件重启方法
- **config_qq.example.py**：`QQ_MANAGEMENT_GROUP_IDS` 配置模板

## v4.6 更新

- **直播通知附加房间状态标签**：新增调用 `room_init` API，获取 `encrypted`/`is_locked`/`is_hidden`/`special_type` 四个字段
- **状态标签展示**：加密房间 → 🔒密码保护、锁定 → 🔐房间已锁定、隐藏 → 👻房间已隐藏、付费 → 💰付费直播、拜年纪 → 🎊拜年纪
- **纯展示不触发通知**：标签仅作为已触发通知的附加信息（路线A），不产生独立的变更事件
- **失败无感兜底**：`room_init` 请求失败时字段默认为 `false`/`0`，不影响开播/下播检测主流程

### XTong 的贡献
- **场景发现与需求设计**：识别加密测试房误推直播通知的痛点，选择路线A（状态标签）而非路线B（独立事件）
- **方案评审**：确认文案格式（QQ/邮件），拍板改动范围

### Claude (AI Assistant) 的贡献
- **room_init API 集成**：`_enrich_room_init` 方法 + 双 API 兜底后补充调用，失败静默兜底
- **状态标签生成**：`_build_status_tags` 静态方法，5 种标签 `|` 拼接
- **QQ/邮件文案**：`generate_qq_message` / `format_email_content` 各追加标签行

## v4.5 更新

- **API 接口迁移**：B站旧版 `api.vc.bilibili.com/dynamic_svr/v1/dynamic_svr/space_history` 已下线（HTTP 404），更换为 `api.bilibili.com/x/polymer/web-dynamic/v1/feed/space`
- **仅需 Cookie**：新版 polymer 接口无需 WBI 签名，带 Cookie 即可正常调用
- **响应结构适配**：新版返回 `items[]` 结构（含 `id_str` / `modules.module_dynamic`），旧版 `cards[]` 解析逻辑完全替换
- **API 独立健康统计**：API 动态列表与置顶评论（Playwright）分开计数，各自独立追踪成功/失败/成功率
- **API 独立 P1/P2 告警**：P1 连续失败 ≥10 次告警（比浏览器 100 次更快响应），P2 成功率 <90% 告警（比浏览器 80% 更严格）
- **日报双通道**：性能报告邮件分两块展示「置顶评论监控 (Playwright)」和「API 动态列表 (urllib)」，各自含次数/成功率/连续失败/告警状态

### XTong 的贡献
- **问题发现与定位**：从 pm2 日志发现 API 404 错误，确认旧版接口已下线
- **需求设计**：API 独立健康统计（不与置顶评论混算）、独立 P1/P2 阈值（API 更快更严）、日报双通道展示
- **本地+云端测试**：本地模拟 P1/P2 告警触发 + 日报推送验证，云端部署验证

### Claude (AI Assistant) 的贡献
- **bili_api.py**：旧版 `api.vc.bilibili.com` → 新版 `api.bilibili.com` polymer 接口迁移，响应结构适配，`(list, bool)` 区分成败
- **health_check.py**：API 独立计数器（`api_success_count`/`api_failure_count`）+ `get_stats()` 双通道输出
- **performance_monitor.py**：API P1/P2 独立告警逻辑 + 告警邮件 HTML + 日报双通道分区
- **monitor.py**：API 健康统计接入 `run_monitoring_cycle`
- **config.py**：`API_P1_FAILURE_THRESHOLD` / `API_P2_SUCCESS_RATE_THRESHOLD` 独立阈值

## v4.4 更新

- **QQ群指令更换置顶**：在群里 `@Bot 更换置顶 <动态ID>` 即可实时更换，无需登录服务器改 config
- **qq_callback_server.py**：aiohttp HTTP 服务器，接收 NapCat 事件回调，解析指令
- **权限校验**：`QQ_ADMIN_USERS` 白名单控制，非授权用户指令被静默忽略
- **双重更新**：写入 config.py 持久化 + 调用 `monitor.update_pinned_dynamic_id()` 内存即时生效
- **回复精准**：只在指令来源群回复，不广播到其他群

### XTong 的贡献
- **需求与测试**：QQ群指令交互设计、NapCat HTTP客户端事件上报配置、本地+云端测试验证

### Claude (AI Assistant) 的贡献
- **qq_callback_server.py**：NapCat 事件解析、正则指令匹配、权限校验、config.py 文件写入
- **monitor.py**：`update_pinned_dynamic_id()` 运行时动态换ID
- **main.py**：回调服务器生命周期管理

## v4.3 更新

- **三通道推送模式可配置**：`QQ_MODE` / `EMAIL_MODE` / `BILI_MODE` 各可选 `"text"` 或 `"screenshot"`
  - `text`：QQ=纯文本+alt属性+评论区图片，邮件=文字+表情+评论区图片（快速不阻塞）
  - `screenshot`：截图内嵌（旧版行为，需等待截图）
  - B站特殊：`screenshot`=截图发布，`text`=跳过不发布
- **推送顺序优化**：默认 text 模式，QQ+邮件先推不等待截图，截图延迟到 B 站发布
- **截图逻辑提取**：`_take_pinned_comment_screenshot` 独立方法，按需调用避免重复截图
- **config 开关**：`QQ_MODE` / `EMAIL_MODE` / `BILI_MODE`，注释含三种切换示例

### XTong 的贡献
- **需求与测试**：三通道模式差异化需求（QQ纯文本、邮件图文、B站截图），推送顺序优化

### Claude (AI Assistant) 的贡献
- **monitor.py**：截图提取为独立方法、`_send_notification` 按模式分流、text模式先推不阻塞
- **config.py**：三通道开关配置 + 切换示例注释

## v4.2 更新

- **自动发布B站动态**：置顶评论变更时上传截图+发布带 `#TAG` 话题的图文动态，附跳转链接
- **auto_publish.py**：独立模块，B站图床上传 + 动态发布 API，异步调用不阻塞通知
- **config 开关**：`AUTO_PUBLISH_ENABLED` 控制功能启用/关闭，话题 ID 和名称可配

### XTong 的贡献
- **需求与测试**：自动发布功能设计、话题 ID 定位、发布效果验证

### Claude (AI Assistant) 的贡献
- **auto_publish.py**：B站图片上传 API + 动态发布 API 实现（multipart 上传 + JSON 组装 + CSRF 鉴权）
- **monitor.py**：`asyncio.create_task` 异步触发自动发布，不阻塞邮件/QQ 通知
- **config.py**：`AUTO_PUBLISH_ENABLED` / `AUTO_PUBLISH_TOPIC_ID` / `AUTO_PUBLISH_TOPIC_NAME` 配置项

## v4.1 更新

- **置顶评论截图推送**：检测到置顶评论变更时，独立高DPI context（`device_scale_factor=2`）截图 `<bili-comment-renderer id="comment">` 元素
- **截图替换旧格式**：QQ/邮件通知中用截图替换文字+表情alt+评论区图片，截图失败自动兜底旧文字格式

### XTong 的贡献
- **截图目标定位**：在浏览器 DevTools 中发现 `#comment` 元素天然隔离置顶评论卡片，无需 DOM 裁剪
- **截图范围验证**：确认子评论与置顶评论的 DOM 边界，简化截图方案
- **需求明确**：截图替代文字图片、失败兜底旧格式、高DPI 截图

### Claude (AI Assistant) 的贡献
- **monitor.py**：置顶评论高DPI截图（`device_scale_factor=2` + 独立context + cookies复制），截图失败不阻塞通知
- **email_renderer.py**：截图 base64 内嵌替换文字+图片区，失败兜底旧文字格式
- **qq_message_generator.py**：截图 CQ:image 替换文字+评论图片，失败兜底旧文字格式
- **render_comment.py**：`screenshot_path` 参数全链路透传（monitor → render → email/QQ）

## v4.0 新功能

### XTong 的贡献
- **核心需求设计与测试**：置顶评论监控逻辑、新动态检测方案、卡片截图 QQ 推送
- **多账户兼容测试**：发现并验证up大号 DOM 特殊性（data-did 属性差异）
- **通知格式设计**：邮件/QQ 推送样式规范，置顶评论 vs 新动态的标题区分
- **云端运维**：长期生产环境运行维护

### Claude (AI Assistant) 的贡献
- **架构重构**：从单一 URL 硬编码升级为 API 动态列表 + 手动置顶 ID 的混合架构
- **bili_api.py**：B站新版 polymer API 客户端，带 Cookie 获取动态列表（无需 WBI 签名，旧版 api.vc 已下线）
- **monitor.py 全重写**：分离新动态检测和置顶评论监控两条独立线路
- **历史记录按 dynamic_id 追踪**：消除置顶动态更换时的误报
- **卡片截图推送**：Playwright 截取动态卡片 → QQ 群图片推送
- **新动态批量通知**：API 差集检测 → 邮件/QQ 合并推送，冷启动静默记录
- **email_renderer.py / qq_message_generator.py**：批量通知的邮件 HTML 模板和 QQ 消息格式
- **live_monitor.py**：补全 close_session 方法

## 核心功能

1. **置顶评论监控** — Playwright 打开置顶动态，抓取置顶评论文字+图片，变化时推送邮件/QQ/B站；推送模式三通道可配；支持 QQ 群 @机器人 实时更换置顶动态ID
2. **新动态检测** — API 定时获取动态列表，差集比对发现新动态，卡片截图 QQ 推送
3. **直播监控** — 轮询 B站直播 API，开播/下播/标题变化即时通知
4. **多通道通知** — 邮件（HTML 格式）+ QQ 群（文字/CQ码图片/卡片截图）
5. **系统运维** — 健康检查、性能监控、日志轮转、浏览器自动重启、P1/P2 告警

## 项目结构

```
BTCE3.0/
├── main.py                    # 程序入口
├── monitor.py                 # 核心监控逻辑
├── auto_publish.py            # B站动态自动发布模块（v4.2）
├── bili_api.py                # B站动态列表 API 客户端
├── live_monitor.py            # 直播状态监控
├── monitor_scheduler.py       # 直播监控调度器
├── render_comment.py          # 评论渲染与变化检测
├── email_renderer.py          # 邮件 HTML 模板
├── email_utils.py             # SMTP 邮件发送
├── qq_message_generator.py    # QQ 消息生成
├── qq_utils.py                # QQ 机器人推送
├── qq_callback_server.py      # QQ回调服务器（v4.4+: @机器人指令）
├── cookie_renewer.py           # Cookie远程更新（v4.7: 邮件二维码→扫码→自动保存）
├── color_config.py            # 邮件渐变色配置
├── config.py                  # 主配置（含 PINNED_DYNAMIC_ID）
├── config_email.example.py    # 邮箱配置模板
├── config_qq.example.py       # QQ配置模板
├── dynamic.py                 # 监控目标列表
├── health_check.py            # 健康检查
├── performance_monitor.py     # 性能监控
├── status_monitor.py          # 状态监控
├── self_monitor.py            # 直播失败计数
├── retry_decorator.py         # 重试装饰器
├── logger_config.py           # 日志配置
├── get_cookies.py             # Cookie 获取工具
├── requirements.txt           # Python 依赖
└── .gitignore                 # Git 忽略规则
```

## 快速开始

### 1. 环境准备
```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. 获取 Cookie
```bash
python get_cookies.py
```
或手动从浏览器导出 Cookie 保存为 `cookies.json`。

### 3. 配置
```bash
cp config_email.example.py config_email.py
cp config_qq.example.py config_qq.py
```
编辑配置文件，填入真实 SMTP 和 QQ 机器人信息。

在 `config.py` 中设置：
- `UP_NAME` / `UP_UID`：监控的 UP 主
- `PINNED_DYNAMIC_ID`：要监测评论的置顶动态 ID

### 4. 运行
```bash
python main.py
```

后台运行（Linux）：
```bash
pm2 start main.py --name bili-monitor --interpreter python3
```

## 配置说明

| 配置项 | 位置 | 说明 |
|--------|------|------|
| 监控目标 | `dynamic.py` `MONITOR_LIST` | UID + 名称 |
| 置顶动态 ID | `config.py` `PINNED_DYNAMIC_ID` | 手动配置，换置顶时修改 |
| 推送模式 | `config.py` `QQ_MODE`/`EMAIL_MODE`/`BILI_MODE` | text=文字+图片, screenshot=截图 |
| 检查间隔 | `config.py` `CHECK_INTERVAL` | 默认 8 秒 |
| 邮箱 | `config_email.py` | SMTP + 收发人 |
| QQ 推送 | `config_qq.py` | 机器人 API + 群号 |
| 浏览器参数 | `config.py` `BROWSER_CONFIG` | headless 模式 |

## 注意事项

- Cookie 约 7 天失效，需定期更新
- 置顶动态更换时需手动更新 `PINNED_DYNAMIC_ID`
- 请勿将 `config_email.py`、`config_qq.py`、`cookies.json` 提交到公开仓库
