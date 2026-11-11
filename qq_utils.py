# qq_utils.py
import aiohttp
import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from logger_config import logger
from config_qq import (
    QQ_BOT_API_URL,
    QQ_BOT_ACCESS_TOKEN,
    QQ_GROUP_IDS,
    QQ_PUSH_ENABLED,
    MAX_MESSAGE_LENGTH,
    QQ_NAPCAT_ENABLED,
    QQ_OFFICIAL_ENABLED,
    QQ_OFFICIAL_GROUPS,
    QQBOT_SEND_URL,
    QQBOT_SEND_TOKEN,
    QQ_OFFICIAL_WAIT_SECONDS,
)
from qq_message_generator import qq_message_generator

# NapCat 的 sendMsg IPC 超时约 10 秒；原始消息失败后等这个窗口过去再补降级消息。
DEGRADED_RETRY_DELAY_SECONDS = 10
# qbot 的文本上限是 2000；官方通道按此截断每个文本 part。
OFFICIAL_TEXT_MAX_LENGTH = 2000
_CQ_IMAGE_RE = re.compile(r"\[CQ:image,[^\]]*?file=([^,\]]+)[^\]]*\]")
_OFFICIAL_STATE_PATH = Path(__file__).parent / "data" / "official_push_state.json"


def _normalize_alias_list(value: Any) -> List[str]:
    """把配置/状态里的别名规范成字符串列表。"""
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if item]
    return []


def load_official_groups() -> List[str]:
    """读取官方推送状态；文件不存在时用 config 的 QQ_OFFICIAL_GROUPS 播种。"""
    if _OFFICIAL_STATE_PATH.is_file():
        try:
            data = json.loads(_OFFICIAL_STATE_PATH.read_text(encoding="utf-8"))
            return _normalize_alias_list(data.get("enabled"))
        except Exception as exc:
            logger.warning("读取官方推送状态失败: %s", exc)
    aliases = _normalize_alias_list(QQ_OFFICIAL_GROUPS)
    save_official_groups(aliases)
    return aliases


def save_official_groups(aliases: List[str]):
    """原子写入官方推送状态。"""
    try:
        _OFFICIAL_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = _OFFICIAL_STATE_PATH.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump({"enabled": aliases}, handle, ensure_ascii=False, indent=2)
        os.replace(tmp_path, _OFFICIAL_STATE_PATH)
    except Exception as exc:
        logger.error("写入官方推送状态失败: %s", exc)


def set_official_group(alias: str, enabled: bool) -> bool:
    """开启/关闭某个官方群的 B站推送；改文件后下一次发送立即生效。"""
    alias = str(alias or "").strip()
    if not alias:
        return False
    aliases = load_official_groups()
    if enabled and alias not in aliases:
        aliases.append(alias)
    elif not enabled and alias in aliases:
        aliases.remove(alias)
    save_official_groups(aliases)
    logger.info("官方推送状态更新: alias=%s enabled=%s", alias, enabled)
    return True


def _parse_cq_message(message: str) -> List[Dict[str, Any]]:
    """把 BTCE 现有 CQ 文本拆成 qbot 需要的 text/image parts。"""
    parts: List[Dict[str, Any]] = []
    text = message or ""
    last_index = 0
    for match in _CQ_IMAGE_RE.finditer(text):
        if match.start() > last_index:
            plain_text = text[last_index:match.start()]
            if plain_text:
                parts.append({"type": "text", "text": plain_text})
        source = match.group(1).strip()
        if source.startswith("file:///"):
            source = source[8:]
        if source:
            parts.append({"type": "image", "source": source})
        last_index = match.end()
    if last_index < len(text):
        plain_text = text[last_index:]
        if plain_text:
            parts.append({"type": "text", "text": plain_text})
    if not parts and text:
        parts.append({"type": "text", "text": text})
    return parts


