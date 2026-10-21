# pinned_card_renderer.py
# 置顶评论卡片渲染：评论内容走 DOM，元信息走 API，Playwright 输出固定宽度 PNG
# 仅用于 screenshot 模式，替代旧的 B站页面元素截图
import asyncio
import base64
import html
import json
import os
import re
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp
from bs4 import BeautifulSoup

from config import (
    BASE_DIR,
    CARD_ASSET_CACHE_DIR,
    CARD_AVATAR_SIZE,
    CARD_COMMENT_IMAGE_WIDTH,
    CARD_DYNAMIC_CACHE_FILE,
    CARD_META_FONT_SIZE,
    CARD_MIN_HEIGHT,
    CARD_NAME_FONT_SIZE,
    CARD_PADDING,
    CARD_PROFILE_CACHE_TTL,
    CARD_SCALE_FACTOR,
    CARD_SIGNATURE_PATH,
    CARD_SIGNATURE_WIDTH,
    CARD_TEXT_FONT_SIZE,
    CARD_TIME_SOURCE,
    CARD_WIDTH,
    MAIL_SAVE_DIR,
    UP_NAME,
    UP_UID,
)
from logger_config import logger


DETAIL_API = "https://api.bilibili.com/x/polymer/web-dynamic/v1/detail?id={dynamic_id}"
REPLY_API = (
    "https://api.bilibili.com/x/v2/reply"
    "?type={comment_type}&oid={comment_oid}&sort=2&ps=20&pn=1"
)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
DEFAULT_NICKNAME_COLOR = "#FB7299"


# ============================================================
# 基础工具
# ============================================================

def _cookie_header(cookies: Any) -> str:
    """把 Playwright cookies 列表或字符串转成 Cookie 请求头"""
    if not cookies:
        return ""
    if isinstance(cookies, str):
        return cookies
    try:
        return "; ".join(
            f"{c.get('name')}={c.get('value')}"
            for c in cookies
            if c.get("name") and c.get("value") is not None
        )
    except Exception:
        return ""


def _resolve_path(path_value: Any) -> Path:
    """相对路径按项目根目录解析，绝对路径原样返回"""
    path = Path(str(path_value))
    if not path.is_absolute():
        path = BASE_DIR / path
    return path


def _file_to_data_uri(path: Path) -> Optional[str]:
    """把本地图片文件转成 data URI，失败返回 None"""
    try:
        if not path.exists():
            return None
        suffix = path.suffix.lower()
        mime = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".avif": "image/avif",
            ".gif": "image/gif",
            ".svg": "image/svg+xml",
        }.get(suffix, "image/png")
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{mime};base64,{data}"
    except Exception as e:
        logger.warning(f"读取本地图片失败 {path}: {e}")
        return None


def _load_json_file(path: Path, default: Any) -> Any:
    """读取 JSON 文件，不存在或损坏时返回默认值"""
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"读取缓存失败 {path}: {e}")
    return default


def _save_json_file(path: Path, data: Any):
    """原子写入 JSON 文件，避免写一半损坏缓存"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(path.name + ".tmp")
        tmp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp_path, path)
    except Exception as e:
        logger.warning(f"写入缓存失败 {path}: {e}")


def _safe_text(value: Any) -> str:
    """把任意值转成安全文本并做 HTML 转义"""
    return html.escape("" if value is None else str(value))


def _normalize_url(url: Any) -> str:
    """补全 B站图片 URL 的协议，http 统一改 https"""
    value = ("" if url is None else str(url)).strip()
    if value.startswith("//"):
        return "https:" + value
    if value.startswith("http://"):
        return "https://" + value[len("http://"):]
    return value


def _safe_color(value: Any) -> str:
    """只接受 #RRGGBB 颜色，其他值用默认粉色"""
    if isinstance(value, str) and re.match(r"^#[0-9a-fA-F]{6}$", value):
        return value
    return DEFAULT_NICKNAME_COLOR


