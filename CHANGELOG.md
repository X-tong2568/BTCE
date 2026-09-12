# Changelog

BTCE 各版本详细变更记录。简表见 [README.md](README.md#版本演进)。

## v4.14 — 置顶动态自动发现+手动/自动双模式 (2026-07)

- **置顶动态自动发现**：通过 API `modules.module_tag.text` 字段识别置顶，替换手动配置 `PINNED_DYNAMIC_ID`
- **`pinned_dynamic_monitor.py`**：纯检测模块，API 发现置顶+对比 state 文件+返回变更结果；不负责邮件/截图
- **手动/自动双模式**：`pinned_dynamic_state.json` 持久化 `mode` 字段；手动模式 API 照查但不自动切换，自动模式更换时自动跟进
- **QQ 回调控制**：`@机器人 切换手动`/`切换自动` 实时切换；`@机器人 更换置顶 <id>` 自动切手动；`测试` 指令显示 B站置顶 vs 监测动态两条
- **置顶更换通知**：自动模式下检测到更换→立即更新监控目标→全页截图→gradient 卡片邮件（base64 内嵌）→通知 `STATUS_MONITOR_EMAILS`
- **`bili_api.py` 重构**：新增 `_fetch_raw()`（含完整 modules）+ `get_dynamics()` 精简版新增 `module_tag` 字段
- **`monitor.py`**：新增 `_take_dynamic_screenshot()` 公用截图方法；主循环第3步定期检测（`PINNED_CHECK_INTERVAL` 间隔）+ 更换后截图邮件流程
- **config_custom 配置分离**：新增 `config_custom.example.py` 模板；个性化文案（QQ消息、邮件标题）走 config 变量 + `config_custom.py` 覆盖，Git 存通用默认，部署不误伤

### XTong 的贡献
- **需求与设计**：置顶动态更换自动检测 → 手动/自动双模式 → QQ 回调控制 → 截图邮件通知，全流程设计

### Claude (AI Assistant) 的贡献
- **编码实现**：全模块编写与重构 (`pinned_dynamic_monitor.py`、`bili_api.py`、`monitor.py`、`qq_callback_server.py`)；`config_custom.py` 配置分离方案设计与实现；部署与服务器验证

### v4.14 补丁 — 手动模式自动恢复 + api_pinned_id 同步修复 (2026-08-03)

- **手动模式自动恢复**：`pinned_dynamic_monitor.py` 手动模式下每1h照查API，若B站实际置顶ID与手动设定的ID一致（说明用户操作已生效），自动调用 `set_mode_auto()` 切回自动模式，返回 `auto_restored=True`
- **条件严格**：仅当 `api_pinned_id` 和 `monitored_id` 都非空且相等时触发；不一致或API失败不触发
- **邮件通知**：`monitor.py` 收到 `auto_restored` 后发邮件到 `STATUS_MONITOR_EMAILS`，模板复用置顶更换邮件的 `ColorConfig` 渐变样式（标题「置顶监测已恢复自动模式」，含恢复时间+当前动态链接+原因说明）
- **`api_pinned_id` 同步修复**：`update_pinned_dynamic_id()`（QQ回调更换置顶）漏更新 `self.api_pinned_id`，导致 `@bot 状态` 消息中「B站置顶」行显示旧ID。现增加 `self.api_pinned_id = new_id`，两条状态行实时一致

### 设计逻辑（备忘）

```
手动模式下的 check_pinned_dynamic() 每1h运行:

   API查到置顶ID → 跟 state 里的 monitored_id 比
   ├── api_id == monitored_id  → set_mode_auto() + auto_restored=True → monitor.py发邮件
   ├── api_id != monitored_id  → 只打日志「不自动切换」，保持manual
   └── api_id is None          → 只打日志，保持manual

自动模式（原有逻辑不变）:
   ├── api_id != monitored_id  → 自动切换 + 截图 + 邮件通知
   └── api_id == monitored_id  → 无操作
```

### XTong 的贡献
- **需求与设计**：手动模式API一致时自动恢复自动模式 + 邮件通知 + 修复状态消息旧ID问题

### Claude (AI Assistant) 的贡献
- **编码实现**：`pinned_dynamic_monitor.py` 手动模式自动恢复逻辑；`monitor.py` 邮件通知+`api_pinned_id` 同步修复；本地双向测试验证；服务器部署

### v4.14 补丁 — QQ消息发送重试降级修复 (2026-08-19)

- **事故背景**：8-19 晚动态推送三群并发带图消息，NapCat 的 sendMsg IPC 超时（约10s），动态消息丢失，群友未收到
- **根因**：服务器 `qq_utils.py` 为旧版（`max_retries` 恒为1，无重试）；动态推送路径（`monitor.py`）未传 `message_data`，无降级兜底
- **重试策略调整**：`qq_utils.py` 原始消息只发 1 次，失败后不重发相同内容；失败群等待 10s（NapCat sendMsg 超时窗口）后补发纯文本降级消息——共 2 次尝试且第 2 次必为降级消息，避免重复推送两条相同消息
- **动态推送接降级**：`monitor.py` 新动态批量推送补传 `message_data`，图片超时时降级兜底，消息不丢失
- **新增常量**：`qq_utils.py` 模块常量 `DEGRADED_RETRY_DELAY_SECONDS = 10`，降级消息延迟发送窗口
- **动态推送降级文案**：`qq_message_generator.py` 新增 `generate_new_dynamic_degraded_message()`，动态推送降级走独立文案（开头「发布了新动态~」+「消息推送超时，请自行查看」提示，不含动态正文）；置顶评论降级保持原文案（`config_custom.py` 个性化文案变量+正文提取）

### XTong 的贡献
- **需求与设计**：定位 NapCat sendMsg 超时问题，设计「重试那次必须是降级消息、不能两条一样」策略，明确测试只发测试群

### Claude (AI Assistant) 的贡献
- **编码实现**：`qq_utils.py` 重试策略调整+降级延迟常量；`monitor.py` 动态推送补传 `message_data`；服务器日志排查（定位旧版无重试根因）；部署与验证

### v4.14 补丁 — 置顶更换截图范围修复 (2026-09-01)

- **修复**：置顶动态更换邮件的内嵌截图从整页截图（`full_page=True`，会截到动态卡片下方的评论区）改为只截动态卡片本身——定位 `.bili-dyn-item, [class*="dyn-card"]` 元素截图，与普通新动态通知的截图方式一致
- **兜底**：未找到动态卡片元素时返回 None，邮件展示「截图加载失败」提示，不影响通知流程

### v4.14 补丁 — 直播动态封面提示条件化 (2026-09-13)

- **修复**：`auto_publish.py` 直播间标题更新动态中，「当前封面：」文案原先固定拼装，封面图下载/上传失败降级为纯文本动态时仍会发出，形成无图空标签
- **改动**：`publish_live_update()` 的 contents 组装改为封面图上传成功（`img` 非空）才追加「当前封面：」条目，图文要么都有、要么都没有

## v4.13 — 动态类型过滤，排除直播自动动态 (2026-07)

- **动态类型过滤**：新增 `DYNAMIC_SKIP_TYPES` 配置项，可排除 B站 自动生成的动态类型
- **默认过滤 `DYNAMIC_TYPE_LIVE_RCMD`**：开播时B站自动生成的直播推荐动态（下播后自动消失），不再触发 QQ/邮件/B站 三通道推送
- **`monitor.py` 过滤逻辑**：API 动态列表检测时增加 `type` 字段判断，命中跳过类型的动态静默加入 `seen` 但不通知
- **可扩展**：`DYNAMIC_SKIP_TYPES` 为集合类型，后续需过滤其他自动动态直接添加类型名即可

### XTong 的贡献
- **需求与设计**：发现问题（开播后B站自动生成的直播动态误推），提出过滤排除需求

### Claude (AI Assistant) 的贡献
- **编码实现**：`config.py` 新增 `DYNAMIC_SKIP_TYPES` 配置；`monitor.py` 导入+过滤逻辑+跳过日志；文档更新与部署

## v4.12 — 邮件HTML统一备份 (2026-07)

- **`send_email()` 内统一备份**：所有邮件发送前自动保存HTML到 `sent_emails/` 目录，覆盖此前遗漏的直播通知和告警邮件
- **移除冗余保存点**：`monitor.py` 中新动态批量通知和置顶评论更新两处手动写文件移除，统一走 `send_email()` 备份
- **备份格式**：`sent_emails/{YYYYMMDD_HHMMSS}_{主题}.html`，SMTP发送前写入确保即使发送失败也有留档

### XTong 的贡献
- **需求与设计**：提出邮件备份需求，便于后续数据分析

### Claude (AI Assistant) 的贡献
- **编码实现**：`email_utils.py` 新增 `_save_email_backup()` 函数；`monitor.py` 冗余保存点清理；文档更新与部署

## v4.11 — B站发布附加评论纯文本+去重标记 (2026-07)

- **动态末尾附加评论纯文本**：B站置顶评论更新动态自动附上评论纯文本（与QQ推送一致：emoji img替换为alt文字）
- **MD5去重标记**：`MD5(评论文本+时间戳)` 取前16位追加到动态末尾，避免B站判重拦截
- **仅影响置顶评论发布**：`publish_dynamic()` 新增 `comment_text` 参数，直播间标题发布 (`publish_live_update`) 不变
- **monitor.py 联动**：`_send_notification()` 从 `cur_html` 提取纯文本，传入B站发布流程

### XTong 的贡献
- **需求与设计**：提出B站动态附加评论纯文本+MD5去重标记需求

### Claude (AI Assistant) 的贡献
- **编码实现**：`publish_dynamic()` 新增 `comment_text` 参数与MD5去重逻辑；`monitor.py` 纯文本提取与传递；文档更新与部署

## v4.10 — 直播标题更新同步B站话题 (2026-07)

- **直播标题更新自动发布B站动态**：标题变化时同步发布图文动态到B站话题
- **动态内容**：封面图 + 新标题 + 更新时间 + 直播间链接 + 房间状态标签（加密/锁定/隐藏/付费/拜年纪）
- **容错降级**：封面下载/上传失败自动降级为纯文本动态，不阻塞QQ/邮件通知
- **可配置开关**：`LIVE_BILI_PUBLISH_ENABLED` 独立控制，复用已有话题配置
- **新增 `auto_publish.py` 函数**：`_download_cover_image()` 下载封面到本地、`publish_live_update()` 完整发布流程
- **`monitor_scheduler.py` 扩展**：`send_live_notification()` 新增B站动态发布通道，fire-and-forget 不阻塞

### XTong 的贡献
- **需求与设计**：提出直播标题更新同步B站话题需求，指定动态内容格式（封面+标题+时间+链接+状态标签）

### Claude (AI Assistant) 的贡献
- **编码实现**：新增 `_download_cover_image()`、`publish_live_update()` 函数；扩展 `monitor_scheduler.py` B站发布通道；容错降级逻辑；文档更新与部署

## v4.9 — 评论直达链接 (2026-07)

- **评论直达链接**：穿透 B站 Web Component 三层 Shadow DOM 提取 `rpid`（评论ID），B站推送新增 `#reply{rpid}` 直达链接
- **双链接推送**：`💻跳转链接`（评论直达）+ `📱跳转链接`（动态页），替代原来被 B站 自动转"网页链接"卡片的方式
- **状态显示当前置顶**：`@机器人 测试` 回复新增 `🔗 当前置顶: https://t.bilibili.com/{ID}`

## v4.8 — 性能优化 (2026-07)

- **页面复用**：`check_dynamic_changes` 持久化复用 Playwright 页面，不再每轮 `new_page()` + `close()`，省 ~1s/轮
- **滚动优化**：触发懒加载的滚动从 5次×1s 降为 3次×0.5s，省 ~3.5s/轮
- **检查间隔缩短**：`CHECK_INTERVAL` 8→6s，配合页面复用后 3s 工作耗时留足缓冲
- **浏览器重启间隔**：`BROWSER_RESTART_INTERVAL` 10→100 轮，减少重启开销
- **综合效果**：单轮总周期 8s→6s，8000 轮从 19.4h→13.3h（-31%）

## v4.7 — Cookie 远程更新 (2026-06)

- **管理群独立**：新增 `QQ_MANAGEMENT_GROUP_IDS` 配置，管理群只接收 `@机器人` 指令，不接收监控推送
- **Cookie 远程更新**：`@机器人 更新凭证` → B站 QR 登录 API 生成二维码 → 邮件内嵌二维码 → 管理员扫码 → 自动保存 `cookies.json` + 热重启浏览器
- **测试指令增强**：`@机器人 测试/状态` 回复增加当前轮次、运行时间、抓取成功率、API 成功率、凭证更新时间
- **新增 `cookie_renewer.py`**：独立模块，B站 `generate`/`poll` 双 API + `qrcode` 库 + MIME 邮件内嵌图片
- **`monitor.py` 新增 `restart_browser()`**：无条件重启浏览器，凭证更新后热加载新 cookie
- **`qq_callback_server.py` 扩展**：`更新凭证/测试/状态` 三条指令 + 推送群+管理群联合白名单

## v4.6 — 直播房间状态标签 (2026-05)

- **直播通知附加房间状态标签**：新增调用 `room_init` API，获取 `encrypted`/`is_locked`/`is_hidden`/`special_type` 四个字段
- **状态标签展示**：加密 → 🔒密码保护、锁定 → 🔐房间已锁定、隐藏 → 👻房间已隐藏、付费 → 💰付费直播、拜年纪 → 🎊拜年纪
- **纯展示不触发通知**：标签仅作为已触发通知的附加信息（路线A），不产生独立的变更事件
- **失败无感兜底**：`room_init` 请求失败时字段默认为 `false`/`0`，不影响开播/下播检测主流程

## v4.5 — API 接口迁移 (2026-04)

- **API 接口迁移**：B站旧版 `api.vc.bilibili.com` 已下线（HTTP 404），更换为 `api.bilibili.com/x/polymer/web-dynamic/v1/feed/space`
- **仅需 Cookie**：新版 polymer 接口无需 WBI 签名
- **响应结构适配**：新版返回 `items[]` 结构（含 `id_str` / `modules.module_dynamic`）
- **API 独立健康统计**：API 动态列表与置顶评论（Playwright）分开计数
- **API 独立 P1/P2 告警**：P1 连续失败 ≥10 次，P2 成功率 <90%
- **日报双通道**：性能报告分「置顶评论监控 (Playwright)」和「API 动态列表 (urllib)」两块

## v4.4 — QQ 群 @机器人 指令 (2026-03)

- **QQ群指令更换置顶**：在群里 `@Bot 更换置顶 <动态ID>` 实时更换，无需登录服务器改 config
- **qq_callback_server.py**：aiohttp HTTP 服务器，接收 NapCat 事件回调
- **权限校验**：`QQ_ADMIN_USERS` 白名单控制
- **双重更新**：写入 config.py 持久化 + 内存即时生效

## v4.3 — 三通道推送模式可配置 (2026-02)

- **三通道推送模式**：`QQ_MODE` / `EMAIL_MODE` / `BILI_MODE` 各可选 `"text"` 或 `"screenshot"`
  - `text`：纯文本+图片（快速不阻塞）
  - `screenshot`：截图内嵌（B站截图发布）
- **推送顺序优化**：默认 text 模式，QQ+邮件先推不等待截图
- **截图逻辑提取**：`_take_pinned_comment_screenshot` 独立方法

## v4.2 — 自动发布B站动态 (2026-01)

- **自动发布B站动态**：置顶评论变更时上传截图+发布带 `#TAG` 话题的图文动态
- **auto_publish.py**：独立模块，B站图床上传 + 动态发布 API，异步调用不阻塞通知
- **config 开关**：`AUTO_PUBLISH_ENABLED` 控制功能启用/关闭

## v4.1 — 置顶评论截图推送 (2025-12)

- **置顶评论截图推送**：高DPI context（`device_scale_factor=2`）截图 `#comment` 元素
- **截图替换旧格式**：QQ/邮件通知中用截图替换文字+表情+图片，失败自动兜底旧文字格式

## v4.0 — 架构升级

- **架构重构**：从单一 URL 硬编码升级为 API 动态列表 + 手动置顶 ID 的混合架构
- **bili_api.py**：B站新版 polymer API 客户端，带 Cookie 获取动态列表
- **monitor.py 全重写**：分离新动态检测和置顶评论监控两条独立线路
- **历史记录按 dynamic_id 追踪**：消除置顶动态更换时的误报
- **卡片截图推送**：Playwright 截取动态卡片 → QQ 群图片推送
- **新动态批量通知**：API 差集检测 → 邮件/QQ 合并推送，冷启动静默记录

## v3.0 — 本地重构版

## v2.0 — 云端部署版（BTCE2.0）

## v1.0 — 初始版本

Playwright 抓取置顶评论 + 邮件/QQ 通知。

---

## 待优化

### rpid 改用 B站公开 API 获取

**现状**（v4.9 fix）：从 Web Component 内部属性 `__data.rpid` 获取，依赖 Playwright 打开的页面。

**方案**：通过 B站公开 API 直接获取，无需登录、无需打开页面。

```python
# 1. 获取评论线程ID（oid）
async def get_comment_oid(dynamic_id: str) -> str:
    url = f"https://api.bilibili.com/x/polymer/web-dynamic/v1/detail?id={dynamic_id}"
    async with aiohttp.ClientSession() as s:
        r = await s.get(url)
        data = await r.json()
        return data["data"]["item"]["basic"]["comment_id_str"]

# 2. 用 oid 获取置顶评论rpid
async def get_rpid(dynamic_id: str) -> str | None:
    oid = await get_comment_oid(dynamic_id)
    url = f"https://api.bilibili.com/x/v2/reply/wbi/main?oid={oid}&type=11&mode=3"
    async with aiohttp.ClientSession() as s:
        r = await s.get(url)
        data = await r.json()
        top = data["data"].get("top_replies")
        return top[0]["rpid_str"] if top else None
```

**优点**：
- 不需要 Playwright、不需要 cookies
- API 响应始终包含 `rpid_str`，与评论是否带 emoji 无关
- 可并行调用（API 拿 rpid + Playwright 拿文字截图），互不阻塞

**代价**：
- 需要 WBI 签名（B站 API 防爬机制，已有实现可复用）
- 多 2 个 HTTP 请求（~1s）
- 仍需 Playwright 拿评论文字和截图（不能完全替代）
