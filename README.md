# BTCE 4.1 — B站动态/直播监控系统

基于 Python + Playwright 的 Bilibili UP 主动态和直播自动化监控系统，支持多通道实时通知。

## 版本演进

| 版本 | 主要变更 |
|------|---------|
| v1.0 | 初始版本：Playwright 抓取置顶评论 + 邮件/QQ 通知 |
| v2.0 | 云端部署版（BTCE2.0） |
| v3.0 | 本地重构版 |
| v4.0 | 架构升级：API 动态列表 + 手动配置置顶 ID + 新动态卡片截图 + 双通道推送 |
| **v4.1** | 置顶评论截图推送：高DPI `#comment` 元素截图，替换文字+表情+图片，失败兜底旧格式 |

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
- **bili_api.py**：B站旧版 API 客户端，带 Cookie 获取动态列表（无需 WBI 签名）
- **monitor.py 全重写**：分离新动态检测和置顶评论监控两条独立线路
- **历史记录按 dynamic_id 追踪**：消除置顶动态更换时的误报
- **卡片截图推送**：Playwright 截取动态卡片 → QQ 群图片推送
- **新动态批量通知**：API 差集检测 → 邮件/QQ 合并推送，冷启动静默记录
- **email_renderer.py / qq_message_generator.py**：批量通知的邮件 HTML 模板和 QQ 消息格式
- **live_monitor.py**：补全 close_session 方法

## 核心功能

1. **置顶评论监控** — Playwright 打开手动配置的置顶动态，抓取置顶评论文字+图片，变化时截图推送邮件/QQ，截图失败兜底文字格式
2. **新动态检测** — API 定时获取动态列表，差集比对发现新动态，卡片截图 QQ 推送
3. **直播监控** — 轮询 B站直播 API，开播/下播/标题变化即时通知
4. **多通道通知** — 邮件（HTML 格式）+ QQ 群（文字/CQ码图片/卡片截图）
5. **系统运维** — 健康检查、性能监控、日志轮转、浏览器自动重启、P1/P2 告警

## 项目结构

```
BTCE3.0/
├── main.py                    # 程序入口
├── monitor.py                 # 核心监控逻辑
├── bili_api.py                # B站动态列表 API 客户端
├── live_monitor.py            # 直播状态监控
├── monitor_scheduler.py       # 直播监控调度器
├── render_comment.py          # 评论渲染与变化检测
├── email_renderer.py          # 邮件 HTML 模板
├── email_utils.py             # SMTP 邮件发送
├── qq_message_generator.py    # QQ 消息生成
├── qq_utils.py                # QQ 机器人推送
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
| 检查间隔 | `config.py` `CHECK_INTERVAL` | 默认 8 秒 |
| 邮箱 | `config_email.py` | SMTP + 收发人 |
| QQ 推送 | `config_qq.py` | 机器人 API + 群号 |
| 浏览器参数 | `config.py` `BROWSER_CONFIG` | headless 模式 |

## 注意事项

- Cookie 约 7 天失效，需定期更新
- 置顶动态更换时需手动更新 `PINNED_DYNAMIC_ID`
- 请勿将 `config_email.py`、`config_qq.py`、`cookies.json` 提交到公开仓库
