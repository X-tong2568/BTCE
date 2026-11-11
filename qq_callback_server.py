# qq_callback_server.py
"""
NapCat HTTP 事件回调服务器（v4.4）。
监听 NapCat 上报的群消息事件，解析 @机器人 指令：
  - 更换置顶 <动态ID>  → 运行时替换置顶动态（自动切手动模式）
  - 切换手动 / 切换自动 → 切换置顶动态控制模式
  - 更新凭证           → 远程更新B站cookies（发二维码到邮箱）
  - 测试 / 状态        → 检查机器人运行状态
  - 帮助 / help / 命令 → 显示所有可用指令
"""

import re
import json
import time
import asyncio
from datetime import datetime
from pathlib import Path
from typing import Optional
import aiohttp
from aiohttp import web
from logger_config import logger
from config_qq import (
    QQ_BOT_API_URL, QQ_BOT_ACCESS_TOKEN, QQ_GROUP_IDS, QQ_MANAGEMENT_GROUP_IDS,
    QQ_BOT_QQ_ID, QQ_CALLBACK_PORT, QQ_CALLBACK_ENABLED, QQ_ADMIN_USERS,
    QQ_OFFICIAL_ADMIN_OPENIDS, QQ_OFFICIAL_FORWARD_TOKEN,
    QQBOT_REPLY_URL, QQBOT_SEND_URL, QQBOT_SEND_TOKEN,
)
from qq_utils import load_official_groups, set_official_group

from pinned_dynamic_monitor import set_mode_manual, set_mode_auto, get_mode

# cookie_renewer 可能因缺少 qrcode 库而导入失败（仅影响更新凭证功能）
try:
    import cookie_renewer
    _COOKIE_RENEWER_AVAILABLE = True
except ImportError as e:
    cookie_renewer = None
    _COOKIE_RENEWER_AVAILABLE = False

# 允许接收@机器人指令的群 = 推送群 + 管理群
_ALLOWED_CALLBACK_GROUPS = set(QQ_GROUP_IDS) | set(QQ_MANAGEMENT_GROUP_IDS)

# config.py 路径
CONFIG_PATH = Path(__file__).parent / "config.py"

# 全局 monitor 引用，由 main.py 在启动时注入
_monitor = None


def set_monitor(monitor):
    """注入 Monitor 实例引用（main.py 启动时调用）"""
    global _monitor
    _monitor = monitor


def _update_config_file(new_id: str) -> bool:
    """写入 config.py，替换 PINNED_DYNAMIC_ID 值"""
    try:
        content = CONFIG_PATH.read_text(encoding="utf-8")
        # 匹配 PINNED_DYNAMIC_ID = "旧值" 或 = ""，替换双引号内的值
        new_content = re.sub(
            r'PINNED_DYNAMIC_ID\s*=\s*"[^"]*"',
            f'PINNED_DYNAMIC_ID = "{new_id}"',
            content
        )
        if new_content == content:
            logger.warning("⚠️ config.py 中未找到 PINNED_DYNAMIC_ID，替换失败")
            return False
        CONFIG_PATH.write_text(new_content, encoding="utf-8")
        logger.info(f"📝 config.py PINNED_DYNAMIC_ID 已更新为 {new_id}")
        return True
    except Exception as e:
        logger.error(f"❌ 写入 config.py 失败: {e}")
        return False


async def _reply_to_group(group_id: str, message: str):
    """通过 NapCat HTTP API 回复群消息"""
    try:
        headers = {"Content-Type": "application/json"}
        if QQ_BOT_ACCESS_TOKEN:
            headers["Authorization"] = f"Bearer {QQ_BOT_ACCESS_TOKEN}"
        payload = {"group_id": group_id, "message": message, "auto_escape": False}
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{QQ_BOT_API_URL}/send_group_msg",
                json=payload, headers=headers, timeout=10
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    if result.get("status") == "ok":
                        logger.info(f"✅ 回调回复已发送到群 {group_id}")
                        return
                logger.warning(f"⚠️ 回调回复失败: HTTP {resp.status}")
    except Exception as e:
        logger.error(f"❌ 回调回复异常: {e}")


