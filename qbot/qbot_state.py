# qbot_state.py
# 队列数据、限速器、msg_seq/每日计数持久化、HTTP 请求体规范化。
import asyncio
import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("qbot")

# 官方图文卡片（msg_type=8）字段上限，超限平台直接 400。
CARD_TITLE_MAX = 50
CARD_DESCRIPTION_MAX = 200

@dataclass
class SendPart:
    """一个待发送内容片段。"""

    type: str
    text: Optional[str] = None
    source: Optional[str] = None
    markdown: Optional[str] = None
    card: Optional[Dict[str, str]] = None


@dataclass
class QueueItem:
    """队列里的逻辑消息；多个 parts 按顺序发送。"""

    request_id: str
    group_openid: str
    group_alias: str
    parts: List[SendPart]
    created_at: float
    future: Optional[asyncio.Future] = None
    attempts: int = 0
    mock_error: Optional[int] = None


class RateLimiter:
    """简单滑动窗口限速；qpm 表示每分钟最多多少次。"""

    def __init__(self, qpm: int):
        self.qpm = max(1, int(qpm))
        self.sent_at = deque()

    async def acquire(self):
        """作用：拿到一个发送名额；超限时睡到窗口滑动。"""
        while True:
            now = time.monotonic()
            while self.sent_at and now - self.sent_at[0] >= 60:
                self.sent_at.popleft()
            if len(self.sent_at) < self.qpm:
                self.sent_at.append(now)
                return
            wait_seconds = 60 - (now - self.sent_at[0]) + 0.05
            await asyncio.sleep(max(wait_seconds, 0.1))


