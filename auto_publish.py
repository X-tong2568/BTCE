# auto_publish.py
"""
B站动态自动发布模块。
- 置顶评论变更：上传截图+发布带话题的图文动态
- 直播间标题更新：下载封面图+上传图床+发布带话题的图文动态
"""

import json
import asyncio
import time
from config import BILI_PUBLISH_TEXT, BILI_PC_LINK_LABEL, BILI_MOBILE_LINK_LABEL
import random
import hashlib
import tempfile
from typing import Optional
import aiohttp
from pathlib import Path
from datetime import datetime
from logger_config import logger


BILI_UPLOAD_URL = "https://api.bilibili.com/x/dynamic/feed/draw/upload_bfs"
BILI_PUBLISH_URL = "https://api.bilibili.com/x/dynamic/feed/create/dyn"


def _csrf_from_cookies(cookies: list) -> str:
    """从cookie列表中提取bili_jct作为CSRF token"""
    for c in cookies:
        if c.get("name") == "bili_jct":
            return c["value"]
    return ""


def _mid_from_cookies(cookies: list) -> str:
    """从cookie列表中提取DedeUserID作为用户mid"""
    for c in cookies:
        if c.get("name") == "DedeUserID":
            return c["value"]
    return ""


def _cookie_header(cookies: list) -> str:
    """cookie列表转为请求头字符串"""
    return "; ".join(f'{c["name"]}={c["value"]}' for c in cookies)


async def upload_image(file_path: str, cookies: list) -> Optional[dict]:
    """
    上传截图到B站图床。
    返回图片信息dict（含image_url/image_width/image_height/img_size），失败返回None。
    """
    csrf = _csrf_from_cookies(cookies)
    if not csrf:
        logger.error("❌ auto_publish: 未找到bili_jct cookie，无法上传图片")
        return None
    cookie_str = _cookie_header(cookies)

    try:
        img_bytes = Path(file_path).read_bytes()
        filename = Path(file_path).name
        form = aiohttp.FormData()
        form.add_field("file_up", img_bytes, filename=filename, content_type="image/png")
        form.add_field("category", "daily")
        form.add_field("biz", "new_dyn")
        form.add_field("csrf", csrf)

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Referer": "https://t.bilibili.com/",
            "Origin": "https://t.bilibili.com",
            "Cookie": cookie_str,
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(BILI_UPLOAD_URL, data=form, headers=headers, timeout=30) as resp:
                raw = await resp.text()
                try:
                    result = json.loads(raw)
                except json.JSONDecodeError:
                    logger.error(f"❌ 图片上传返回非JSON: HTTP {resp.status} body={raw[:200]}")
                    return None
                if result.get("code") == 0:
                    data = result["data"]
                    logger.info(f"📤 图片上传成功: {data['image_url']} ({data['img_size']}KB)")
                    return {
                        "img_src": data["image_url"],
                        "img_width": data["image_width"],
                        "img_height": data["image_height"],
                        "img_size": data["img_size"],
                    }
                else:
                    logger.error(f"❌ 图片上传失败: code={result.get('code')} msg={result.get('message')}")
                    return None
    except Exception as e:
        logger.error(f"❌ 图片上传异常: {e}")
        return None


async def publish_dynamic(dynamic_id: str, screenshot_path: str, cookies: list,
                          up_name: str, topic_id: int, topic_name: str, rpid: str = None,
                          comment_text: str = None) -> bool:
    """
    发布B站动态（图文+话题+超链接）。
    返回True表示发布成功，False表示失败。
    """
    csrf = _csrf_from_cookies(cookies)
    if not csrf:
        logger.error("❌ auto_publish: 未找到bili_jct cookie，无法发布动态")
        return False
    cookie_str = _cookie_header(cookies)

    # 1) 上传图片
    img = await upload_image(screenshot_path, cookies)
    if not img:
        logger.warning("⚠️ auto_publish: 图片上传失败，跳过发布")
        return False

    # 2) 组装动态内容
    reply_url = f"https://t.bilibili.com/{dynamic_id}#reply{rpid}" if rpid else ""
    link_url = f"https://t.bilibili.com/{dynamic_id}?comment_on=1&spm_id_from=333.1387.0.0"
    mid = _mid_from_cookies(cookies)
    upload_id = f"{mid}_{int(time.time())}_{random.randint(1000, 9999)}"

    contents = [
        {"raw_text": f"【{up_name}】{BILI_PUBLISH_TEXT}", "type": 1, "biz_id": ""},
    ]
    if rpid:
        contents.append({"raw_text": f"\n{BILI_PC_LINK_LABEL} {reply_url}", "type": 1, "biz_id": ""})
    contents.append({"raw_text": f"\n{BILI_MOBILE_LINK_LABEL} {link_url}", "type": 1, "biz_id": ""})
    if comment_text:
        # 生成短哈希作为去重标记（评论文本+时间戳 → MD5前16位）
        hash_src = f"{comment_text}{time.time()}"
        short_hash = hashlib.md5(hash_src.encode()).hexdigest()[:16]
        contents.append({"raw_text": f"\n置顶评论：{comment_text}\n随机字符：{short_hash}", "type": 1, "biz_id": ""})

    body = {
        "dyn_req": {
            "scene": 2,
            "content": {
                "contents": contents,
            },
            "pics": [img],
            "topic": {
                "id": topic_id,
                "name": topic_name,
                "from_source": "dyn.web.list",
                "from_topic_id": 0,
            },
            "option": {"up_choose_comment": 0, "close_comment": 0},
            "meta": {"app_meta": {"from": "create.dynamic.web", "mobi_app": "web"}},
            "upload_id": upload_id,
        }
    }

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Content-Type": "application/json",
            "Referer": "https://t.bilibili.com/",
            "Origin": "https://t.bilibili.com",
            "Cookie": cookie_str,
        }

        async with aiohttp.ClientSession() as session:
            url = f"{BILI_PUBLISH_URL}?csrf={csrf}&platform=web"
            async with session.post(url, json=body, headers=headers, timeout=30) as resp:
                result = await resp.json()
                if result.get("code") == 0:
                    dyn_id = result.get("data", {}).get("dyn_id_str", "?")
                    logger.info(f"✅ 动态发布成功: https://t.bilibili.com/{dyn_id}")
                    return True
                else:
                    logger.error(f"❌ 动态发布失败: code={result.get('code')} msg={result.get('message')}")
                    return False
    except Exception as e:
        logger.error(f"❌ 动态发布异常: {e}")
        return False


