# bili_api.py
"""B站 API 轻量客户端——调用 polymer web-dynamic 新版接口拿动态列表，仅需 Cookie 即可"""
import html
import json
import asyncio
from pathlib import Path
from logger_config import logger

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

# 新版 API（2026.06 旧版 api.vc.bilibili.com 已下线，换成 polymer 接口）
FEED_SPACE_URL = (
    "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
    "?host_mid={uid}"
)


def _escape_html(text: str) -> str:
    """HTML 转义（文本节点用）"""
    return html.escape(text or "", quote=False)


def _build_content_html(desc: dict, fallback_text: str) -> str:
    """把动态富文本节点转成 HTML：B站表情还原成内联图片，归档后可直接显示"""
    nodes = desc.get("rich_text_nodes") or []
    if not nodes:
        return _escape_html(fallback_text).replace("\n", "<br>")

    parts = []
    for node in nodes:
        node_type = node.get("type", "")
        text = node.get("text", "") or ""
        if node_type == "RICH_TEXT_NODE_TYPE_EMOJI":
            emoji = node.get("emoji") or {}
            icon = emoji.get("icon_url") or emoji.get("webp_url") or ""
            name = (emoji.get("text") or text).strip().strip("[]")
            if icon:
                parts.append(
                    f'<img class="emoji" alt="[{html.escape(name, quote=True)}]" '
                    f'src="{html.escape(icon, quote=True)}">'
                )
                continue
        parts.append(_escape_html(text).replace("\n", "<br>"))
    return "".join(parts)


class BiliAPI:
    """B站动态列表 API 客户端"""

    def __init__(self, cookie_file: Path):
        self.cookie_file = cookie_file

    def _load_cookie_str(self) -> str:
        """加载Cookie文件，返回Cookie字符串"""
        try:
            if not self.cookie_file.exists():
                return ""
            cookies = json.loads(self.cookie_file.read_text(encoding="utf-8"))
            return "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        except Exception as e:
            logger.error(f"Cookie加载失败: {e}")
            return ""

    async def close(self):
        pass  # 无状态，不需要清理

    async def _fetch_raw(self, uid: str) -> tuple[list[dict], bool]:
        """调用 API 并返回原始 items 列表（含完整 modules），供需要详情的模块使用"""
        import urllib.request

        cookie_str = self._load_cookie_str()
        url = FEED_SPACE_URL.format(uid=uid)

        def _sync_request():
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Referer": f"https://space.bilibili.com/{uid}/dynamic",
                    "Cookie": cookie_str,
                }
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())

        try:
            data = await asyncio.to_thread(_sync_request)
        except Exception as e:
            logger.error(f"API请求失败: {e}")
            return [], False

        if data.get("code") != 0:
            logger.error(f"API返回异常 code={data.get('code')} msg={data.get('message')}")
            return [], False

        api_data = data.get("data") or {}
        return api_data.get("items", []), True

    async def get_dynamics(self, uid: str) -> list[dict]:
        """
        获取用户空间动态列表（精简版，供新动态检测用）。
        返回: (list[dict], bool) — 列表 + 是否成功
        """
        items, ok = await self._fetch_raw(uid)
        if not ok:
            return [], False

        result = []
        for item in items:
            dyn_id = item.get("id_str", "")
            dyn_type = item.get("type", "")

            modules = item.get("modules", {})
            mod_dyn = modules.get("module_dynamic", {})

            # desc 可能为 None（纯图动态）
            desc = mod_dyn.get("desc") or {}
            content = desc.get("text", "") or ""
            content_html = _build_content_html(desc, content)

            # 提取图片列表（漫画/动态图片）
            major = mod_dyn.get("major") or {}
            images = []
            draw_items = (major.get("draw") or {}).get("items", [])
            for di in draw_items:
                src = di.get("src", "")
                if src:
                    if src.startswith("//"):
                        src = "https:" + src
                    elif src.startswith("http://"):
                        src = src.replace("http://", "https://")
                    images.append(src)

            result.append({
                "dynamic_id": dyn_id,
                "type": dyn_type,
                "content": content.strip(),
                "content_html": content_html.strip(),
                "images": images,
                "timestamp": 0,
                "module_tag": (modules.get("module_tag") or {}).get("text", ""),
            })

        return result, True