async def _reply_to_official_group(alias: str, msg_id: str, message: str, msg_seq: int = 1):
    """通过 qbot /official_reply 做被动回复；不占主动消息频次。"""
    if not msg_id:
        logger.warning("官方被动回复跳过：msg_id 为空 alias=%s", alias)
        return
    headers = {"Content-Type": "application/json"}
    if QQBOT_SEND_TOKEN:
        headers["Authorization"] = "Bearer " + QQBOT_SEND_TOKEN
    payload = {
        "group": alias,
        "msg_id": msg_id,
        "content": message,
        "msg_seq": msg_seq,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                QQBOT_REPLY_URL,
                json=payload,
                headers=headers,
                timeout=10,
            ) as response:
                if response.status == 200:
                    result = await response.json(content_type=None)
                    if result.get("code") == 0:
                        logger.info("✅ 官方被动回复已发送: alias=%s", alias)
                        return
                    logger.warning("⚠️ 官方被动回复失败: alias=%s result=%s", alias, result)
                    return
                logger.warning("⚠️ 官方被动回复失败: alias=%s HTTP %s", alias, response.status)
    except Exception as exc:
        logger.error("❌ 官方被动回复异常: alias=%s err=%s", alias, exc)


async def _send_official_active(alias: str, message: str):
    """官方主动消息：用于长流程（如更新凭证）的后续状态提示。"""
    text = str(message or "")
    if len(text) > 2000:
        text = text[:1997] + "..."
    headers = {"Content-Type": "application/json"}
    if QQBOT_SEND_TOKEN:
        headers["Authorization"] = "Bearer " + QQBOT_SEND_TOKEN
    payload = {
        "group": alias,
        "parts": [{"type": "text", "text": text}],
        "wait": False,
        "request_id": "btce_official_" + str(alias) + "_" + str(int(time.time() * 1000)),
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                QQBOT_SEND_URL,
                json=payload,
                headers=headers,
                timeout=10,
            ) as response:
                result = await response.json(content_type=None)
                if response.status == 200 and isinstance(result, dict) and result.get("code") == 0:
                    logger.info("✅ 官方主动消息已受理: alias=%s", alias)
                    return
                logger.warning("⚠️ 官方主动消息失败: alias=%s status=%s result=%s", alias, response.status, result)
    except Exception as exc:
        logger.error("❌ 官方主动消息异常: alias=%s err=%s", alias, exc)


async def _fetch_official_qbot_groups() -> dict:
    """读取 qbot /health 的 groups，作为官方群列表。"""
    base_url = QQBOT_SEND_URL.rsplit("/", 1)[0]
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                base_url + "/health",
                timeout=5,
            ) as response:
                data = await response.json(content_type=None)
        if isinstance(data, dict):
            payload = data.get("data") or {}
            groups = payload.get("groups") or {}
            if isinstance(groups, dict):
                return groups
    except Exception as exc:
        logger.warning("获取 qbot 官方群列表失败: %s", exc)
    return {}


async def _official_list_text(extra_aliases: Optional[list] = None) -> str:
    """生成官方群列表，✓=BTCE 正在推送，✗=未推送。"""
    enabled = set(load_official_groups())
    groups = await _fetch_official_qbot_groups()
    all_aliases = dict(groups)
    for alias in extra_aliases or []:
        if alias and alias not in all_aliases:
            all_aliases[alias] = {"source": "auto"}

    lines = []
    for alias, info in all_aliases.items():
        mark = "✓" if alias in enabled else "✗"
        source = info.get("source", "?") if isinstance(info, dict) else "?"
        lines.append(mark + " " + str(alias) + " (" + str(source) + ")")
    for alias in sorted(enabled - set(all_aliases.keys())):
        lines.append("✓ " + alias + " (qbot 未配置)")
    if not lines:
        lines.append("(暂无官方群)")
    return (
        "官方渠道列表（✓=推送中）:\n"
        + "\n".join(lines)
        + "\n操作: @bot 开启官方推送 <别名> / @bot 关闭官方推送 <别名>"
    )


async def _notify_management_group(alias: str):
    """官方 bot 加入新群时，通知共用管理群。"""
    text = (
        "官方 bot 加入新群\n"
        "alias: " + alias + "\n"
        + await _official_list_text([alias])
    )
    sent = False
    for group_id in QQ_MANAGEMENT_GROUP_IDS:
        if not group_id:
            continue
        await _reply_to_group(str(group_id), text)
        sent = True
    if not sent:
        logger.warning("官方 bot 加入新群，但未配置管理群: alias=%s", alias)