# ============================================================
# B站 API
# ============================================================

def _get_json_sync(url: str, cookie_str: str, referer: str = "https://t.bilibili.com/") -> dict:
    """同步 GET 一个 B站 JSON 接口"""
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": referer,
        "Origin": "https://t.bilibili.com",
    }
    if cookie_str:
        headers["Cookie"] = cookie_str
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    if data.get("code") != 0:
        raise RuntimeError(
            f"B站接口错误 code={data.get('code')} msg={data.get('message')}"
        )
    return data


async def _get_comment_basic(dynamic_id: str, cookie_str: str) -> Dict[str, Any]:
    """
    获取动态对应的评论区 oid / type。

    注意：评论接口的 oid 不是动态 ID，必须用 basic.comment_id_str。
    """
    cache = _load_json_file(CARD_DYNAMIC_CACHE_FILE, {})
    cache_key = str(dynamic_id)
    cached = cache.get(cache_key) or {}
    if cached.get("comment_id_str") and cached.get("comment_type") is not None:
        return cached

    url = DETAIL_API.format(dynamic_id=dynamic_id)
    data = await asyncio.to_thread(_get_json_sync, url, cookie_str)
    item = (data.get("data") or {}).get("item") or {}
    basic = item.get("basic") or {}
    comment_oid = str(basic.get("comment_id_str") or "")
    comment_type = basic.get("comment_type")
    if not comment_oid or comment_type is None:
        raise RuntimeError("detail API 未返回 comment_id_str/comment_type")

    entry = {
        "comment_id_str": comment_oid,
        "comment_type": comment_type,
        "saved_at": time.time(),
    }
    cache[cache_key] = entry

    # 控制缓存条数，防止无限增长
    if len(cache) > 50:
        ordered = sorted(cache.items(), key=lambda kv: kv[1].get("saved_at", 0))
        cache = dict(ordered[-50:])
    _save_json_file(CARD_DYNAMIC_CACHE_FILE, cache)
    logger.info(f"卡片: 动态 {dynamic_id} 评论区 oid={comment_oid} type={comment_type}")
    return entry


async def _fetch_reply_info(
    dynamic_id: str, cookie_str: str, dom_rpid: Optional[str]
) -> Optional[Dict[str, Any]]:
    """
    获取置顶评论的 API 元信息。

    Returns:
        {"reply": 原始评论, "rpid_match": bool}；接口失败返回 None。
    """
    basic = await _get_comment_basic(dynamic_id, cookie_str)
    url = REPLY_API.format(
        comment_type=basic["comment_type"],
        comment_oid=basic["comment_id_str"],
    )
    data = await asyncio.to_thread(_get_json_sync, url, cookie_str)
    reply_data = data.get("data") or {}
    top_replies = reply_data.get("top_replies") or []
    replies = reply_data.get("replies") or []

    chosen = None
    rpid_match = False
    if dom_rpid:
        for raw in list(top_replies) + list(replies):
            if str(raw.get("rpid")) == str(dom_rpid):
                chosen = raw
                rpid_match = True
                break

    # DOM 没拿到 rpid 时，仍保留 top_replies[0] 供日志/兜底观察，
    # 但调用方只会在 rpid_match=True 时使用它的 IP/时间/作者。
    if chosen is None and top_replies:
        chosen = top_replies[0]

    if chosen is None:
        return None
    return {"reply": chosen, "rpid_match": rpid_match}


def _find_avatar_layer_url(avatar_item: Any) -> str:
    """从 B站 avatar_item 图层里找头像合成图 URL"""
    avatar_urls: List[str] = []
    other_urls: List[str] = []

    def walk(obj: Any):
        if isinstance(obj, dict):
            tags = ((obj.get("layer_config") or {}).get("tags") or {})
            resource = obj.get("resource") or {}
            image_src = ((resource.get("res_image") or {}).get("image_src") or {})
            remote = (image_src.get("remote") or {})
            url = remote.get("url") or ""
            if url:
                if "AVATAR_LAYER" in tags:
                    avatar_urls.append(url)
                else:
                    other_urls.append(url)
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)

    walk(avatar_item)
    if avatar_urls:
        return avatar_urls[0]
    if other_urls:
        return other_urls[0]
    return ""