# ------------------------------------------------------------------
# 直播间标题更新 → B站动态发布
# ------------------------------------------------------------------
async def _download_cover_image(url: str) -> Optional[str]:
    """下载直播间封面图到临时文件，返回临时文件路径，失败返回None"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=30) as resp:
                if resp.status != 200:
                    logger.warning(f"⚠️ 封面下载HTTP {resp.status}: {url[:80]}...")
                    return None
                data = await resp.read()
                if not data or len(data) < 100:
                    logger.warning(f"⚠️ 封面数据过小: {len(data) if data else 0} bytes")
                    return None
                # 根据URL推断文件后缀
                suffix = ".jpg" if ("jpg" in url.lower() or "jpeg" in url.lower()) else ".png"
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix, prefix="btce_cover_")
                tmp.write(data)
                tmp.close()
                logger.info(f"📥 封面下载成功: {tmp.name} ({len(data)} bytes)")
                return tmp.name
    except Exception as e:
        logger.error(f"❌ 封面下载异常: {e}")
        return None


async def publish_live_update(cover_url: str, title: str, room_id: int,
                               status_tags: str, cookies: list,
                               topic_id: int, topic_name: str,
                               up_name: str) -> bool:
    """
    发布直播间标题更新到B站动态（封面图+标题+时间+链接+状态标签）。
    封面下载/上传失败不阻塞，降级为纯文本动态。
    返回True表示发布成功，False表示失败。
    """
    csrf = _csrf_from_cookies(cookies)
    if not csrf:
        logger.error("❌ live_publish: 未找到bili_jct cookie，无法发布")
        return False
    cookie_str = _cookie_header(cookies)

    # 1) 下载封面图并上传到B站图床
    img = None
    tmp_path = None
    if cover_url:
        tmp_path = await _download_cover_image(cover_url)
        if tmp_path:
            img = await upload_image(tmp_path, cookies)
            if not img:
                logger.warning("⚠️ live_publish: 封面上传失败，改为纯文本发布")
        else:
            logger.warning("⚠️ live_publish: 封面下载失败，改为纯文本发布")

    # 2) 组装动态内容
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    link_url = f"https://live.bilibili.com/{room_id}"

    contents = [
        {"raw_text": f"【{up_name}】直播间标题更新！", "type": 1, "biz_id": ""},
        {"raw_text": f"\n✏️ 新标题：{title}", "type": 1, "biz_id": ""},
        {"raw_text": f"\n⏰ 更新时间：{current_time}", "type": 1, "biz_id": ""},
        {"raw_text": f"\n🔗 {link_url}", "type": 1, "biz_id": ""},
    ]
    # 封面图上传成功才附「当前封面」提示；降级纯文本时不出现空标签
    if img:
        contents.append({"raw_text": f"\n 当前封面：", "type": 1, "biz_id": ""})
    if status_tags:
        contents.append({"raw_text": f"\n⚠️ 房间状态：{status_tags}", "type": 1, "biz_id": ""})

    # 3) 发布动态
    mid = _mid_from_cookies(cookies)
    upload_id = f"{mid}_{int(time.time())}_{random.randint(1000, 9999)}"

    body = {
        "dyn_req": {
            "scene": 2,
            "content": {"contents": contents},
            "pics": [img] if img else [],
            "topic": {
                "id": topic_id,
                "name": topic_name,
                "from_source": "dyn.web.list",
                "from_topic_id": 0,
            },
            "option": {"up_choose_comment": 0, "close_comment": 0},
            "meta": {"app_meta": {"from": "create.dynamic.web", "mobi_app": "web"}},
            "upload_id": upload_id,
        }
    }

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Content-Type": "application/json",
            "Referer": "https://t.bilibili.com/",
            "Origin": "https://t.bilibili.com",
            "Cookie": cookie_str,
        }

        async with aiohttp.ClientSession() as session:
            url = f"{BILI_PUBLISH_URL}?csrf={csrf}&platform=web"
            async with session.post(url, json=body, headers=headers, timeout=30) as resp:
                result = await resp.json()
                if result.get("code") == 0:
                    dyn_id = result.get("data", {}).get("dyn_id_str", "?")
                    logger.info(f"✅ 直播动态发布成功: https://t.bilibili.com/{dyn_id}")
                    return True
                else:
                    logger.error(f"❌ 直播动态发布失败: code={result.get('code')} msg={result.get('message')}")
                    return False
    except Exception as e:
        logger.error(f"❌ 直播动态发布异常: {e}")
        return False
    finally:
        # 清理临时封面文件
        if tmp_path:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                pass