def _parse_command(message: str) -> Optional[str]:
    """
    解析消息中的更换置顶指令。
    格式: [CQ:at,qq=<bot_qq>] 更换置顶 <dynamic_id>
    返回 dynamic_id 或 None。
    """
    # 转义 bot QQ 号，构建 @提及 匹配
    bot_qq = re.escape(QQ_BOT_QQ_ID)
    # 匹配 [CQ:at,qq=<bot_qq>] 后面跟 更换置顶 + 动态ID
    pattern = rf'\[CQ:at,qq={bot_qq}[^\]]*\]\s*更换置顶\s+(\d{{15,20}})'
    match = re.search(pattern, message)
    if match:
        return match.group(1)
    return None


async def handle_callback(request: web.Request) -> web.Response:
    """处理 NapCat 事件回调 POST /napcat/callback"""
    try:
        data = await request.json()
    except Exception:
        return web.Response(text="invalid json", status=400)

    # 只处理群消息事件
    if data.get("post_type") != "message" or data.get("message_type") != "group":
        return web.json_response({"status": "ok"})

    group_id = str(data.get("group_id", ""))
    user_id = str(data.get("user_id", ""))
    message = data.get("message", "")
    sender_nick = data.get("sender", {}).get("nickname", "未知")

    # 只处理推送群+管理群的消息
    if group_id not in _ALLOWED_CALLBACK_GROUPS:
        return web.json_response({"status": "ok"})

    logger.info(f"📩 收到群消息: 群={group_id} 用户={user_id}({sender_nick}) 内容={message[:100]}")

    # 官方渠道管理（共用管理群）：必须 @ NapCat 机器人，避免把官方 bot 的消息当指令
    napcat_at = re.search(
        rf'\[CQ:at,qq={re.escape(QQ_BOT_QQ_ID)}[^\]]*\]',
        message,
    )
    if group_id in QQ_MANAGEMENT_GROUP_IDS and napcat_at:
        if re.search(r"(官方列表|官方推送列表)", message):
            if user_id not in QQ_ADMIN_USERS:
                await _reply_to_group(group_id, "❌ 你没有权限执行此操作")
            else:
                await _reply_to_group(group_id, await _official_list_text())
            return web.json_response({"status": "ok"})

        match_push = re.search(
            r"(开启|关闭)官方推送[\s:：,，]*([A-Za-z0-9_\-]+)?",
            message,
        )
        if match_push:
            if user_id not in QQ_ADMIN_USERS:
                await _reply_to_group(group_id, "❌ 你没有权限执行此操作")
                return web.json_response({"status": "ok"})
            action = match_push.group(1)
            target_alias = match_push.group(2)
            if not target_alias:
                await _reply_to_group(group_id, "请指定别名，例如: @bot 开启官方推送 auto_2")
                return web.json_response({"status": "ok"})
            if action == "开启":
                qbot_groups = await _fetch_official_qbot_groups()
                if qbot_groups and target_alias not in qbot_groups:
                    await _reply_to_group(group_id, "未知的官方群别名: " + target_alias)
                    return web.json_response({"status": "ok"})
            set_official_group(target_alias, action == "开启")
            await _reply_to_group(group_id, "已" + action + "官方推送: " + target_alias)
            return web.json_response({"status": "ok"})

    # 尝试解析更换置顶指令
    new_id = _parse_command(message)
    if new_id:
        # 权限检查
        if user_id not in QQ_ADMIN_USERS:
            await _reply_to_group(group_id, "❌ 你没有权限执行此操作")
            return web.json_response({"status": "ok"})

        # 更新 config.py 文件
        file_ok = _update_config_file(new_id)

        # 更新运行中的 Monitor
        if _monitor:
            _monitor.update_pinned_dynamic_id(new_id)

        if file_ok:
            await _reply_to_group(
                group_id,
                f"✅ 置顶动态ID已更换\n🔗 https://t.bilibili.com/{new_id}"
            )
        else:
            await _reply_to_group(
                group_id,
                "⚠️ 内存已更新但 config.py 写入失败，请检查日志"
            )
    # 模式切换指令：@bot 切换手动 / @bot 切换自动
    if re.search(rf'\[CQ:at,qq={re.escape(QQ_BOT_QQ_ID)}[^\]]*\]\s*切换手动', message):
        if user_id not in QQ_ADMIN_USERS:
            await _reply_to_group(group_id, "你没有权限执行此操作")
            return web.json_response({"status": "ok"})
        current_id = _monitor.pinned_dynamic_id if _monitor else "?"
        set_mode_manual()  # 保持当前ID不变，切到手动
        await _reply_to_group(group_id,
            f"已切换为手动模式\n当前置顶: https://t.bilibili.com/{current_id}")
        return web.json_response({"status": "ok"})

    if re.search(rf'\[CQ:at,qq={re.escape(QQ_BOT_QQ_ID)}[^\]]*\]\s*切换自动', message):
        if user_id not in QQ_ADMIN_USERS:
            await _reply_to_group(group_id, "你没有权限执行此操作")
            return web.json_response({"status": "ok"})
        set_mode_auto()
        await _reply_to_group(group_id,
            "已切换为自动模式\nAPI检测恢复后会自动同步B站置顶")
        return web.json_response({"status": "ok"})

    # 测试指令：@bot 测试 / @bot 状态
    if re.search(rf'\[CQ:at,qq={re.escape(QQ_BOT_QQ_ID)}[^\]]*\]\s*(测试|状态)', message):
        from datetime import datetime
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 读取 cookies.json 最后修改时间
        cookie_file = CONFIG_PATH.parent / "cookies.json"
        if cookie_file.exists():
            mtime = datetime.fromtimestamp(cookie_file.stat().st_mtime)
            last_cookie = mtime.strftime("%Y-%m-%d %H:%M:%S")
        else:
            last_cookie = "未知（文件不存在）"

        # 拼装监控运行状态
        if _monitor:
            stats = _monitor.health_checker.get_stats(total_loops=_monitor.loop_count)
            monitor_info = (
                f"🎛 模式: {'手动' if get_mode() == 'manual' else '自动'}\n"
                f"🔗 B站置顶: https://t.bilibili.com/{getattr(_monitor, 'api_pinned_id', _monitor.pinned_dynamic_id)}\n"
                f"📌 监测动态: https://t.bilibili.com/{_monitor.pinned_dynamic_id}\n"
                f"🔄 当前轮次: 第{_monitor.loop_count}轮\n"
                f"⏱ 已运行: {stats.get('重启后运行时间', '?')}\n"
                f"📊 置顶抓取: {stats.get('抓取成功率', '?')} ({stats.get('抓取成功次数', 0)}/{stats.get('抓取次数', 0)})\n"
                f"📡 API请求: 成功率{stats.get('API成功率', '?')} ({stats.get('API成功次数', 0)}/{stats.get('API请求次数', 0)})"
            )
        else:
            monitor_info = "⚠️ Monitor 实例未就绪"

        await _reply_to_group(
            group_id,
            f"✅ 机器人运行正常\n"
            f"🕐 服务器时间: {now}\n"
            f"{monitor_info}\n"
            f"📨 推送群: {len(QQ_GROUP_IDS)}个 | 🔧 管理群: {len(QQ_MANAGEMENT_GROUP_IDS)}个\n"
            f"👤 管理员: {len(QQ_ADMIN_USERS)}人\n"
            f"🍪 凭证更新: {'可用' if _COOKIE_RENEWER_AVAILABLE else '不可用(qrcode未安装)'}\n"
            f"🍪 上次更新: {last_cookie}"
        )
        return web.json_response({"status": "ok"})

    # 更新凭证指令：@bot 更新凭证（v4.7）
    if re.search(rf'\[CQ:at,qq={re.escape(QQ_BOT_QQ_ID)}[^\]]*\]\s*更新凭证', message):
        if user_id not in QQ_ADMIN_USERS:
            await _reply_to_group(group_id, "❌ 你没有权限执行此操作")
            return web.json_response({"status": "ok"})

        if not _COOKIE_RENEWER_AVAILABLE:
            await _reply_to_group(group_id, "❌ 凭证更新功能不可用：qrcode 库未安装")
            return web.json_response({"status": "ok"})

        # 立即回复，后台执行凭证更新（可能需要几分钟）
        await _reply_to_group(group_id, "🔐 正在生成登录二维码...")

        async def _do_renewal():
            """后台执行凭证更新流程"""
            async def status_cb(msg: str):
                """状态回调 → 推送到QQ群"""
                await _reply_to_group(group_id, msg)

            result = await cookie_renewer.run_cookie_renewal(
                on_status_update=status_cb
            )

            # 更新成功后重启浏览器以加载新cookie
            if result == "success" and _monitor:
                try:
                    await _monitor.restart_browser()
                    logger.info("🔄 浏览器已重启，新凭证已加载")
                except Exception as e:
                    logger.error(f"重启浏览器失败: {e}")

        asyncio.create_task(_do_renewal())
        return web.json_response({"status": "ok"})

    # 帮助指令：@bot 帮助 / @bot help
    if re.search(rf'\[CQ:at,qq={re.escape(QQ_BOT_QQ_ID)}[^\]]*\]\s*(帮助|help|命令)', message, re.IGNORECASE):
        help_text = (
            "📋 可用指令列表\n\n"
            "🔹 通用指令（所有人可用）：\n"
            "　@机器人 测试　→ 查看运行状态+成功率+凭证时间\n"
            "　@机器人 帮助　→ 显示本帮助\n\n"
            "🔸 管理指令（仅管理员）：\n"
            "　@机器人 更换置顶 <动态ID>　→ 替换置顶动态（自动切手动）\n"
            "　@机器人 切换手动　→ 切为手动模式，锁定当前置顶\n"
            "　@机器人 切换自动　→ 切为自动模式，API自动跟踪\n"
            "　@机器人 更新凭证　→ 远程更新B站cookies（邮箱二维码）\n\n"
            "🔹 官方渠道（管理群，仅管理员）：\n"
            "　@机器人 官方列表　→ 查看官方群及推送状态\n"
            "　@机器人 开启官方推送 <别名>　→ 开启某官方群推送\n"
            "　@机器人 关闭官方推送 <别名>　→ 关闭某官方群推送"
        )
        await _reply_to_group(group_id, help_text)
        return web.json_response({"status": "ok"})

    # 可以在此扩展更多指令

    return web.json_response({"status": "ok"})


