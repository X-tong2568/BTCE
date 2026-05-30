# qq_callback_server.py
"""
NapCat HTTP 事件回调服务器（v4.4）。
监听 NapCat 上报的群消息事件，解析 @机器人 指令实现运行时更换置顶动态ID。
"""

import re
import json
import asyncio
from pathlib import Path
from typing import Optional
import aiohttp
from aiohttp import web
from logger_config import logger
from config_qq import (
    QQ_BOT_API_URL, QQ_BOT_ACCESS_TOKEN, QQ_GROUP_IDS,
    QQ_BOT_QQ_ID, QQ_CALLBACK_PORT, QQ_CALLBACK_ENABLED, QQ_ADMIN_USERS
)

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
        return web.Response(text="ok")

    group_id = str(data.get("group_id", ""))
    user_id = str(data.get("user_id", ""))
    message = data.get("message", "")
    sender_nick = data.get("sender", {}).get("nickname", "未知")

    # 只处理配置的群
    if group_id not in QQ_GROUP_IDS:
        return web.Response(text="ok")

    logger.info(f"📩 收到群消息: 群={group_id} 用户={user_id}({sender_nick}) 内容={message[:100]}")

    # 尝试解析更换置顶指令
    new_id = _parse_command(message)
    if new_id:
        # 权限检查
        if user_id not in QQ_ADMIN_USERS:
            await _reply_to_group(group_id, "❌ 你没有权限执行此操作")
            return web.Response(text="ok")

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
    # 可以在此扩展更多指令
    # elif "查看置顶" in message: ...

    return web.Response(text="ok")


async def start_callback_server():
    """启动回调 HTTP 服务器"""
    if not QQ_CALLBACK_ENABLED:
        logger.info("ℹ️ QQ回调服务器已禁用")
        return None
    if not QQ_BOT_QQ_ID:
        logger.warning("⚠️ QQ_BOT_QQ_ID 未配置，回调服务器不启动")
        return None
    if not QQ_ADMIN_USERS or all(u == "" for u in QQ_ADMIN_USERS):
        logger.warning("⚠️ QQ_ADMIN_USERS 未配置授权用户，回调服务器不启动")
        return None

    app = web.Application()
    app.router.add_post("/napcat/callback", handle_callback)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", QQ_CALLBACK_PORT)
    await site.start()
    logger.info(f"✅ QQ回调服务器已启动: http://127.0.0.1:{QQ_CALLBACK_PORT}/napcat/callback")
    logger.info(f"   授权用户: {QQ_ADMIN_USERS}")
    return runner