def _extract_author(member: dict) -> Dict[str, Any]:
    """从评论 member 中提取卡片头部需要的作者信息"""
    member = member or {}
    vip = member.get("vip") or {}
    pendant = member.get("pendant") or {}
    nft_region = (member.get("nft_interaction") or {}).get("region") or {}

    nickname_color = vip.get("nickname_color") or vip.get("nicknameColor")
    composite_url = _find_avatar_layer_url(member.get("avatar_item"))
    return {
        "mid": str(member.get("mid") or UP_UID),
        "name": member.get("uname") or UP_NAME,
        "nickname_color": _safe_color(nickname_color),
        "avatar_url": member.get("avatar") or member.get("face") or "",
        "composite_url": composite_url,
        "avatar_round": not bool(composite_url),
        "pendant_url": pendant.get("image_enhance") or pendant.get("image") or "",
        "badge_url": nft_region.get("icon") or "",
    }


def _format_ctime(ctime: Any, fallback: str) -> str:
    """把 B站 ctime 转成北京时间字符串，失败用检测时间兜底"""
    try:
        dt = datetime.fromtimestamp(int(ctime), tz=timezone.utc) + timedelta(hours=8)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return fallback


# ============================================================
# 头像 / 头像框缓存（B2）
# ============================================================

def _profile_paths(mid: str):
    """返回某个 mid 的资料缓存 JSON 和头像 PNG 路径"""
    CARD_ASSET_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe_mid = re.sub(r"[^0-9A-Za-z_-]", "_", str(mid or UP_UID))
    return (
        CARD_ASSET_CACHE_DIR / f"profile_{safe_mid}.json",
        CARD_ASSET_CACHE_DIR / f"avatar_{safe_mid}.png",
    )


def _load_profile(mid: str, allow_expired: bool = False) -> Optional[Dict[str, Any]]:
    """读取头像/头像框缓存；默认过期返回 None"""
    json_path, avatar_path = _profile_paths(mid)
    data = _load_json_file(json_path, None)
    if not isinstance(data, dict):
        return None
    if not allow_expired:
        saved_at = float(data.get("saved_at") or 0)
        if CARD_PROFILE_CACHE_TTL > 0 and time.time() - saved_at > CARD_PROFILE_CACHE_TTL:
            return None
    if avatar_path.exists():
        data["avatar_path"] = str(avatar_path)
    else:
        data["avatar_path"] = ""
    return data


