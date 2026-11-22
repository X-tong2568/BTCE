# qbot_sender.py
# 官方通道发送器：队列、频控、重试、图片上传、本地 HTTP 处理。
import asyncio
import base64
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp
import botpy
from aiohttp import web
from botpy.errors import (
    AuthenticationFailedError,
    ForbiddenError,
    MethodNotAllowedError,
    NotFoundError,
    SequenceNumberError,
    ServerError,
)
from botpy.http import Route

import config
from qbot_errors import MEDIA_FALLBACK_CODES, QBotAPIError, classify_error
from qbot_state import (
    QueueItem,
    RateLimiter,
    SendPart,
    StateStore,
    next_auto_alias,
    normalize_group_map,
    normalize_request_parts,
)

logger = logging.getLogger("qbot")

class OfficialSender:
    """官方通道发送器：队列、频控、重试、图片上传都在这里。"""

    def __init__(self, mock: bool = False):
        self.mock = bool(mock or config.QBOT_MOCK)
        self.group_key = config.QBOT_ENV
        self.group_openids: Dict[str, str] = {}
        self.auto_groups: Dict[str, str] = {}
        self.alias_overrides: Dict[str, str] = {}
        self.default_group: Optional[str] = None
        self.client: Optional[botpy.Client] = None
        self.ready = asyncio.Event()
        self.queues: Dict[str, asyncio.Queue] = {}
        self.worker_tasks: Dict[str, asyncio.Task] = {}
        self.state = StateStore(config.DATA_DIR / "qbot_state.json")
        self.group_limiters: Dict[str, RateLimiter] = {}
        self.bot_limiter = RateLimiter(config.RATE_LIMIT_BOT_QPM)
        self.group_active_allowed: Dict[str, Optional[bool]] = {}
        self.sent_count = 0
        self.failed_count = 0
        self.dropped_count = 0
        self.passive_reply_count = 0
        self.started_at = time.time()

    @property
    def group_openid(self) -> str:
        """兼容旧代码：返回默认群的 openid。"""
        if self.default_group:
            return self.group_openids.get(self.default_group, "")
        return ""

    async def start(self):
        """作用：为每个群启动独立队列和 worker；mock 模式下直接标记就绪。"""
        raw_map = config.GROUP_OPENIDS.get(self.group_key)
        self.group_openids = normalize_group_map(raw_map)
        self.auto_groups = self._load_auto_groups()

        # 合并上次运行自动加入的群；配置里的别名优先。
        for alias, openid in self.auto_groups.items():
            if alias in self.group_openids:
                continue
            if openid in self.group_openids.values():
                continue
            self.group_openids[alias] = openid

        # 应用上次管理指令改过的别名（persist 在 alias_overrides.json）
        self.alias_overrides = self._load_alias_overrides()
        self._apply_alias_overrides()

        self.default_group = next(iter(self.group_openids), None)

        for alias, openid in self.group_openids.items():
            if openid in self.queues:
                logger.warning("重复的 group_openid，跳过别名: %s", alias)
                continue
            self.queues[openid] = asyncio.Queue(maxsize=config.SEND_QUEUE_MAX)
            self.group_limiters[openid] = RateLimiter(config.RATE_LIMIT_GROUP_QPM)
            self.worker_tasks[openid] = asyncio.create_task(self._worker(openid))

        if not self.queues:
            logger.error("未配置 group_openid，发送队列不会启动")

        if self.mock:
            self.ready.set()

        logger.info(
            "发送器启动: env=%s groups=%s default=%s mock=%s",
            self.group_key,
            ",".join(self.group_openids.keys()) or "(空)",
            self.default_group or "(空)",
            self.mock,
        )

    def _load_auto_groups(self) -> Dict[str, str]:
        """作用：读取上次自动加入的群映射（按环境隔离）。"""
        path = config.DATA_DIR / "auto_groups.json"
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            env_map = data.get(self.group_key) or {}
            return {
                str(alias): str(openid)
                for alias, openid in env_map.items()
                if alias and openid
            }
        except Exception as exc:
            logger.warning("读取 auto_groups.json 失败: %s", exc)
            return {}

    def _save_auto_groups(self):
        """作用：保存自动加入的群映射，重启后仍可推送。"""
        path = config.DATA_DIR / "auto_groups.json"
        data = {}
        try:
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        data[self.group_key] = self.auto_groups
        try:
            tmp_path = path.with_suffix(".tmp")
            with open(tmp_path, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
        except Exception as exc:
            logger.warning("写入 auto_groups.json 失败: %s", exc)

    def _load_alias_overrides(self) -> Dict[str, str]:
        """作用：读取群别名覆盖（openid -> 新别名，按环境隔离）。"""
        path = config.DATA_DIR / "alias_overrides.json"
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            env_map = data.get(self.group_key) or {}
            return {
                str(openid): str(alias)
                for openid, alias in env_map.items()
                if openid and alias
            }
        except Exception as exc:
            logger.warning("读取 alias_overrides.json 失败: %s", exc)
            return {}

    def _save_alias_overrides(self):
        """作用：保存群别名覆盖，重启后仍使用新别名。"""
        path = config.DATA_DIR / "alias_overrides.json"
        data = {}
        try:
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        data[self.group_key] = self.alias_overrides
        try:
            tmp_path = path.with_suffix(".tmp")
            with open(tmp_path, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
        except Exception as exc:
            logger.warning("写入 alias_overrides.json 失败: %s", exc)

    def _apply_alias_overrides(self):
        """作用：启动时把覆盖别名应用到 group_openids / auto_groups。"""
        if not self.alias_overrides:
            return
        changed_auto = False
        for openid, target in list(self.alias_overrides.items()):
            if not openid or not target:
                continue
            old_aliases = [
                alias
                for alias, existing in self.group_openids.items()
                if existing == openid
            ]
            if not old_aliases:
                continue
            if target in self.group_openids and self.group_openids[target] != openid:
                logger.warning("别名覆盖冲突，跳过: openid=%s target=%s", openid, target)
                continue
            was_auto = any(alias in self.auto_groups for alias in old_aliases)
            for alias in old_aliases:
                self.group_openids.pop(alias, None)
                self.auto_groups.pop(alias, None)
            self.group_openids[target] = openid
            if was_auto:
                self.auto_groups[target] = openid
                changed_auto = True
        if changed_auto:
            self._save_auto_groups()

    @staticmethod
    def _valid_alias(alias: str) -> bool:
        """作用：校验群别名：非空、无空白、最长 32 字符。"""
        return bool(alias) and len(alias) <= 32 and not any(ch.isspace() for ch in alias)

    def rename_group(self, old_alias: Any, new_alias: Any) -> Dict[str, Any]:
        """作用：修改群别名，同步内存和 alias_overrides.json。"""
        old_alias = str(old_alias or "").strip()
        new_alias = str(new_alias or "").strip()
        if not self._valid_alias(old_alias) or not self._valid_alias(new_alias):
            return {
                "code": 400,
                "data": None,
                "msg": "别名必须非空、无空白且不超过32字符",
            }
        if old_alias == new_alias:
            return {
                "code": 0,
                "data": {"old": old_alias, "new": new_alias},
                "msg": "别名相同",
            }
        if old_alias not in self.group_openids:
            return {"code": 404, "data": None, "msg": "未知的群别名: " + old_alias}

        openid = self.group_openids[old_alias]
        if new_alias in self.group_openids and self.group_openids[new_alias] != openid:
            return {"code": 409, "data": None, "msg": "新别名已被占用: " + new_alias}

        was_auto = old_alias in self.auto_groups
        self.group_openids.pop(old_alias, None)
        self.group_openids[new_alias] = openid
        if was_auto:
            self.auto_groups.pop(old_alias, None)
            self.auto_groups[new_alias] = openid
            self._save_auto_groups()

        self.alias_overrides[openid] = new_alias
        self._save_alias_overrides()
        if self.default_group == old_alias:
            self.default_group = new_alias

        logger.info(
            "群别名已修改: %s -> %s (openid=%s auto=%s)",
            old_alias,
            new_alias,
            openid,
            was_auto,
        )
        return {
            "code": 0,
            "data": {"old": old_alias, "new": new_alias, "auto": was_auto},
            "msg": "ok",
        }

    def ensure_group_for_openid(self, group_openid: str) -> Optional[str]:
        """作用：新群自动加入推送列表；已存在返回现有别名。"""
        if not group_openid or not config.QBOT_AUTO_ADD_GROUPS:
            return None

        for alias, existing in self.group_openids.items():
            if existing == group_openid:
                return alias

        # 被移出后又拉回：优先沿用管理指令改过的新别名
        preferred = self.alias_overrides.get(group_openid)
        if preferred and preferred not in self.group_openids:
            alias = preferred
        else:
            alias = next_auto_alias(self.group_openids.keys())
        self.group_openids[alias] = group_openid
        self.auto_groups[alias] = group_openid
        self.queues[group_openid] = asyncio.Queue(maxsize=config.SEND_QUEUE_MAX)
        self.group_limiters[group_openid] = RateLimiter(config.RATE_LIMIT_GROUP_QPM)
        self.worker_tasks[group_openid] = asyncio.create_task(self._worker(group_openid))
        if self.default_group is None:
            self.default_group = alias
        self._save_auto_groups()
        logger.info(
            "新群已自动加入推送列表: alias=%s group_openid=%s；"
            "BTCE 侧如需推送，把 BTCE 群号映射到 %s",
            alias,
            group_openid,
            alias,
        )
        return alias

    def disable_auto_group(self, group_openid: str) -> Optional[str]:
        """作用：机器人被移出群时，把自动加入的群从推送列表移除。"""
        alias = next(
            (
                alias
                for alias, openid in self.auto_groups.items()
                if openid == group_openid
            ),
            None,
        )
        if not alias:
            return None

        self.auto_groups.pop(alias, None)
        self.group_openids.pop(alias, None)
        self.queues.pop(group_openid, None)
        task = self.worker_tasks.pop(group_openid, None)
        self.group_limiters.pop(group_openid, None)
        if task:
            task.cancel()
        if self.default_group == alias:
            self.default_group = next(iter(self.group_openids), None)
        self._save_auto_groups()
        logger.warning("机器人被移出群，已从自动推送列表移除: alias=%s", alias)
        return alias

    def attach_client(self, client: botpy.Client):
        """作用：绑定 botpy Client，供发送和鉴权使用。"""
        self.client = client

    def set_ready(self, reason: str):
        """作用：WebSocket READY/RESUMED 后放行队列。"""
        self.ready.set()
        queue_size = sum(queue.qsize() for queue in self.queues.values())
        logger.info("发送队列就绪: reason=%s queue_size=%s", reason, queue_size)

    def set_group_active(self, group_openid: str, allowed: bool):
        """作用：记录群主动消息开关状态。"""
        if not group_openid:
            return
        self.group_active_allowed[group_openid] = allowed
        logger.info(
            "群主动消息状态更新: group_openid=%s allowed=%s",
            group_openid,
            allowed,
        )

    def alias_for_openid(self, group_openid: str) -> Optional[str]:
        """作用：把 group_openid 反查成群别名，供事件转发。"""
        for alias, openid in self.group_openids.items():
            if openid == group_openid:
                return alias
        return None

    async def enqueue(
        self,
        group_key: str,
        parts: List[SendPart],
        wait: bool,
        request_id: str,
        group_alias: Optional[str] = None,
        mock_error: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        作用：把逻辑消息放进指定群的队列；wait=True 时等待发送结果。
        入参：group_key 环境；group_alias 群别名；parts；wait；request_id；mock_error。
        出参：统一 {code, data, msg}。
        """
        if not config.QQ_OFFICIAL_ENABLED:
            return {"code": 503, "data": None, "msg": "官方通道已关闭"}
        if group_key != self.group_key:
            return {
                "code": 400,
                "data": None,
                "msg": "group_key 与当前进程环境不一致: " + group_key + " != " + self.group_key,
            }
        if not self.group_openids:
            return {"code": 503, "data": None, "msg": "未配置 group_openid"}
        if not parts:
            return {"code": 400, "data": None, "msg": "消息内容为空"}
        if mock_error is not None and not (self.mock and config.QBOT_ALLOW_MOCK_ERROR):
            return {"code": 400, "data": None, "msg": "当前模式不允许 mock_error"}

        alias = group_alias or self.default_group
        if not alias:
            return {"code": 503, "data": None, "msg": "未指定群"}
        if alias not in self.group_openids:
            return {
                "code": 400,
                "data": {"available_groups": list(self.group_openids.keys())},
                "msg": "未知的群别名: " + str(alias),
            }

        group_openid = self.group_openids[alias]
        queue = self.queues.get(group_openid)
        if queue is None:
            return {"code": 503, "data": None, "msg": "该群发送队列未启动: " + str(alias)}

        future = asyncio.get_running_loop().create_future() if wait else None
        item = QueueItem(
            request_id=request_id,
            group_openid=group_openid,
            group_alias=alias,
            parts=parts,
            created_at=time.monotonic(),
            future=future,
            mock_error=mock_error,
        )

        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:
            self.dropped_count += 1
            logger.error("发送队列已满，丢弃消息: request_id=%s group=%s", request_id, alias)
            return {
                "code": 503,
                "data": {"request_id": request_id, "group": alias},
                "msg": "发送队列已满",
            }

        logger.info(
            "消息入队: request_id=%s group=%s parts=%s queue_size=%s",
            request_id,
            alias,
            len(parts),
            queue.qsize(),
        )

        if not wait:
            return {
                "code": 0,
                "data": {
                    "request_id": request_id,
                    "group": alias,
                    "queued": True,
                    "queue_size": queue.qsize(),
                },
                "msg": "queued",
            }

        try:
            return await asyncio.wait_for(future, timeout=config.PROBE_WAIT_TIMEOUT)
        except asyncio.TimeoutError:
            # 超时后不再回填 future，避免 “Future exception was never retrieved”。
            item.future = None
            return {
                "code": 504,
                "data": {"request_id": request_id, "group": alias},
                "msg": "等待发送结果超时",
            }

    async def _worker(self, group_openid: str):
        """作用：单群 FIFO worker，保证同一群内消息按入队顺序发送。"""
        queue = self.queues[group_openid]
        while True:
            item = await queue.get()
            try:
                result = await self._process_item(item)
            except asyncio.CancelledError:
                result = self._failure(
                    item,
                    QBotAPIError(None, "worker 被取消", False),
                    [],
                )
                raise
            except Exception as exc:
                logger.exception("处理队列消息异常: request_id=%s", item.request_id)
                result = self._failure(
                    item,
                    QBotAPIError(None, "未捕获异常: " + str(exc), False),
                    [],
                )
            finally:
                queue.task_done()

            if item.future and not item.future.done():
                item.future.set_result(result)

    async def _process_item(self, item: QueueItem) -> Dict[str, Any]:
        """作用：按顺序发送一个逻辑消息的所有 parts。"""
        part_results = []

        for part in item.parts:
            try:
                part_result = await self._send_part_with_retry(item, part)
                part_results.append(part_result)
            except QBotAPIError as exc:
                self.failed_count += 1
                logger.error(
                    "发送失败: request_id=%s group=%s type=%s code=%s retryable=%s msg=%s",
                    item.request_id,
                    item.group_alias,
                    part.type,
                    exc.code,
                    exc.retryable,
                    exc.message,
                )
                return self._failure(item, exc, part_results)

        self.sent_count += 1
        logger.info(
            "发送成功: request_id=%s group=%s parts=%s",
            item.request_id,
            item.group_alias,
            len(part_results),
        )
        return {
            "code": 0,
            "data": {
                "request_id": item.request_id,
                "group": item.group_alias,
                "parts": part_results,
            },
            "msg": "ok",
        }

    async def _send_part_with_retry(self, item: QueueItem, part: SendPart) -> Dict[str, Any]:
        """作用：单个 part 的发送与重试；重试只重发当前 part，不重发前面成功的部分。"""
        last_error: Optional[QBotAPIError] = None

        for attempt in range(1, config.RETRY_MAX_ATTEMPTS + 1):
            if time.monotonic() - item.created_at > config.SEND_QUEUE_TTL_SECONDS:
                raise QBotAPIError(None, "消息超过队列 TTL，放弃发送", False)

            try:
                await self._wait_ready()
                await self._acquire_limits(item.group_openid)
                result = await self._send_part(item, part)
                self.state.incr_daily(item.group_openid)
                return {
                    "type": part.type,
                    "status": "ok",
                    "attempts": attempt,
                    "result": result,
                }
            except QBotAPIError as exc:
                last_error = exc
                item.attempts += 1
                if not exc.retryable or attempt >= config.RETRY_MAX_ATTEMPTS:
                    raise
                delay = self._retry_delay(exc.code, attempt)
                logger.warning(
                    "发送重试: request_id=%s type=%s code=%s attempt=%s delay=%ss msg=%s",
                    item.request_id,
                    part.type,
                    exc.code,
                    attempt,
                    delay,
                    exc.message,
                )
                await asyncio.sleep(delay)
            except Exception as exc:
                item.attempts += 1
                last_error = QBotAPIError(None, "未捕获异常: " + str(exc), True)
                if attempt >= config.RETRY_MAX_ATTEMPTS:
                    raise last_error
                delay = self._retry_delay(None, attempt)
                logger.warning(
                    "发送异常重试: request_id=%s type=%s attempt=%s delay=%ss msg=%s",
                    item.request_id,
                    part.type,
                    attempt,
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)

        if last_error:
            raise last_error
        raise QBotAPIError(None, "发送失败", False)

    def _retry_delay(self, code: Optional[int], attempt: int) -> float:
        """作用：计算重试延迟；频控错误至少等 60 秒。"""
        delay = min(
            config.RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1)),
            config.RETRY_MAX_DELAY_SECONDS,
        )
        if code == 40034100:
            delay = max(delay, 60)
        return float(delay)

    async def _wait_ready(self):
        """作用：等待 WebSocket READY/RESUMED；mock 模式直接通过。"""
        if self.mock:
            return
        try:
            await asyncio.wait_for(self.ready.wait(), timeout=config.QBOT_HTTP_TIMEOUT)
        except asyncio.TimeoutError:
            raise QBotAPIError(None, "WebSocket 尚未就绪", True)

    async def _acquire_limits(self, group_openid: str):
        """作用：检查每日上限并获取 Bot/单群发送名额。"""
        daily_count = self.state.get_daily(group_openid)
        if daily_count >= config.RATE_LIMIT_DAILY_PER_GROUP:
            raise QBotAPIError(40034100, "单群每日主动消息已达上限", False)

        allowed = self.group_active_allowed.get(group_openid)
        if allowed is False:
            raise QBotAPIError(40034105, "群主动消息开关已关闭", False)

        group_limiter = self.group_limiters.get(group_openid)
        if group_limiter is None:
            group_limiter = RateLimiter(config.RATE_LIMIT_GROUP_QPM)
            self.group_limiters[group_openid] = group_limiter

        await self.bot_limiter.acquire()
        await group_limiter.acquire()

    async def _send_part(self, item: QueueItem, part: SendPart) -> Dict[str, Any]:
        """作用：真正发送一个 part；mock 模式只模拟成功/注入错误。"""
        if self.mock:
            if item.mock_error is not None:
                retryable, reason = classify_error(item.mock_error, "mock")
                raise QBotAPIError(
                    item.mock_error,
                    "mock 注入错误: " + reason,
                    retryable,
                )
            await asyncio.sleep(0.05)
            return {
                "type": part.type,
                "mock": True,
                "msg_id": "mock_" + uuid.uuid4().hex[:8],
            }

        if part.type == "text":
            text = part.text or ""
            if not text.strip():
                raise QBotAPIError(None, "文本内容为空", False)
            if len(text) > config.MAX_MESSAGE_LENGTH:
                raise QBotAPIError(
                    40054007,
                    "文本长度 " + str(len(text)) + " 超过上限 " + str(config.MAX_MESSAGE_LENGTH),
                    False,
                )
            return await self._post_group_message(item.group_openid, msg_type=0, content=text)

        if part.type == "markdown":
            markdown_text = part.markdown or ""
            if not markdown_text.strip():
                raise QBotAPIError(None, "Markdown 内容为空", False)
            return await self._post_group_message(
                item.group_openid,
                msg_type=2,
                markdown={"content": markdown_text},
            )

        if part.type == "image":
            return await self._send_image(item.group_openid, part.source or "")

        if part.type == "card":
            return await self._send_card(item.group_openid, part.card or {})

        raise QBotAPIError(None, "不支持的 part 类型: " + part.type, False)

    async def _post_group_message(self, group_openid: str, **kwargs: Any) -> Dict[str, Any]:
        """作用：调用 botpy 的群消息接口，并统一检查业务错误码。"""
        if not self.client:
            raise QBotAPIError(None, "botpy Client 未初始化", False)

        msg_seq = self.state.next_seq(group_openid)
        try:
            result = await self.client.api.post_group_message(
                group_openid=group_openid,
                msg_seq=msg_seq,
                **kwargs,
            )
        except QBotAPIError:
            raise
        except Exception as exc:
            raise self._wrap_botpy_exception(exc)

        return self._check_result(result, "发送消息")

    async def _send_card(
        self,
        group_openid: str,
        card: Dict[str, str],
    ) -> Dict[str, Any]:
        """
        作用：发送 msg_type=8 图文卡片。
        说明：botpy 1.2.1 的 post_group_message 没有 card 参数，走底层 HTTP 请求。
        """
        if not self.client:
            raise QBotAPIError(None, "botpy Client 未初始化", False)

        title = str(card.get("title") or "").strip()
        description = str(card.get("description") or "").strip()
        url = str(card.get("url") or "").strip()
        pic_url = str(card.get("pic_url") or "").strip()
        if not title or not description or not url or not pic_url:
            raise QBotAPIError(None, "图文卡片字段不完整", False)

        payload = {
            "msg_type": 8,
            "card": {
                "type": "tuwen",
                "content": {
                    "title": title,
                    "description": description,
                    "url": url,
                    "pic_url": pic_url,
                },
            },
            "msg_seq": self.state.next_seq(group_openid),
        }
        route = Route(
            "POST",
            "/v2/groups/{group_openid}/messages",
            group_openid=group_openid,
        )
        try:
            result = await self.client.api._http.request(route, json=payload)
        except Exception as exc:
            raise self._wrap_botpy_exception(exc)

        return self._check_result(result, "发送图文卡片")

    async def send_passive_reply(
        self,
        group_openid: str,
        msg_id: str,
        content: str,
        msg_seq: int = 1,
    ) -> Dict[str, Any]:
        """作用：按 msg_id 做被动回复，不走主动消息队列和主动频控。"""
        if self.mock:
            await asyncio.sleep(0.05)
            self.passive_reply_count += 1
            return {"id": "mock_reply_" + uuid.uuid4().hex[:8]}

        if not self.client:
            raise QBotAPIError(None, "botpy Client 未初始化", False)

        try:
            result = await self.client.api.post_group_message(
                group_openid=group_openid,
                msg_id=msg_id,
                msg_seq=msg_seq,
                content=content,
            )
        except QBotAPIError:
            raise
        except Exception as exc:
            raise self._wrap_botpy_exception(exc)

        checked = self._check_result(result, "被动回复")
        self.passive_reply_count += 1
        return checked

    def _check_result(self, result: Any, action: str) -> Dict[str, Any]:
        """作用：把 botpy 返回值统一成字典，并识别 code 非 0 的业务错误。"""
        if result is None:
            raise QBotAPIError(None, action + "无返回（可能超时）", True)

        if isinstance(result, str):
            try:
                result = json.loads(result)
            except Exception:
                raise QBotAPIError(None, action + "返回非 JSON: " + result[:200], True)

        if not isinstance(result, dict):
            raise QBotAPIError(None, action + "返回格式异常: " + str(result)[:200], True)

        raw_code = result.get("code")
        if raw_code is None:
            raw_code = result.get("err_code", 0)
        if raw_code in (0, "0", None):
            return result

        try:
            code = int(raw_code)
        except (TypeError, ValueError):
            code = None

        message = str(result.get("message") or result.get("msg") or "")
        retryable, reason = classify_error(code, message)
        raise QBotAPIError(
            code,
            action + "失败: " + reason + " | " + str(result)[:300],
            retryable,
        )

    def _wrap_botpy_exception(self, exc: Exception) -> QBotAPIError:
        """作用：把 botpy 抛出的 HTTP 异常映射成 QBotAPIError。"""
        if isinstance(exc, AuthenticationFailedError):
            return QBotAPIError(401, "鉴权失败: " + str(exc), False)
        if isinstance(exc, ForbiddenError):
            return QBotAPIError(403, "无权限: " + str(exc), False)
        if isinstance(exc, NotFoundError):
            return QBotAPIError(404, "接口或群不存在: " + str(exc), False)
        if isinstance(exc, MethodNotAllowedError):
            return QBotAPIError(405, "接口方法不允许: " + str(exc), False)
        if isinstance(exc, SequenceNumberError):
            return QBotAPIError(429, "发送超频: " + str(exc), True)
        if isinstance(exc, ServerError):
            return QBotAPIError(500, "官方服务端错误: " + str(exc), True)
        if isinstance(exc, (asyncio.TimeoutError, aiohttp.ClientError, OSError)):
            return QBotAPIError(None, "网络错误: " + type(exc).__name__ + ": " + str(exc), True)
        return QBotAPIError(None, "未知异常: " + type(exc).__name__ + ": " + str(exc), True)

    async def _send_image(self, group_openid: str, source: str) -> Dict[str, Any]:
        """作用：发图完整流程：上传拿 file_info -> msg_type=7 发送。"""
        if not source:
            raise QBotAPIError(None, "图片路径或 URL 为空", False)

        normalized = source
        if normalized.startswith("file:///"):
            normalized = normalized[8:]

        media: Optional[Dict[str, Any]] = None
        if normalized.startswith("http://") or normalized.startswith("https://"):
            try:
                media = await self._upload_url_image(group_openid, normalized)
            except QBotAPIError as exc:
                if not (exc.retryable or exc.code in MEDIA_FALLBACK_CODES):
                    raise
                logger.warning(
                    "URL 图片上传失败，改为下载后二进制上传: code=%s msg=%s",
                    exc.code,
                    exc.message,
                )
                data = await self._download_image(normalized)
                media = await self._upload_image_bytes(
                    group_openid,
                    data,
                    "image_" + uuid.uuid4().hex[:8] + ".png",
                )
        else:
            path = Path(normalized)
            if not path.is_file():
                raise QBotAPIError(None, "本地图片不存在: " + normalized, False)
            data = path.read_bytes()
            media = await self._upload_image_bytes(group_openid, data, path.name)

        if not media:
            raise QBotAPIError(None, "图片上传未返回 file_info", False)
        file_info = media.get("file_info")
        if not file_info:
            raise QBotAPIError(None, "图片上传返回缺少 file_info", False)

        # file_info 有时效且群级别隔离：这里只用一次，不跨群缓存。
        return await self._post_group_message(
            group_openid,
            msg_type=7,
            media={"file_info": file_info},
        )

    async def _upload_url_image(self, group_openid: str, url: str) -> Dict[str, Any]:
        """作用：把公网 URL 交给官方转存，返回上传结果。"""
        if not self.client:
            raise QBotAPIError(None, "botpy Client 未初始化", False)
        try:
            result = await self.client.api.post_group_file(
                group_openid=group_openid,
                file_type=1,
                url=url,
                srv_send_msg=False,
            )
        except Exception as exc:
            raise self._wrap_botpy_exception(exc)
        return self._check_result(result, "上传图片URL")

    async def _upload_image_bytes(
        self,
        group_openid: str,
        data: bytes,
        filename: str,
    ) -> Dict[str, Any]:
        """
        作用：本地图片走二进制直传，返回含 file_info 的结果。
        说明：botpy 1.2.1 的 post_group_file 只接受 url，不暴露 file_data；
              这里直接用它的 _http.request 走同一个鉴权/沙箱域名。
        """
        if not self.client:
            raise QBotAPIError(None, "botpy Client 未初始化", False)
        if len(data) > config.MEDIA_BINARY_MAX_BYTES:
            raise QBotAPIError(
                None,
                "图片 " + str(len(data)) + " 字节超过二进制直传上限 "
                + str(config.MEDIA_BINARY_MAX_BYTES),
                False,
            )

        encoded = base64.b64encode(data).decode("ascii")
        route = Route(
            "POST",
            "/v2/groups/{group_openid}/files",
            group_openid=group_openid,
        )
        payload = {
            "file_type": 1,
            "file_name": filename,
            "file_data": encoded,
            "srv_send_msg": False,
        }
        try:
            result = await self.client.api._http.request(route, json=payload)
        except Exception as exc:
            raise self._wrap_botpy_exception(exc)

        logger.info(
            "图片二进制上传完成: group_openid=%s file=%s bytes=%s",
            group_openid,
            filename,
            len(data),
        )
        return self._check_result(result, "上传图片")

    async def _download_image(self, url: str) -> bytes:
        """作用：下载远程图片，供 URL 转存失败时回退使用。"""
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
            ),
            "Referer": "https://www.bilibili.com/",
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    headers=headers,
                    timeout=config.IMAGE_DOWNLOAD_TIMEOUT,
                ) as response:
                    if response.status != 200:
                        raise QBotAPIError(
                            None,
                            "图片下载失败 HTTP " + str(response.status),
                            True,
                        )
                    data = await response.read()
        except QBotAPIError:
            raise
        except Exception as exc:
            raise QBotAPIError(None, "图片下载异常: " + str(exc), True)

        if len(data) > config.MEDIA_BINARY_MAX_BYTES:
            raise QBotAPIError(None, "下载图片超过二进制直传上限", False)
        return data

    def _failure(
        self,
        item: QueueItem,
        error: QBotAPIError,
        part_results: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """作用：生成失败的统一响应。"""
        return {
            "code": error.code if error.code is not None else 1,
            "data": {
                "request_id": item.request_id,
                "group": item.group_alias,
                "error_code": error.code,
                "retryable": error.retryable,
                "attempts": item.attempts,
                "parts": part_results,
            },
            "msg": error.message,
        }

    def _check_local_auth(self, request: web.Request) -> bool:
        """作用：校验本地 HTTP 共享口令；未配置口令时放行。"""
        if not config.QBOT_SEND_TOKEN:
            return True
        auth_header = request.headers.get("Authorization", "")
        token_header = request.headers.get("X-QBOT-Token", "")
        return (
            auth_header == "Bearer " + config.QBOT_SEND_TOKEN
            or token_header == config.QBOT_SEND_TOKEN
        )

    async def handle_send(self, request: web.Request) -> web.Response:
        """作用：处理 POST /official_send。"""
        if not self._check_local_auth(request):
            return web.json_response(
                {"code": 401, "data": None, "msg": "本地共享口令错误"},
                status=401,
            )

        try:
            payload = await request.json()
        except Exception:
            return web.json_response(
                {"code": 400, "data": None, "msg": "请求不是合法 JSON"},
                status=400,
            )
        if not isinstance(payload, dict):
            return web.json_response(
                {"code": 400, "data": None, "msg": "请求体必须是对象"},
                status=400,
            )

        try:
            parts = normalize_request_parts(payload)
        except ValueError as exc:
            return web.json_response(
                {"code": 400, "data": None, "msg": str(exc)},
                status=400,
            )

        group_key = str(payload.get("group_key") or self.group_key)
        group_alias = payload.get("group")
        if group_alias is not None:
            group_alias = str(group_alias)
        request_id = str(payload.get("request_id") or uuid.uuid4().hex[:12])
        wait = bool(payload.get("wait", False))
        mock_error = payload.get("mock_error")
        if mock_error is not None:
            try:
                mock_error = int(mock_error)
            except (TypeError, ValueError):
                return web.json_response(
                    {"code": 400, "data": None, "msg": "mock_error 必须是整数"},
                    status=400,
                )

        result = await self.enqueue(
            group_key=group_key,
            parts=parts,
            wait=wait,
            request_id=request_id,
            group_alias=group_alias,
            mock_error=mock_error,
        )
        status = 200
        if result.get("code") in (401, 503, 504):
            status = 503 if result.get("code") == 503 else 200
        return web.json_response(result, status=status)

    async def handle_alias(self, request: web.Request) -> web.Response:
        """作用：处理 POST /official_alias；修改群别名。"""
        if not self._check_local_auth(request):
            return web.json_response(
                {"code": 401, "data": None, "msg": "本地共享口令错误"},
                status=401,
            )

        try:
            payload = await request.json()
        except Exception:
            return web.json_response(
                {"code": 400, "data": None, "msg": "请求不是合法 JSON"},
                status=400,
            )
        if not isinstance(payload, dict):
            return web.json_response(
                {"code": 400, "data": None, "msg": "请求体必须是对象"},
                status=400,
            )

        result = self.rename_group(payload.get("old"), payload.get("new"))
        if result.get("code") == 400:
            status = 400
        elif result.get("code") == 404:
            status = 404
        elif result.get("code") == 409:
            status = 409
        else:
            status = 200
        return web.json_response(result, status=status)

    async def handle_reply(self, request: web.Request) -> web.Response:
        """作用：处理 POST /official_reply；用入站 msg_id 做被动回复。"""
        if not self._check_local_auth(request):
            return web.json_response(
                {"code": 401, "data": None, "msg": "本地共享口令错误"},
                status=401,
            )

        try:
            payload = await request.json()
        except Exception:
            return web.json_response(
                {"code": 400, "data": None, "msg": "请求不是合法 JSON"},
                status=400,
            )
        if not isinstance(payload, dict):
            return web.json_response(
                {"code": 400, "data": None, "msg": "请求体必须是对象"},
                status=400,
            )

        group_alias = str(payload.get("group") or "")
        msg_id = str(payload.get("msg_id") or "")
        content = str(payload.get("content") or "")
        try:
            msg_seq = int(payload.get("msg_seq", 1))
        except (TypeError, ValueError):
            return web.json_response(
                {"code": 400, "data": None, "msg": "msg_seq 必须是整数"},
                status=400,
            )

        if not group_alias:
            return web.json_response(
                {"code": 400, "data": None, "msg": "缺少 group"},
                status=400,
            )
        if not msg_id:
            return web.json_response(
                {"code": 400, "data": None, "msg": "缺少 msg_id"},
                status=400,
            )
        if not content.strip():
            return web.json_response(
                {"code": 400, "data": None, "msg": "content 为空"},
                status=400,
            )
        if msg_seq < 1 or msg_seq > 5:
            return web.json_response(
                {"code": 400, "data": None, "msg": "msg_seq 必须在 1~5"},
                status=400,
            )
        if len(content) > config.MAX_MESSAGE_LENGTH:
            return web.json_response(
                {"code": 400, "data": None, "msg": "content 超过长度上限"},
                status=400,
            )

        group_openid = self.group_openids.get(group_alias)
        if not group_openid:
            return web.json_response(
                {
                    "code": 400,
                    "data": {"available_groups": list(self.group_openids.keys())},
                    "msg": "未知的群别名: " + group_alias,
                },
                status=400,
            )

        try:
            result = await self.send_passive_reply(
                group_openid,
                msg_id,
                content,
                msg_seq,
            )
        except QBotAPIError as exc:
            logger.error(
                "被动回复失败: group=%s msg_id=%s code=%s msg=%s",
                group_alias,
                msg_id,
                exc.code,
                exc.message,
            )
            return web.json_response(
                {
                    "code": exc.code if exc.code is not None else 1,
                    "data": {
                        "group": group_alias,
                        "error_code": exc.code,
                        "retryable": exc.retryable,
                    },
                    "msg": exc.message,
                }
            )

        logger.info(
            "被动回复成功: group=%s msg_id=%s msg_seq=%s",
            group_alias,
            msg_id,
            msg_seq,
        )
        return web.json_response(
            {
                "code": 0,
                "data": {
                    "group": group_alias,
                    "msg_id": msg_id,
                    "msg_seq": msg_seq,
                    "result": result,
                },
                "msg": "ok",
            }
        )

    async def handle_health(self, request: web.Request) -> web.Response:
        """作用：返回发送器健康状态，供 probe 和 72h 观察使用。"""
        groups = {}
        total_queue = 0
        for alias, openid in self.group_openids.items():
            queue = self.queues.get(openid)
            queue_size = queue.qsize() if queue else 0
            total_queue += queue_size
            groups[alias] = {
                "openid_tail": openid[-6:],
                "source": "auto" if alias in self.auto_groups else "config",
                "queue_size": queue_size,
                "daily_sent": self.state.get_daily(openid),
                "active_allowed": self.group_active_allowed.get(openid),
            }

        discovered = []
        list_path = config.DATA_DIR / "discovered_group_openids.txt"
        if list_path.is_file():
            try:
                for line in list_path.read_text(encoding="utf-8").splitlines():
                    openid = line.strip()
                    if not openid:
                        continue
                    alias = next(
                        (a for a, o in self.group_openids.items() if o == openid),
                        None,
                    )
                    discovered.append(
                        {
                            "openid": openid,
                            "alias": alias,
                            "configured": bool(alias),
                            "source": (
                                "auto"
                                if alias in self.auto_groups
                                else ("config" if alias else None)
                            ),
                        }
                    )
            except Exception:
                discovered = []

        default_openid = self.group_openid
        data = {
            "env": self.group_key,
            "mock": self.mock,
            "ws_ready": self.ready.is_set(),
            "group_openid_configured": bool(self.group_openids),
            "default_group": self.default_group,
            "groups": groups,
            "discovered_groups": discovered,
            "queue_size": total_queue,
            "sent": self.sent_count,
            "failed": self.failed_count,
            "dropped": self.dropped_count,
            "passive_replies": self.passive_reply_count,
            "active_allowed": self.group_active_allowed.get(default_openid) if default_openid else None,
            "daily_sent": self.state.get_daily(default_openid) if default_openid else 0,
            "uptime_seconds": int(time.time() - self.started_at),
        }
        return web.json_response({"code": 0, "data": data, "msg": "ok"})

    async def close(self):
        """作用：停止所有 worker 并关闭 botpy 客户端。"""
        tasks = list(self.worker_tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.worker_tasks.clear()
        if self.client:
            try:
                await self.client.close()
            except Exception as exc:
                logger.warning("关闭 botpy Client 失败: %s", exc)
            self.client = None


