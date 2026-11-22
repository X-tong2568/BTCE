# qbot.py
# 官方 QQ Bot 适配器入口：botpy WebSocket 事件、本地 HTTP 服务、鉴权检查。
import argparse
import asyncio
import logging
import re
import sys
from pathlib import Path
from typing import Any, Optional, Tuple

import aiohttp
import botpy
from aiohttp import web
from botpy.robot import Token

import config
from logger_config import setup_logging
from qbot_sender import OfficialSender

logger = logging.getLogger("qbot")


def save_discovered_group(group_openid: str, data_dir: Path) -> Tuple[Path, Path]:
    """
    作用：把发现的 group_openid 写入“最新值”和“去重列表”两个文件。
    入参：group_openid 群 openid；data_dir 数据目录。
    出参：(最新值文件路径, 列表文件路径)。
    """
    latest_path = data_dir / "discovered_group_openid.txt"
    list_path = data_dir / "discovered_group_openids.txt"
    data_dir.mkdir(parents=True, exist_ok=True)

    with open(latest_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(group_openid + "\n")

    existing = []
    if list_path.is_file():
        try:
            existing = [
                line.strip()
                for line in list_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except Exception:
            existing = []

    if group_openid not in existing:
        existing.append(group_openid)
        with open(list_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(existing) + "\n")

    return latest_path, list_path


def parse_test_command(content: str) -> Optional[Tuple[str, str]]:
    """
    作用：解析测试指令；只用于 ttkj-bot 联调，不操作 BTCE。
    入参：官方群消息 content（可能带 <@...>）。
    出参：(命令, 参数) 或 None。
    """
    text = re.sub(r"<@[^>]+>", "", content or "").strip()
    if not text:
        return None

    lowered = text.lower()
    if lowered in ("帮助", "help", "命令"):
        return ("help", "")
    if lowered in ("测试", "状态", "status"):
        return ("status", "")
    if lowered == "切换手动":
        return ("manual", "")
    if lowered == "切换自动":
        return ("auto", "")
    if lowered == "更新凭证":
        return ("renew", "")

    match = re.search(r"更换置顶\s+(\d{15,20})", text)
    if match:
        return ("set_pinned", match.group(1))
    return None


def is_admin_openid(member_openid: str, admin_openids: Any) -> bool:
    """作用：判断官方事件发送者是否在管理员 openid 白名单。"""
    if not member_openid:
        return False
    if isinstance(admin_openids, str):
        admin_openids = [admin_openids]
    return member_openid in (admin_openids or [])


def is_bot_mentioned(content: str, bot_openid: str) -> bool:
    """作用：判断群消息是否真正 @ 了机器人（官方格式 <@openid>）。"""
    if not content or not bot_openid:
        return False
    return ("<@" + bot_openid + ">") in content


def install_botpy_compat():
    """
    作用：给 qq-botpy 1.2.1 补上 GROUP_MESSAGE_CREATE 解析。
    原因：2026 平台实际下发 group_message_create，SDK 只注册了
          group_at_message_create；不补会导致事件被直接丢弃。
    """
    from botpy.connection import ConnectionState
    from botpy.message import GroupMessage

    if hasattr(ConnectionState, "parse_group_message_create"):
        return

    def parse_group_message_create(self, payload):
        """作用：把 GROUP_MESSAGE_CREATE 转成 GroupMessage 并派发。"""
        message = GroupMessage(
            self.api,
            payload.get("id", None),
            payload.get("d", {}),
        )
        self._dispatch("group_message_create", message)

    ConnectionState.parse_group_message_create = parse_group_message_create
    logger.info("已安装 botpy 兼容补丁: GROUP_MESSAGE_CREATE")


class QQBOTClient(botpy.Client):
    """botpy 客户端子类：把 WS 事件桥接到发送器和回调转发。"""

    def __init__(
        self,
        sender: OfficialSender,
        discover_group: bool = False,
        **kwargs: Any,
    ):
        super().__init__(**kwargs)
        self.sender = sender
        self.discover_group = discover_group

    async def on_ready(self):
        bot_name = getattr(self.robot, "name", "?")
        logger.info("官方 Bot 已上线: %s", bot_name)
        self.sender.set_ready("ready")

    async def on_resumed(self):
        logger.info("官方 Bot WebSocket 已恢复: reason=resume")
        self.sender.set_ready("resumed")

    async def on_group_at_message_create(self, message: Any):
        """作用：兼容旧事件名 GROUP_AT_MESSAGE_CREATE。"""
        await self._handle_group_message(message, "group_at_message_create")

    async def on_group_message_create(self, message: Any):
        """作用：兼容 2026 平台新事件名 GROUP_MESSAGE_CREATE。"""
        await self._handle_group_message(message, "group_message_create")

    async def _handle_group_message(self, message: Any, event_name: str):
        """作用：只处理真正 @ 机器人的消息；其他消息只做常驻发现。"""
        group_openid = getattr(message, "group_openid", "")
        member_openid = getattr(getattr(message, "author", None), "member_openid", "")
        content = getattr(message, "content", "") or ""
        mentioned_bot = is_bot_mentioned(content, config.QBOT_BOT_OPENID)

        log_method = logger.info if mentioned_bot else logger.debug
        log_method(
            "收到群消息(event=%s): message_id=%s group_openid=%s member_openid=%s content=%s",
            event_name,
            getattr(message, "id", ""),
            group_openid,
            member_openid,
            content[:200],
        )

        # 常驻发现：任何群消息都可以自动记录/加群；但不等于要回复。
        self._record_discovered_group(group_openid)
        self.sender.ensure_group_for_openid(group_openid)

        if not mentioned_bot:
            logger.debug("未 @ 机器人，忽略指令/转发: content=%s", content[:100])
            return

        is_admin = is_admin_openid(member_openid, config.QBOT_ADMIN_OPENIDS)
        if is_admin:
            if config.QBOT_ECHO_ENABLED and "ping" in content.lower():
                await self._send_passive_reply(message, "pong", "pong")

            if config.QBOT_TEST_COMMANDS:
                command = parse_test_command(content)
                if command:
                    await self._reply_test_command(message, command)
        else:
            logger.info(
                "非管理员 @ 消息，不回复: member_openid=%s content=%s",
                member_openid,
                content[:100],
            )

        if config.QBOT_CALLBACK_FORWARD_URL:
            asyncio.create_task(self._forward_event(message, event_name))

    async def _send_passive_reply(self, message: Any, text: str, label: str):
        """作用：被动回复并检查返回码；失败只记日志，不影响主流程。"""
        try:
            result = await message.reply(content=text)
            if isinstance(result, dict):
                code = result.get("code", 0)
                if code not in (0, "0", None):
                    logger.warning(
                        "%s 回复失败: code=%s msg=%s",
                        label,
                        code,
                        result.get("message") or result.get("msg") or "",
                    )
                    return
            logger.info(
                "%s 回复成功: group_openid=%s",
                label,
                getattr(message, "group_openid", ""),
            )
        except Exception as exc:
            logger.warning("%s 回复异常: %s", label, exc)

    async def _reply_test_command(
        self,
        message: Any,
        command: Tuple[str, str],
    ):
        """作用：测试模式回显管理指令；不操作 BTCE。"""
        name, argument = command
        if name == "help":
            text = (
                "ttkj-bot 测试指令（仅管理员，不会操作 BTCE）：\n"
                "@机器人 ping\n"
                "@机器人 测试 / 状态\n"
                "@机器人 更换置顶 <动态ID>\n"
                "@机器人 切换手动\n"
                "@机器人 切换自动\n"
                "@机器人 更新凭证"
            )
        elif name == "status":
            groups = ",".join(self.sender.group_openids.keys()) or "(空)"
            queue_size = sum(queue.qsize() for queue in self.sender.queues.values())
            text = (
                "ttkj-bot 测试状态\n"
                "WS: 在线\n"
                "已配置群: " + groups + "\n"
                "队列: " + str(queue_size) + "\n"
                "BTCE 监控状态需接入 BTCE 后查看"
            )
        elif name == "set_pinned":
            text = "测试模式：已收到更换置顶 " + argument + "；未写入 BTCE"
        elif name == "manual":
            text = "测试模式：已收到切换手动；未操作 BTCE"
        elif name == "auto":
            text = "测试模式：已收到切换自动；未操作 BTCE"
        elif name == "renew":
            text = "测试模式：更新凭证需接入 BTCE 后测试"
        else:
            text = "测试模式：未知指令"

        await self._send_passive_reply(message, text, "测试指令/" + name)

    async def on_group_msg_reject(self, event: Any):
        """作用：群关闭了机器人主动消息。"""
        group_openid = getattr(event, "group_openid", "")
        logger.warning("群关闭了机器人主动消息: group_openid=%s", group_openid)
        self.sender.set_group_active(group_openid, False)

    async def on_group_msg_receive(self, event: Any):
        """作用：群允许了机器人主动消息。"""
        group_openid = getattr(event, "group_openid", "")
        logger.info("群允许了机器人主动消息: group_openid=%s", group_openid)
        self.sender.set_group_active(group_openid, True)

    async def on_group_add_robot(self, event: Any):
        """作用：机器人被拉进群；记录、自动加入推送列表，并通知 BTCE。"""
        group_openid = getattr(event, "group_openid", "")
        logger.info("机器人加入群: group_openid=%s", group_openid)
        self._record_discovered_group(group_openid)
        alias = self.sender.ensure_group_for_openid(group_openid)
        if alias and config.QBOT_CALLBACK_FORWARD_URL:
            asyncio.create_task(
                self._forward_group_event(
                    "group_add_robot",
                    group_openid,
                    alias,
                    event,
                )
            )

    async def on_group_del_robot(self, event: Any):
        """作用：机器人被移出群；自动群移除，并通知 BTCE 关闭官方推送。"""
        group_openid = getattr(event, "group_openid", "")
        logger.warning("机器人被移出群: group_openid=%s", group_openid)
        alias = self.sender.disable_auto_group(group_openid)
        if not alias:
            alias = self.sender.alias_for_openid(group_openid)
        if alias and config.QBOT_CALLBACK_FORWARD_URL:
            asyncio.create_task(
                self._forward_group_event(
                    "group_del_robot",
                    group_openid,
                    alias,
                    event,
                )
            )

    def _record_discovered_group(self, group_openid: str):
        """作用：把新群 openid 落盘；已知群不重复刷日志。"""
        if not group_openid:
            return
        if group_openid in (self.sender.group_openids or {}).values():
            return
        try:
            latest_path, list_path = save_discovered_group(group_openid, config.DATA_DIR)
        except Exception as exc:
            logger.warning("写入 group_openid 文件失败: %s", exc)
            return
        logger.info(
            "发现新群 group_openid=%s，已写入 %s 和 %s；"
            "请把它加到 qbot 的 GROUP_OPENIDS（起个别名）和 BTCE 的 "
            "QQ_OFFICIAL_GROUPS（官方推送别名列表）",
            group_openid,
            latest_path,
            list_path,
        )

    async def _forward_event(
        self,
        message: Any,
        event_name: str = "group_at_message_create",
    ):
        """作用：把官方群消息转发给 BTCE；不伪造 NapCat 字段。"""
        group_openid = getattr(message, "group_openid", "")
        payload = {
            "source": "qq_official",
            "event_type": event_name,
            "event_id": getattr(message, "event_id", ""),
            "group_openid": group_openid,
            "group": self.sender.alias_for_openid(group_openid),
            "member_openid": getattr(getattr(message, "author", None), "member_openid", ""),
            "message_id": getattr(message, "id", ""),
            "content": getattr(message, "content", ""),
            "timestamp": getattr(message, "timestamp", ""),
        }
        await self._post_forward(payload)

    async def _forward_group_event(
        self,
        event_type: str,
        group_openid: str,
        alias: str,
        event: Any,
    ):
        """作用：把加入/移出群事件转发给 BTCE，用于管理群通知。"""
        payload = {
            "source": "qq_official",
            "event_type": event_type,
            "group_openid": group_openid,
            "group": alias,
            "op_member_openid": getattr(event, "op_member_openid", ""),
            "timestamp": getattr(event, "timestamp", ""),
        }
        await self._post_forward(payload)

    async def _post_forward(self, payload: dict):
        """作用：把事件 POST 到 BTCE；失败只记日志，不影响 qbot。"""
        if not config.QBOT_CALLBACK_FORWARD_URL:
            return
        headers = {}
        if config.QBOT_FORWARD_TOKEN:
            headers["X-QBOT-Forward-Token"] = config.QBOT_FORWARD_TOKEN
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    config.QBOT_CALLBACK_FORWARD_URL,
                    json=payload,
                    headers=headers,
                    timeout=config.QBOT_FORWARD_TIMEOUT,
                ) as response:
                    if response.status >= 400:
                        logger.warning(
                            "事件转发失败: HTTP %s url=%s event=%s",
                            response.status,
                            config.QBOT_CALLBACK_FORWARD_URL,
                            payload.get("event_type"),
                        )
        except Exception as exc:
            logger.warning("事件转发异常: %s", exc)


def create_client(
    sender: OfficialSender,
    discover_group: bool = False,
) -> QQBOTClient:
    """作用：创建官方 Bot 客户端；sandbox/prod 由进程环境决定。"""
    install_botpy_compat()
    intents = botpy.Intents(public_messages=True)
    client = QQBOTClient(
        sender=sender,
        discover_group=discover_group,
        intents=intents,
        is_sandbox=(config.QBOT_ENV == "sandbox"),
        timeout=config.QBOT_HTTP_TIMEOUT,
        bot_log=True,
    )
    sender.attach_client(client)
    return client


async def start_http_server(sender: OfficialSender) -> web.AppRunner:
    """作用：启动本地 HTTP 接口。"""
    app = web.Application()
    app.router.add_post(config.QBOT_SEND_PATH, sender.handle_send)
    app.router.add_post("/official_reply", sender.handle_reply)
    app.router.add_post("/official_alias", sender.handle_alias)
    app.router.add_get("/health", sender.handle_health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(
        runner,
        config.QBOT_SEND_HOST,
        config.QBOT_SEND_PORT,
    )
    await site.start()
    logger.info(
        "本地发送接口已启动: http://%s:%s%s",
        config.QBOT_SEND_HOST,
        config.QBOT_SEND_PORT,
        config.QBOT_SEND_PATH,
    )
    logger.info(
        "被动回复接口: http://%s:%s/official_reply",
        config.QBOT_SEND_HOST,
        config.QBOT_SEND_PORT,
    )
    logger.info(
        "群别名修改接口: http://%s:%s/official_alias",
        config.QBOT_SEND_HOST,
        config.QBOT_SEND_PORT,
    )
    logger.info(
        "健康检查接口: http://%s:%s/health",
        config.QBOT_SEND_HOST,
        config.QBOT_SEND_PORT,
    )
    return runner


async def run_auth_check() -> int:
    """作用：只做 APP_ID/APP_SECRET 登录鉴权检查；错误 IP/Token 会在这里明确失败。"""
    if not config.QBOT_APP_ID or not config.QBOT_APP_SECRET:
        logger.error("鉴权失败: APP_ID 或 APP_SECRET 未配置")
        return 2

    client = botpy.Client(
        intents=botpy.Intents(public_messages=True),
        is_sandbox=(config.QBOT_ENV == "sandbox"),
        timeout=config.QBOT_HTTP_TIMEOUT,
        bot_log=False,
    )
    try:
        robot = await client.http.login(
            Token(config.QBOT_APP_ID, config.QBOT_APP_SECRET)
        )
        logger.info("鉴权成功: %s", getattr(robot, "name", "?"))
        return 0
    except Exception as exc:
        logger.error("鉴权/登录失败: %s: %s", type(exc).__name__, exc)
        return 1
    finally:
        await client.http.close()


async def main_async(args: argparse.Namespace) -> int:
    """作用：进程主入口；本地 HTTP 服务与 botpy WebSocket 并行运行。"""
    setup_logging("qbot")

    # 命令行显式覆盖只用于 --check-auth 等自检场景，优先于 config_custom.py。
    if getattr(args, "app_id", None):
        config.QBOT_APP_ID = args.app_id
    if getattr(args, "app_secret", None):
        config.QBOT_APP_SECRET = args.app_secret

    if args.check_auth:
        return await run_auth_check()

    if args.mock:
        config.QBOT_MOCK = True
        config.QBOT_ALLOW_MOCK_ERROR = True
        if not config.GROUP_OPENIDS.get(config.QBOT_ENV):
            config.GROUP_OPENIDS[config.QBOT_ENV] = {"mock": "mock_group_openid"}

    if args.host:
        config.QBOT_SEND_HOST = args.host
    if args.port:
        config.QBOT_SEND_PORT = args.port

    discover_group = bool(args.discover_group)

    if not config.QBOT_MOCK:
        if not config.QBOT_APP_ID or not config.QBOT_APP_SECRET:
            logger.error(
                "未配置 QBOT_APP_ID/QBOT_APP_SECRET，无法连接官方 Bot；可先用 --mock 自测"
            )
            return 2
        if not config.GROUP_OPENIDS.get(config.QBOT_ENV) and not discover_group:
            logger.error(
                "未配置 %s 的 group_openid；可先用 --discover-group 发现，或 --mock 自测",
                config.QBOT_ENV,
            )
            return 2
        if discover_group:
            logger.info(
                "发现模式：把机器人拉进测试群，或在群里 @ 它；"
                "日志和数据文件会打印 group_openid"
            )

    sender = OfficialSender(mock=config.QBOT_MOCK)
    await sender.start()
    runner = await start_http_server(sender)
    client_task: Optional[asyncio.Task] = None
    client: Optional[QQBOTClient] = None

    try:
        if config.QBOT_MOCK:
            logger.info("已进入 mock 模式：不连接 QQ，只验证本地契约和队列逻辑")
            await asyncio.Event().wait()
        else:
            client = create_client(sender, discover_group=discover_group)
            client_task = asyncio.create_task(
                client.start(config.QBOT_APP_ID, config.QBOT_APP_SECRET)
            )
            await client_task
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logger.error("官方 Bot 运行失败: %s: %s", type(exc).__name__, exc)
        return 1
    finally:
        if client_task:
            client_task.cancel()
            try:
                await client_task
            except (asyncio.CancelledError, Exception):
                pass
        await sender.close()
        await runner.cleanup()
    return 0


def parse_args() -> argparse.Namespace:
    """作用：解析启动参数。"""
    parser = argparse.ArgumentParser(description="ttkj-bot 官方 QQ Bot 适配器")
    parser.add_argument(
        "--mock",
        action="store_true",
        help="不连接 QQ，只跑本地 HTTP/队列/错误分类",
    )
    parser.add_argument(
        "--check-auth",
        action="store_true",
        help="只做登录鉴权检查后退出",
    )
    parser.add_argument(
        "--discover-group",
        action="store_true",
        help="只连 WebSocket，发现并打印群 group_openid",
    )
    parser.add_argument(
        "--app-id",
        default=None,
        help="临时覆盖 APP_ID，仅用于 --check-auth 自检",
    )
    parser.add_argument(
        "--app-secret",
        default=None,
        help="临时覆盖 APP_SECRET，仅用于 --check-auth 自检",
    )
    parser.add_argument("--host", default=None, help="覆盖本地监听地址")
    parser.add_argument("--port", type=int, default=None, help="覆盖本地监听端口")
    return parser.parse_args()


if __name__ == "__main__":
    try:
        exit_code = asyncio.run(main_async(parse_args()))
    except KeyboardInterrupt:
        exit_code = 0
    sys.exit(exit_code)
