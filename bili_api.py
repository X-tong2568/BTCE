# bili_api.py
"""B站 API 轻量客户端——调用 polymer web-dynamic 新版接口拿动态列表，仅需 Cookie 即可"""
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

    async def get_dynamics(self, uid: str) -> list[dict]:
        """
        获取用户空间动态列表（用 urllib 同步请求，asyncio.to_thread 包装）。
        返回: (list[dict], bool) — 列表 + 是否成功
        """
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
        items = api_data.get("items", [])
        result = []
        for item in items:
            # 新版结构: item.id_str / item.type / item.modules.module_dynamic
            dyn_id = item.get("id_str", "")
            dyn_type = item.get("type", "")

            modules = item.get("modules", {})
            mod_dyn = modules.get("module_dynamic", {})

            # desc 可能为 None（纯图动态）
            desc = mod_dyn.get("desc")
            content = (desc.get("text", "") if desc else "")

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
                "images": images,
                "timestamp": 0,  # 新版接口此层无 timestamp，不影响新动态检测
            })

        return result, True