def _load_latest_profile() -> Optional[Dict[str, Any]]:
    """API 完全失败时，用最近一次保存的头像资料兜底"""
    try:
        CARD_ASSET_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        files = sorted(
            CARD_ASSET_CACHE_DIR.glob("profile_*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for json_path in files:
            data = _load_json_file(json_path, None)
            if not isinstance(data, dict):
                continue
            avatar_path = Path(str(data.get("avatar_path") or ""))
            if avatar_path.exists():
                data["avatar_path"] = str(avatar_path)
                return data
    except Exception as e:
        logger.warning(f"读取最近头像缓存失败: {e}")
    return None


def _save_profile(mid: str, author: Dict[str, Any], avatar_path: Optional[Path]):
    """保存头像/头像框缓存"""
    json_path, _ = _profile_paths(mid)
    data = {
        "mid": str(mid),
        "uname": author.get("name", ""),
        "nickname_color": author.get("nickname_color", DEFAULT_NICKNAME_COLOR),
        "avatar_url": author.get("avatar_url", ""),
        "composite_url": author.get("composite_url", ""),
        "avatar_round": bool(author.get("avatar_round")),
        "pendant_url": author.get("pendant_url", ""),
        "badge_url": author.get("badge_url", ""),
        "avatar_path": str(avatar_path) if avatar_path and avatar_path.exists() else "",
        "saved_at": time.time(),
    }
    _save_json_file(json_path, data)


# ============================================================
# 评论内容渲染
# ============================================================

def _sanitize_dom_html(raw_html: str) -> str:
    """
    清洗 DOM 评论 HTML：补全图片协议、去掉链接、限制表情尺寸。

    评论正文本身来自 B站 DOM，这里只做安全/样式归一化，不改变文字。
    """
    if not raw_html:
        return ""
    try:
        soup = BeautifulSoup(raw_html, "html.parser")
        for tag in soup(["script", "style", "iframe", "video", "audio"]):
            tag.decompose()
        for link in soup.find_all("a"):
            link.unwrap()
        for img in soup.find_all("img"):
            src = (img.get("src") or "").strip()
            if not src:
                img.decompose()
                continue
            src = _normalize_url(src)
            if not src:
                img.decompose()
                continue
            img["src"] = src
            img["class"] = "emote"
            img["loading"] = "eager"
            img["onerror"] = (
                "this.replaceWith(document.createTextNode(this.alt || ''))"
            )
        return str(soup)
    except Exception as e:
        logger.warning(f"卡片: 清洗 DOM 评论失败: {e}")
        return _safe_text(BeautifulSoup(raw_html, "html.parser").get_text(" ", strip=True))


def _render_api_content(content: dict) -> str:
    """API 兜底：把 message + emote 渲染成 HTML 片段"""
    if not isinstance(content, dict):
        return ""
    message = html.escape(str(content.get("message") or ""))
    emotes = content.get("emote") or {}
    if isinstance(emotes, dict):
        for key, info in emotes.items():
            if not isinstance(info, dict):
                continue
            url = _normalize_url(info.get("url"))
            if not url:
                continue
            meta = info.get("meta") or {}
            size = meta.get("size", 1) if isinstance(meta, dict) else 1
            css_class = "emote emote-large" if size == 2 else "emote"
            escaped_key = html.escape(str(key))
            img_tag = (
                f'<img class="{css_class}" src="{html.escape(url)}" '
                f'alt="{escaped_key}" title="{escaped_key}" '
                f'onerror="this.replaceWith(document.createTextNode(this.alt || \'\'))">'
            )
            message = message.replace(escaped_key, img_tag)
    return message.replace("\n", "<br>")


def _api_picture_urls(content: dict) -> List[str]:
    """从 API content.pictures 提取图片 URL"""
    result: List[str] = []
    if not isinstance(content, dict):
        return result
    for pic in content.get("pictures") or []:
        if not isinstance(pic, dict):
            continue
        url = _normalize_url(pic.get("img_src"))
        if url and url not in result:
            result.append(url)
    return result


# ============================================================
# 图片下载 / data URI
# ============================================================

def _cdn_thumb_url(url: str, width: int, height: int) -> str:
    """给 B站 CDN 图片加缩略图后缀，降低下载体积"""
    if not url:
        return ""
    if "hdslb.com" not in url or "@" in url:
        return url
    return f"{url}@{width}w_{height}h_1c_1s.webp"


def _cdn_comment_image_url(url: str) -> str:
    """评论图片取原图，保证满宽显示时清晰"""
    if not url:
        return ""
    if "hdslb.com" in url and "@" in url:
        return url.split("@")[0]
    return url


async def _download_image_data_uri(
    session: aiohttp.ClientSession, url: str
) -> Optional[str]:
    """下载图片并转成 data URI，失败返回 None"""
    if not url:
        return None
    try:
        async with session.get(
            url,
            headers={"User-Agent": USER_AGENT, "Referer": "https://t.bilibili.com/"},
            timeout=15,
        ) as resp:
            if resp.status != 200:
                logger.warning(f"卡片: 图片下载 HTTP {resp.status}: {url[:100]}")
                return None
            data = await resp.read()
            if not data:
                return None
            content_type = resp.headers.get("Content-Type", "").split(";")[0].strip()
            if not content_type.startswith("image/"):
                content_type = "image/webp" if ".webp" in url else "image/png"
            encoded = base64.b64encode(data).decode("ascii")
            return f"data:{content_type};base64,{encoded}"
    except Exception as e:
        logger.warning(f"卡片: 图片下载失败 {url[:100]}: {e}")
        return None


async def _download_image_to_file(url: str, base_path: Path) -> Optional[Path]:
    """下载头像/头像框图片到本地缓存，按 Content-Type 选择扩展名"""
    if not url:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                headers={"User-Agent": USER_AGENT, "Referer": "https://t.bilibili.com/"},
                timeout=15,
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"卡片: 头像下载 HTTP {resp.status}: {url[:100]}")
                    return None
                data = await resp.read()
                if not data:
                    return None
                content_type = resp.headers.get("Content-Type", "").split(";")[0].strip()
                ext = {
                    "image/webp": ".webp",
                    "image/png": ".png",
                    "image/jpeg": ".jpg",
                    "image/avif": ".avif",
                    "image/gif": ".gif",
                }.get(content_type)
                if not ext:
                    logger.warning(f"卡片: 头像类型不支持 {content_type}: {url[:100]}")
                    return None
                path = base_path.with_suffix(ext)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                return path
    except Exception as e:
        logger.warning(f"卡片: 头像下载失败 {url[:100]}: {e}")
        return None


# ============================================================
# HTML 组装
# ============================================================

def _build_comment_images_html(
    image_data_uris: List[str], remote_urls: List[str]
) -> str:
    """评论图片区域：优先 data URI，失败用远程 URL"""
    parts: List[str] = []
    if CARD_COMMENT_IMAGE_WIDTH and CARD_COMMENT_IMAGE_WIDTH > 0:
        width_css = f"width:{CARD_COMMENT_IMAGE_WIDTH}px;max-width:100%;"
    else:
        # 0 表示铺满内容区，和正文同宽
        width_css = "width:100%;max-width:100%;"
    for data_uri in image_data_uris:
        if not data_uri:
            continue
        parts.append(
            f'<img src="{html.escape(data_uri)}" alt="评论图片" '
            f'style="{width_css}height:auto;display:block;">'
        )
    for url in remote_urls:
        if not url:
            continue
        parts.append(
            f'<img src="{html.escape(url)}" alt="评论图片" '
            f'style="{width_css}height:auto;display:block;" '
            f'onerror="this.style.display=\'none\'">'
        )
    if not parts:
        return ""
    return '<div class="images">' + "".join(parts) + "</div>"


def _build_card_html(
    author: Dict[str, Any],
    avatar_data_uri: Optional[str],
    signature_data_uri: Optional[str],
    timestamp: str,
    location: str,
    content_html: str,
    images_html: str,
) -> str:
    """拼出完整的卡片 HTML（自包含 CSS）"""
    name = html.escape(str(author.get("name") or UP_NAME))
    name_color = _safe_color(author.get("nickname_color"))
    meta_parts = [html.escape(timestamp)]
    if location:
        meta_parts.append(html.escape(location))
    meta_text = "&nbsp;&nbsp;&nbsp;".join(meta_parts)

    if avatar_data_uri:
        avatar_class = "avatar-round" if author.get("avatar_round") else ""
        avatar_html = (
            f'<div class="avatar-box"><img class="{avatar_class}" '
            f'src="{html.escape(avatar_data_uri)}" alt="头像"></div>'
        )
    else:
        placeholder = html.escape((author.get("name") or UP_NAME)[:1])
        avatar_html = f'<div class="avatar-box avatar-placeholder">{placeholder}</div>'

    signature_html = ""
    if signature_data_uri:
        signature_html = (
            f'<div class="signature"><img src="{html.escape(signature_data_uri)}" '
            f'alt="签名"></div>'
        )

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<style>
    * {{ box-sizing: border-box; }}
    html, body {{ margin: 0; padding: 0; background: #fff; }}
    body {{
        font-family: 'Microsoft YaHei', 'PingFang SC', 'Noto Sans CJK SC', Arial, sans-serif;
        -webkit-font-smoothing: antialiased;
    }}
    #card {{
        width: {CARD_WIDTH}px;
        min-height: {CARD_MIN_HEIGHT}px;
        background: #fff;
        padding: {CARD_PADDING}px;
        display: flex;
        flex-direction: column;
    }}
    .header {{
        display: flex;
        align-items: center;
        gap: 28px;
    }}
    .avatar-box {{
        width: {CARD_AVATAR_SIZE}px;
        height: {CARD_AVATAR_SIZE}px;
        flex: 0 0 {CARD_AVATAR_SIZE}px;
        display: flex;
        align-items: center;
        justify-content: center;
    }}
    .avatar-box img {{
        max-width: 100%;
        max-height: 100%;
        display: block;
    }}
    .avatar-box img.avatar-round {{
        border-radius: 50%;
        overflow: hidden;
    }}
    .avatar-placeholder {{
        border-radius: 50%;
        background: #f2f2f2;
        color: #999;
        font-size: 52px;
        font-weight: 600;
    }}
    .author-info {{
        min-width: 0;
    }}
    .name {{
        font-size: {CARD_NAME_FONT_SIZE}px;
        font-weight: 600;
        line-height: 1.2;
        color: {name_color};
        word-break: break-word;
    }}
    .meta {{
        margin-top: 12px;
        font-size: {CARD_META_FONT_SIZE}px;
        color: #999;
        line-height: 1.3;
    }}
    .content {{
        margin-top: 44px;
        font-size: {CARD_TEXT_FONT_SIZE}px;
        color: #222;
        line-height: 1.6;
        word-break: break-word;
        overflow-wrap: anywhere;
    }}
    .content img.emote {{
        width: auto;
        height: 1.25em;
        max-width: 2em;
        vertical-align: -0.2em;
        margin: 0 2px;
    }}
    .content img.emote-large {{
        height: 2.2em;
        max-width: 3em;
    }}
    .images {{
        display: flex;
        flex-wrap: wrap;
        gap: 14px;
        margin-top: 28px;
    }}
    .images img {{
        border-radius: 6px;
    }}
    .spacer {{
        flex: 1 1 auto;
        min-height: 40px;
    }}
    .signature {{
        align-self: flex-end;
        margin-top: 24px;
    }}
    .signature img {{
        width: {CARD_SIGNATURE_WIDTH}px;
        max-width: 100%;
        height: auto;
        display: block;
    }}
</style>
</head>
<body>
<div id="card">
    <div class="header">
        {avatar_html}
        <div class="author-info">
            <div class="name">{name}</div>
            <div class="meta">{meta_text}</div>
        </div>
    </div>
    <div class="content">{content_html}{images_html}</div>
    <div class="spacer"></div>
    {signature_html}
</div>
</body>
</html>"""


# ============================================================
# Playwright 渲染
# ============================================================

async def render_card_png(browser, html_text: str, output_path: Path) -> Optional[str]:
    """用 Playwright 把卡片 HTML 渲染成固定宽度 PNG"""
    context = None
    try:
        try:
            scale_factor = max(1.0, float(CARD_SCALE_FACTOR))
        except (TypeError, ValueError):
            scale_factor = 1.0
        context = await browser.new_context(device_scale_factor=scale_factor)
        page = await context.new_page()
        await page.set_viewport_size({"width": CARD_WIDTH, "height": 1200})
        await page.set_content(html_text, wait_until="load")
        try:
            await page.evaluate("document.fonts.ready")
        except Exception:
            pass
        try:
            await page.wait_for_function(
                "() => Array.from(document.images).every(img => img.complete)",
                timeout=8000,
            )
        except Exception:
            logger.warning("卡片: 等待图片加载超时，继续截图")
        await asyncio.sleep(0.2)

        card = page.locator("#card")
        await card.wait_for(state="visible", timeout=5000)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        await card.screenshot(path=str(output_path), type="png")
        logger.info(f"卡片: 输出成功 {output_path}")
        return str(output_path)
    except Exception as e:
        logger.warning(f"卡片: 渲染失败: {e}")
        return None
    finally:
        if context:
            try:
                await context.close()
            except Exception:
                pass


# ============================================================
# 对外入口
# ============================================================

async def generate_pinned_card(
    browser,
    dynamic_id: str,
    current_html: str,
    current_images: List[str],
    rpid: Optional[str],
    cookies: Any,
    detect_time: str,
    output_dir: Optional[Path] = None,
) -> Optional[str]:
    """
    生成置顶评论卡片 PNG。

    Args:
        browser: Playwright Browser（用于卡片渲染）
        dynamic_id: 动态 ID
        current_html: DOM 抓到的评论正文 HTML
        current_images: DOM 抓到的评论图片 URL
        rpid: DOM 抓到的评论 rpid，用于和 API 校验
        cookies: Playwright cookies 列表
        detect_time: 检测时间字符串（ctime 不可用时兜底）
        output_dir: 输出目录，默认 MAIL_SAVE_DIR
    Returns:
        PNG 路径；失败返回 None（调用方回退旧截图）
    """
    try:
        output_dir = Path(output_dir or MAIL_SAVE_DIR)
        output_dir.mkdir(parents=True, exist_ok=True)
        cookie_str = _cookie_header(cookies)

        # 1) API 元信息
        reply_info = None
        try:
            reply_info = await _fetch_reply_info(dynamic_id, cookie_str, rpid)
            if reply_info is None:
                logger.warning("卡片: 评论 API 未返回置顶评论，使用缓存/检测时间")
            elif not reply_info.get("rpid_match"):
                logger.warning(
                    f"卡片: DOM rpid={rpid} 与 API rpid 不匹配，"
                    f"不使用该条 API 的 IP/时间/作者"
                )
        except Exception as e:
            logger.warning(f"卡片: 评论 API 失败，使用缓存/检测时间: {e}")

        reply = (reply_info or {}).get("reply") or {}
        rpid_match = bool((reply_info or {}).get("rpid_match"))
        member = reply.get("member") or {}
        content = reply.get("content") or {}
        reply_control = reply.get("reply_control") or {}

        author: Dict[str, Any] = {}
        if rpid_match and member:
            author = _extract_author(member)

        # 2) 头像缓存（API 合成头像）
        mid = author.get("mid") or UP_UID
        avatar_path: Optional[Path] = None
        cached = _load_profile(mid, allow_expired=False)
        if cached and cached.get("avatar_path"):
            avatar_path = Path(cached["avatar_path"])
            if not author:
                author = {
                    "mid": cached.get("mid", mid),
                    "name": cached.get("uname") or UP_NAME,
                    "nickname_color": cached.get("nickname_color"),
                    "avatar_url": cached.get("avatar_url", ""),
                    "composite_url": cached.get("composite_url", ""),
                    "avatar_round": bool(cached.get("avatar_round")),
                }
        else:
            # API 失败时用最近一次缓存
            if not author:
                latest = _load_latest_profile()
                if latest:
                    author = {
                        "mid": latest.get("mid", mid),
                        "name": latest.get("uname") or UP_NAME,
                        "nickname_color": latest.get("nickname_color"),
                        "avatar_url": latest.get("avatar_url", ""),
                        "composite_url": latest.get("composite_url", ""),
                        "avatar_round": bool(latest.get("avatar_round")),
                    }
                    if latest.get("avatar_path"):
                        avatar_path = Path(latest["avatar_path"])

            # 缓存过期/缺失：下载 API 合成头像并写回缓存
            if author and not avatar_path:
                avatar_url = author.get("composite_url") or author.get("avatar_url")
                if avatar_url:
                    _, base_png = _profile_paths(mid)
                    downloaded = await _download_image_to_file(
                        _cdn_thumb_url(avatar_url, 254, 254), base_png
                    )
                    if downloaded:
                        avatar_path = downloaded
                        _save_profile(mid, author, downloaded)

            # 下载失败时退回过期缓存
            if not avatar_path:
                old = _load_profile(mid, allow_expired=True)
                if old and old.get("avatar_path"):
                    avatar_path = Path(old["avatar_path"])

        if not author:
            author = {
                "mid": UP_UID,
                "name": UP_NAME,
                "nickname_color": DEFAULT_NICKNAME_COLOR,
                "avatar_url": "",
                "composite_url": "",
                "avatar_round": True,
            }

        # 3) 正文：优先 DOM，失败用 API 内容
        content_html = _sanitize_dom_html(current_html)
        image_urls: List[str] = []
        for url in current_images or []:
            url = _normalize_url(url)
            if url and url not in image_urls:
                image_urls.append(url)

        if not content_html:
            content_html = _render_api_content(content)
        if not image_urls:
            image_urls = _api_picture_urls(content)

        if not content_html and not image_urls:
            logger.warning("卡片: 评论正文和图片都为空，放弃生成")
            return None

        # 4) 下载图片 + 签名
        avatar_data_uri = _file_to_data_uri(avatar_path) if avatar_path else None
        signature_path = _resolve_path(CARD_SIGNATURE_PATH)
        signature_data_uri = _file_to_data_uri(signature_path)
        if not signature_data_uri:
            fallback_signature = BASE_DIR / "assets" / "signature_example.png"
            signature_data_uri = _file_to_data_uri(fallback_signature)
        if not signature_data_uri:
            logger.warning(f"卡片: 签名图片不存在 {signature_path}，本次不渲染签名")

        async with aiohttp.ClientSession() as session:
            if not avatar_data_uri:
                avatar_url = author.get("composite_url") or author.get("avatar_url")
                if avatar_url:
                    thumb_url = _cdn_thumb_url(avatar_url, 254, 254)
                    avatar_data_uri = await _download_image_data_uri(session, thumb_url)
                    if not avatar_data_uri and thumb_url != avatar_url:
                        avatar_data_uri = await _download_image_data_uri(session, avatar_url)

            image_data_uris: List[str] = []
            remote_image_urls: List[str] = []
            for url in image_urls:
                thumb_url = _cdn_comment_image_url(url)
                data_uri = await _download_image_data_uri(session, thumb_url)
                if data_uri:
                    image_data_uris.append(data_uri)
                else:
                    remote_image_urls.append(url)

        images_html = _build_comment_images_html(image_data_uris, remote_image_urls)

        # 5) 时间 / IP
        if CARD_TIME_SOURCE == "detect" or not rpid_match:
            timestamp = detect_time
        else:
            timestamp = _format_ctime(reply.get("ctime"), detect_time)
        location = (reply_control.get("location") or "") if rpid_match else ""

        # 6) 组装 + 渲染
        card_html = _build_card_html(
            author=author,
            avatar_data_uri=avatar_data_uri,
            signature_data_uri=signature_data_uri,
            timestamp=timestamp,
            location=location,
            content_html=content_html,
            images_html=images_html,
        )
        ts = time.strftime("%Y%m%d%H%M%S")
        output_path = output_dir / f"pinned_card_{dynamic_id}_{ts}.png"
        return await render_card_png(browser, card_html, output_path)

    except Exception as e:
        logger.warning(f"卡片: 生成异常，回退旧截图: {e}")
        return None