class StateStore:
    """持久化 msg_seq 和每日计数，避免进程重启后重复 msg_seq 被官方去重。"""

    def __init__(self, path: Path):
        self.path = path
        self.data = {"seq": {}, "daily": {"date": "", "counts": {}}}
        self._load()

    def _load(self):
        """作用：读取本地状态；文件损坏时重建，不影响启动。"""
        try:
            if self.path.is_file():
                raw = self.path.read_text(encoding="utf-8")
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    self.data["seq"] = parsed.get("seq") or {}
                    daily = parsed.get("daily") or {}
                    if isinstance(daily, dict):
                        self.data["daily"] = {
                            "date": daily.get("date") or "",
                            "counts": daily.get("counts") or {},
                        }
        except Exception as exc:
            logger.warning("读取状态文件失败，使用内存状态: %s", exc)

    def _save(self):
        """作用：原子写入状态文件；失败只告警，不阻断发送。"""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.path.with_suffix(".tmp")
            with open(tmp_path, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(self.data, handle, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self.path)
        except Exception as exc:
            logger.warning("写入状态文件失败: %s", exc)

    def next_seq(self, group_openid: str) -> int:
        """作用：为一次发送分配单调递增的 msg_seq。"""
        current = int(self.data["seq"].get(group_openid, 0))
        current += 1
        self.data["seq"][group_openid] = current
        self._save()
        return current

    def _today(self) -> str:
        return time.strftime("%Y-%m-%d")

    def get_daily(self, group_openid: str) -> int:
        """作用：返回某群当天已发送条数。"""
        if self.data["daily"].get("date") != self._today():
            self.data["daily"] = {"date": self._today(), "counts": {}}
            self._save()
        return int(self.data["daily"]["counts"].get(group_openid, 0))

    def incr_daily(self, group_openid: str):
        """作用：某群当天发送条数 +1。"""
        count = self.get_daily(group_openid) + 1
        self.data["daily"]["counts"][group_openid] = count
        self._save()


def normalize_part(raw: Any) -> SendPart:
    """
    作用：把 HTTP 请求里的 part 规范成 SendPart。
    入参：raw 字典，支持 type=text/image/markdown。
    出参：SendPart；参数不合法时抛 ValueError。
    """
    if not isinstance(raw, dict):
        raise ValueError("part 必须是对象")

    part_type = str(raw.get("type") or "").strip().lower()
    if part_type == "text":
        text = raw.get("text")
        if text is None:
            text = raw.get("content", "")
        return SendPart(type="text", text=str(text))

    if part_type == "image":
        source = (
            raw.get("source")
            or raw.get("path")
            or raw.get("url")
            or raw.get("file_path")
            or raw.get("content")
        )
        return SendPart(type="image", source=str(source or ""))

    if part_type == "markdown":
        markdown = raw.get("markdown")
        if markdown is None:
            markdown = raw.get("content", "")
        return SendPart(type="markdown", markdown=str(markdown))

    if part_type == "card":
        title = str(raw.get("title") or "").strip()
        description = str(raw.get("description") or "").strip()
        url = str(raw.get("url") or "").strip()
        pic_url = str(raw.get("pic_url") or "").strip()
        if not title:
            raise ValueError("card.title 不能为空")
        if len(title) > CARD_TITLE_MAX:
            raise ValueError("card.title 超过 " + str(CARD_TITLE_MAX) + " 字")
        if not description:
            raise ValueError("card.description 不能为空")
        if len(description) > CARD_DESCRIPTION_MAX:
            raise ValueError(
                "card.description 超过 " + str(CARD_DESCRIPTION_MAX) + " 字"
            )
        if not url.startswith(("http://", "https://")):
            raise ValueError("card.url 必须是 http(s) 地址")
        if not pic_url.startswith(("http://", "https://")):
            raise ValueError("card.pic_url 必须是 http(s) 地址")
        return SendPart(
            type="card",
            card={
                "title": title,
                "description": description,
                "url": url,
                "pic_url": pic_url,
            },
        )

    raise ValueError("不支持的 part 类型: " + part_type)


def next_auto_alias(existing_aliases: Any) -> str:
    """作用：生成下一个未占用的 auto_N 别名。"""
    used = set(existing_aliases or [])
    index = 1
    while "auto_" + str(index) in used:
        index += 1
    return "auto_" + str(index)


def normalize_group_map(value: Any) -> Dict[str, str]:
    """
    作用：把 config 里的 GROUP_OPENIDS 值规范成“群别名 -> openid”。
    入参：字符串（旧单群）、字典（多群）、列表（自动 group1/group2）。
    出参：{别名: openid}；空值会被过滤。
    """
    if isinstance(value, str):
        return {"default": value} if value else {}

    if isinstance(value, dict):
        result = {}
        for key, openid in value.items():
            if key and openid:
                result[str(key)] = str(openid)
        return result

    if isinstance(value, (list, tuple)):
        result = {}
        for index, openid in enumerate(value):
            if openid:
                result["group" + str(index + 1)] = str(openid)
        return result

    return {}


def normalize_request_parts(payload: Dict[str, Any]) -> List[SendPart]:
    """
    作用：兼容两种契约：parts 数组，或老的 type/content 单条格式。
    入参：HTTP JSON。
    出参：SendPart 列表。
    """
    raw_parts = payload.get("parts")
    if raw_parts is not None:
        if not isinstance(raw_parts, list) or not raw_parts:
            raise ValueError("parts 必须是非空数组")
        return [normalize_part(item) for item in raw_parts]

    part_type = str(payload.get("type") or "").strip().lower()
    if not part_type:
        raise ValueError("缺少 parts 或 type")

    legacy = {"type": part_type}
    content = payload.get("content")
    if part_type == "image":
        legacy["source"] = payload.get("file_path") or content
    elif part_type == "markdown":
        legacy["markdown"] = content
    elif part_type == "card":
        legacy.update({
            "title": payload.get("title"),
            "description": payload.get("description"),
            "url": payload.get("url"),
            "pic_url": payload.get("pic_url"),
        })
    else:
        legacy["text"] = content
    return [normalize_part(legacy)]