class QQMessageSender:
    """QQ群消息发送器：NapCat 与官方 Bot 双通道，两个推送列表独立维护。"""

    def __init__(self):
        self.api_url = QQ_BOT_API_URL
        self.access_token = QQ_BOT_ACCESS_TOKEN
        self.headers = {}
        if self.access_token:
            self.headers = {"Authorization": "Bearer " + self.access_token}

    async def _send_napcat(self, group_id: str, message: str, is_degraded: bool = False) -> bool:
        """原 NapCat 发送逻辑；只负责 NapCat，不判断官方开关。"""
        if len(message) > MAX_MESSAGE_LENGTH:
            message = message[:MAX_MESSAGE_LENGTH - 3] + "..."

        max_retries = 1
        message_type = "降级消息" if is_degraded else "普通消息"

        for attempt in range(max_retries):
            try:
                async with aiohttp.ClientSession() as session:
                    payload = {
                        "group_id": group_id,
                        "message": message,
                        "auto_escape": False,
                    }
                    async with session.post(
                        f"{self.api_url}/send_group_msg",
                        json=payload,
                        headers=self.headers,
                        timeout=30,
                    ) as response:
                        if response.status == 200:
                            result = await response.json()
                            if result.get("status") == "ok":
                                logger.info("✅ QQ群 %s %s发送成功", group_id, message_type)
                                return True
                            logger.error(
                                "❌❌ QQ群 %s %s第%s次发送失败: %s",
                                group_id,
                                message_type,
                                attempt + 1,
                                result,
                            )
                        else:
                            logger.error(
                                "❌❌ QQ群 %s %s第%s次API请求失败: %s",
                                group_id,
                                message_type,
                                attempt + 1,
                                response.status,
                            )
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    logger.info("等待%s秒后重试...", wait_time)
                    await asyncio.sleep(wait_time)
            except asyncio.TimeoutError:
                logger.error(
                    "❌❌ QQ群 %s %s第%s次消息发送超时",
                    group_id,
                    message_type,
                    attempt + 1,
                )
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    logger.info("等待%s秒后重试...", wait_time)
                    await asyncio.sleep(wait_time)
            except Exception as exc:
                logger.error(
                    "❌❌ QQ群 %s %s第%s次发送异常: %s",
                    group_id,
                    message_type,
                    attempt + 1,
                    exc,
                )
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    logger.info("等待%s秒后重试...", wait_time)
                    await asyncio.sleep(wait_time)

        logger.error("❌❌ QQ群 %s %s发送失败，已重试%s次", group_id, message_type, max_retries)
        return False

    async def _send_official(self, alias: str, message: str) -> Optional[bool]:
        """官方通道：CQ -> parts，POST qbot；异常只记日志，不冒泡。"""
        if not QQ_OFFICIAL_ENABLED:
            return None

        alias = str(alias or "").strip()
        if not alias:
            return None

        parts = _parse_cq_message(message)
        for part in parts:
            if part.get("type") == "text":
                text = str(part.get("text") or "")
                if len(text) > OFFICIAL_TEXT_MAX_LENGTH:
                    logger.warning(
                        "官方通道文本超过 %s 字，已截断: alias=%s",
                        OFFICIAL_TEXT_MAX_LENGTH,
                        alias,
                    )
                    part["text"] = text[:OFFICIAL_TEXT_MAX_LENGTH - 3] + "..."
        if not parts:
            return None

        wait_seconds = int(QQ_OFFICIAL_WAIT_SECONDS or 0)
        payload = {
            "group": alias,
            "parts": parts,
            "wait": wait_seconds > 0,
            "request_id": "btce_" + alias + "_" + str(int(time.time() * 1000)),
        }
        headers = {"Content-Type": "application/json"}
        if QQBOT_SEND_TOKEN:
            headers["Authorization"] = "Bearer " + QQBOT_SEND_TOKEN

        try:
            timeout = wait_seconds + 10 if wait_seconds > 0 else 10
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    QQBOT_SEND_URL,
                    json=payload,
                    headers=headers,
                    timeout=timeout,
                ) as response:
                    result = await response.json(content_type=None)
                    if response.status >= 400:
                        logger.error(
                            "官方QQ通道 HTTP %s: alias=%s body=%s",
                            response.status,
                            alias,
                            result,
                        )
                        return False
        except Exception as exc:
            logger.error("官方QQ通道异常，已忽略: alias=%s err=%s", alias, exc)
            return False

        if isinstance(result, dict) and result.get("code") == 0:
            logger.info("官方QQ通道已受理: alias=%s parts=%s", alias, len(parts))
            return True

        code = result.get("code") if isinstance(result, dict) else None
        msg = result.get("msg") if isinstance(result, dict) else result
        logger.error("官方QQ通道失败: alias=%s code=%s msg=%s", alias, code, msg)
        return False

    def _build_targets(self) -> List[Tuple[str, str]]:
        """NapCat 群列表 + 官方别名列表，两个列表独立维护。"""
        targets: List[Tuple[str, str]] = []
        if QQ_NAPCAT_ENABLED:
            targets.extend(("napcat", str(group_id)) for group_id in QQ_GROUP_IDS)
        if QQ_OFFICIAL_ENABLED:
            official_groups = load_official_groups()
            targets.extend(("official", str(alias)) for alias in official_groups if alias)
        return targets

    async def _send_target(
        self,
        target: Tuple[str, str],
        message: str,
        is_degraded: bool = False,
    ) -> bool:
        """按 (channel, value) 发送一个目标。"""
        channel, value = target
        if channel == "napcat":
            return await self._send_napcat(value, message, is_degraded)
        if channel == "official":
            result = await self._send_official(value, message)
            return bool(result) if result is not None else True
        logger.error("未知推送通道: %s", channel)
        return False

    async def _send_to_targets(
        self,
        targets: List[Tuple[str, str]],
        message: str,
        is_degraded: bool = False,
    ) -> List[bool]:
        """并发发送到多个目标，异常按失败处理。"""
        if not targets:
            return []
        tasks = [self._send_target(target, message, is_degraded) for target in targets]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        processed: List[bool] = []
        for target, result in zip(targets, results):
            if isinstance(result, Exception):
                logger.error("❌❌ 推送异常: target=%s err=%s", target, result)
                processed.append(False)
            else:
                processed.append(bool(result))
        return processed

    async def send_group_message(self, group_id: str, message: str, is_degraded: bool = False) -> bool:
        """兼容旧调用：只走 NapCat。双通道统一走 send_to_all_groups。"""
        if not QQ_PUSH_ENABLED:
            logger.info("QQ推送已禁用，跳过发送")
            return True
        return await self._send_napcat(group_id, message, is_degraded)

    async def send_to_all_groups(self, message: str) -> List[bool]:
        """向 NapCat 群列表和官方别名列表发送原始消息。"""
        if not QQ_PUSH_ENABLED:
            logger.info("QQ推送已禁用，跳过发送")
            return []
        targets = self._build_targets()
        if not targets:
            logger.warning("NapCat/官方通道都未配置推送目标，跳过发送")
            return []
        results = await self._send_to_targets(targets, message, False)
        success_count = sum(1 for result in results if result)
        total_count = len(results)
        if success_count == total_count:
            logger.info("✅ 所有推送目标发送成功 (%s/%s)", success_count, total_count)
        else:
            logger.warning("⚠️ 推送结果: %s成功, %s失败", success_count, total_count - success_count)
        return results

    async def send_with_degradation(self, original_message: str, message_data: dict) -> List[bool]:
        """带降级机制：原始消息失败的目标，等 NapCat 超时窗口后补降级消息。"""
        if not QQ_PUSH_ENABLED:
            logger.info("QQ推送已禁用，跳过发送")
            return []

        targets = self._build_targets()
        if not targets:
            logger.warning("NapCat/官方通道都未配置推送目标，跳过发送")
            return []

        original_results = await self._send_to_targets(targets, original_message, False)
        failed_indices = [index for index, result in enumerate(original_results) if not result]
        if not failed_indices:
            logger.info("✅ 所有推送目标原始消息发送成功")
            return original_results

        logger.warning("⚠️ 检测到 %s 个推送目标失败，启用降级机制", len(failed_indices))
        await asyncio.sleep(DEGRADED_RETRY_DELAY_SECONDS)

        if message_data.get("degraded_type") == "new_dynamic":
            degraded_message = qq_message_generator.generate_new_dynamic_degraded_message(
                up_name=message_data.get("up_name", ""),
                dynamic_id=message_data.get("dynamic_id", ""),
                current_time=message_data.get("current_time", ""),
            )
        else:
            degraded_message = qq_message_generator.generate_degraded_message(
                up_name=message_data.get("up_name", ""),
                dynamic_id=message_data.get("dynamic_id", ""),
                current_html=message_data.get("current_html", ""),
                current_time=message_data.get("current_time", ""),
                current_images=message_data.get("current_images", []),
                original_failed=True,
            )

        final_results = list(original_results)
        for index in failed_indices:
            target = targets[index]
            result = await self._send_target(target, degraded_message, True)
            final_results[index] = result
            if result:
                logger.info("✅ 降级消息发送成功: target=%s", target)
            else:
                logger.error("❌❌ 降级消息发送失败: target=%s", target)

        return final_results


# 全局实例
qq_sender = QQMessageSender()


async def send_qq_message(message: str, message_data: Optional[dict] = None) -> List[bool]:
    """发送QQ群消息（便捷函数）：NapCat 列表 + 官方列表分别维护。"""
    if message_data and any(key in message_data for key in ["up_name", "dynamic_id", "current_html"]):
        return await qq_sender.send_with_degradation(message, message_data)
    return await qq_sender.send_to_all_groups(message)