async def handle_official_callback(request: web.Request) -> web.Response:
    """接收 qbot 转发的官方群消息/事件；群消息只处理 @ 机器人的管理员。"""
    if QQ_OFFICIAL_FORWARD_TOKEN:
        token = request.headers.get("X-QBOT-Forward-Token", "")
        if token != QQ_OFFICIAL_FORWARD_TOKEN:
            return web.json_response({"status": "forbidden"}, status=403)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"status": "invalid json"}, status=400)

    if data.get("source") != "qq_official":
        return web.json_response({"status": "ok"})

    event_type = str(data.get("event_type") or "")
    if event_type == "group_add_robot":
        new_alias = str(data.get("group") or "")
        if new_alias:
            logger.info("官方 bot 加入新群: alias=%s", new_alias)
            await _notify_management_group(new_alias)
        return web.json_response({"status": "ok"})

    if event_type == "group_del_robot":
        removed_alias = str(data.get("group") or "")
        if removed_alias:
            set_official_group(removed_alias, False)
            logger.warning("官方 bot 被移出群，已关闭官方推送: alias=%s", removed_alias)
            for group_id in QQ_MANAGEMENT_GROUP_IDS:
                if group_id:
                    await _reply_to_group(
                        str(group_id),
                        "官方 bot 被移出群，已关闭官方推送: " + removed_alias,
                    )
        return web.json_response({"status": "ok"})

    admin_openids = QQ_OFFICIAL_ADMIN_OPENIDS
    if isinstance(admin_openids, str):
        admin_openids = [admin_openids]

    alias = str(data.get("group") or "")
    if not alias:
        return web.json_response({"status": "ok"})

    member_openid = str(data.get("member_openid") or "")
    msg_id = str(data.get("message_id") or "")
    content = re.sub(r"<@[^>]+>", "", str(data.get("content") or "")).strip()
    if not content:
        return web.json_response({"status": "ok"})

    logger.info(
        "官方群消息: alias=%s user_openid=%s content=%s",
        alias,
        member_openid,
        content[:100],
    )

    if member_openid not in admin_openids:
        logger.warning("官方指令忽略：非管理员 openid=%s alias=%s", member_openid, alias)
        return web.json_response({"status": "ok"})

    # 官方渠道列表
    if re.search(r"(官方列表|官方推送列表)", content):
        await _reply_to_official_group(alias, msg_id, await _official_list_text())
        return web.json_response({"status": "ok"})

    # 开启/关闭官方推送（不写别名时操作当前群）
    match_push = re.search(
        r"(开启|关闭)官方推送[\s:：,，]*([A-Za-z0-9_\-]+)?",
        content,
    )
    if match_push:
        action = match_push.group(1)
        target_alias = match_push.group(2) or alias
        if action == "开启":
            qbot_groups = await _fetch_official_qbot_groups()
            if qbot_groups and target_alias not in qbot_groups:
                await _reply_to_official_group(
                    alias,
                    msg_id,
                    "未知的官方群别名: " + target_alias,
                )
                return web.json_response({"status": "ok"})
        set_official_group(target_alias, action == "开启")
        await _reply_to_official_group(
            alias,
            msg_id,
            "已" + action + "官方推送: " + target_alias,
        )
        return web.json_response({"status": "ok"})

    # 更换置顶
    match_pin = re.search(r"更换置顶\s+(\d{15,20})", content)
    if match_pin:
        new_id = match_pin.group(1)
        file_ok = _update_config_file(new_id)
        if _monitor:
            _monitor.update_pinned_dynamic_id(new_id)
        if file_ok:
            await _reply_to_official_group(
                alias,
                msg_id,
                "置顶动态ID已更换\n链接: https://t.bilibili.com/" + new_id,
            )
        else:
            await _reply_to_official_group(
                alias,
                msg_id,
                "内存已更新但 config.py 写入失败，请检查日志",
            )
        return web.json_response({"status": "ok"})

    # 切换手动
    if re.search(r"切换手动", content):
        current_id = _monitor.pinned_dynamic_id if _monitor else "?"
        set_mode_manual()
        await _reply_to_official_group(
            alias,
            msg_id,
            "已切换为手动模式\n当前置顶: https://t.bilibili.com/" + str(current_id),
        )
        return web.json_response({"status": "ok"})

    # 切换自动
    if re.search(r"切换自动", content):
        set_mode_auto()
        await _reply_to_official_group(
            alias,
            msg_id,
            "已切换为自动模式\nAPI检测恢复后会自动同步B站置顶",
        )
        return web.json_response({"status": "ok"})

    # 测试/状态
    if re.search(r"(测试|状态)", content):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cookie_file = CONFIG_PATH.parent / "cookies.json"
        if cookie_file.exists():
            mtime = datetime.fromtimestamp(cookie_file.stat().st_mtime)
            last_cookie = mtime.strftime("%Y-%m-%d %H:%M:%S")
        else:
            last_cookie = "未知（文件不存在）"

        if _monitor:
            stats = _monitor.health_checker.get_stats(total_loops=_monitor.loop_count)
            monitor_info = (
                "模式: " + ("手动" if get_mode() == "manual" else "自动") + "\n"
                "B站置顶: https://t.bilibili.com/"
                + str(getattr(_monitor, "api_pinned_id", _monitor.pinned_dynamic_id))
                + "\n"
                "监测动态: https://t.bilibili.com/"
                + str(_monitor.pinned_dynamic_id)
                + "\n"
                "当前轮次: 第" + str(_monitor.loop_count) + "轮\n"
                "已运行: " + str(stats.get("重启后运行时间", "?")) + "\n"
                "置顶抓取: " + str(stats.get("抓取成功率", "?"))
                + " (" + str(stats.get("抓取成功次数", 0))
                + "/" + str(stats.get("抓取次数", 0)) + ")\n"
                "API请求: 成功率" + str(stats.get("API成功率", "?"))
                + " (" + str(stats.get("API成功次数", 0))
                + "/" + str(stats.get("API请求次数", 0)) + ")"
            )
        else:
            monitor_info = "Monitor 实例未就绪"

        reply = (
            "机器人运行正常\n"
            "服务器时间: " + now + "\n"
            + monitor_info + "\n"
            "推送群: NapCat " + str(len(QQ_GROUP_IDS))
            + "个 | 官方 " + str(len(load_official_groups()))
            + "个 | 管理群 " + str(len(QQ_MANAGEMENT_GROUP_IDS)) + "个\n"
            "上次凭证更新: " + last_cookie
        )
        await _reply_to_official_group(alias, msg_id, reply)
        return web.json_response({"status": "ok"})

    # 更新凭证：二维码发管理邮箱；首条被动回复，后续状态用官方主动消息
    if re.search(r"更新凭证", content):
        if not _COOKIE_RENEWER_AVAILABLE:
            await _reply_to_official_group(
                alias,
                msg_id,
                "凭证更新功能不可用：qrcode 库未安装",
            )
            return web.json_response({"status": "ok"})

        await _reply_to_official_group(
            alias,
            msg_id,
            "正在生成登录二维码，请查收管理邮箱...",
        )

        async def _do_official_renewal():
            async def status_cb(status_message: str):
                await _send_official_active(alias, status_message)

            try:
                result = await cookie_renewer.run_cookie_renewal(
                    on_status_update=status_cb
                )
            except Exception as exc:
                logger.error("❌ 官方渠道凭证更新异常: alias=%s err=%s", alias, exc)
                await _send_official_active(alias, "凭证更新异常: " + str(exc))
                return

            if result == "success" and _monitor:
                try:
                    await _monitor.restart_browser()
                    logger.info("🔄 浏览器已重启，新凭证已加载")
                except Exception as exc:
                    logger.error("重启浏览器失败: %s", exc)
            await _send_official_active(alias, "凭证更新结束: " + str(result))

        asyncio.create_task(_do_official_renewal())
        return web.json_response({"status": "ok"})

    # 帮助
    if re.search(r"(帮助|help|命令)", content, re.IGNORECASE):
        help_text = (
            "官方 Bot 可用指令（仅管理员）：\n"
            "@机器人 测试 / 状态\n"
            "@机器人 更换置顶 <动态ID>\n"
            "@机器人 切换手动\n"
            "@机器人 切换自动\n"
            "@机器人 更新凭证（二维码发管理邮箱）\n"
            "@机器人 官方列表\n"
            "@机器人 开启官方推送 [别名]\n"
            "@机器人 关闭官方推送 [别名]\n"
            "@机器人 帮助"
        )
        await _reply_to_official_group(alias, msg_id, help_text)
        return web.json_response({"status": "ok"})

    return web.json_response({"status": "ok"})


async def start_callback_server():
    """启动回调 HTTP 服务器（NapCat + 官方 Bot 两个入口）"""
    if not QQ_CALLBACK_ENABLED:
        logger.info("ℹ️ QQ回调服务器已禁用")
        return None

    official_groups = load_official_groups()
    official_admins = QQ_OFFICIAL_ADMIN_OPENIDS
    if isinstance(official_admins, str):
        official_admins = [official_admins]
    has_napcat_admin = bool(QQ_ADMIN_USERS and any(u for u in QQ_ADMIN_USERS))
    has_official_admin = bool([u for u in official_admins if u])

    if not QQ_BOT_QQ_ID and not official_groups:
        logger.warning("⚠️ QQ_BOT_QQ_ID 和官方群都未配置，回调服务器不启动")
        return None
    if not has_napcat_admin and not has_official_admin:
        logger.warning("⚠️ NapCat/官方管理员均未配置，回调服务器不启动")
        return None

    app = web.Application()
    app.router.add_post("/napcat/callback", handle_callback)
    app.router.add_post("/official/callback", handle_official_callback)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", QQ_CALLBACK_PORT)
    await site.start()
    logger.info(
        "✅ QQ回调服务器已启动: http://127.0.0.1:%s/napcat/callback",
        QQ_CALLBACK_PORT,
    )
    logger.info(
        "   官方回调: http://127.0.0.1:%s/official/callback",
        QQ_CALLBACK_PORT,
    )
    logger.info(
        "   授权用户: %s | 官方管理员 openid 数: %s",
        QQ_ADMIN_USERS,
        sum(1 for u in official_admins if u),
    )
    return runner
