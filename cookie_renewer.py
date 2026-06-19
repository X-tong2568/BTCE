# cookie_renewer.py
"""
Cookie 远程更新模块（v4.7）。
通过 B站 API 生成登录二维码 → 邮件发送 → 管理员扫码 → 自动保存凭证。
全程 HTTP API，无需浏览器，适合无头服务器。
"""

import json
import time
import io
import re
import urllib.request
import urllib.parse
import http.cookiejar
from pathlib import Path
from typing import Optional, Tuple, List
from datetime import datetime

import qrcode

from logger_config import logger

# ===== B站登录 API =====
BILIBILI_QR_GENERATE = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
BILIBILI_QR_POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"

# ===== B站扫码状态码 =====
QR_NOT_SCANNED = 86101       # 未扫码
QR_SCANNED_UNCONFIRMED = 86090  # 已扫码未确认
QR_EXPIRED = 86038           # 已过期
QR_SUCCESS = 0               # 登录成功

# ===== 必需 cookie 字段 =====
REQUIRED_COOKIES = ["SESSDATA", "bili_jct", "DedeUserID", "sid"]

# ===== 轮询配置 =====
MAX_WAIT_SECONDS = 180       # 最长等3分钟
POLL_INTERVAL = 3            # 每3秒查一次
MAX_RETRIES = 2              # 二维码过期后最多重试2次

# ===== User-Agent =====
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


def _http_get_with_cookies(url: str, timeout: int = 10) -> Tuple[dict, List[dict]]:
    """
    发送 GET 请求并自动捕获 Set-Cookie。
    返回 (json_data, cookies_list)
    """
    cookie_jar = http.cookiejar.CookieJar()
    handler = urllib.request.HTTPCookieProcessor(cookie_jar)
    opener = urllib.request.build_opener(handler)

    req = urllib.request.Request(url)
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Referer", "https://passport.bilibili.com/login")

    resp = opener.open(req, timeout=timeout)
    data = json.loads(resp.read().decode())

    cookies = []
    for cookie in cookie_jar:
        cookies.append({
            "name": cookie.name,
            "value": cookie.value,
            "domain": cookie.domain or ".bilibili.com",
            "path": cookie.path or "/",
            "expires": cookie.expires,
            "secure": bool(cookie.secure),
        })

    return data, cookies


def generate_qrcode() -> Optional[Tuple[str, str]]:
    """
    调用 B站 API 生成登录二维码。
    返回 (qrcode_key, scan_url) 或 None
    """
    try:
        data, _ = _http_get_with_cookies(BILIBILI_QR_GENERATE)
        if data.get("code") != 0:
            logger.error(f"生成二维码失败: {data}")
            return None
        key = data["data"]["qrcode_key"]
        url = data["data"]["url"]
        logger.info(f"✅ 二维码已生成: key={key[:16]}...")
        return key, url
    except Exception as e:
        logger.error(f"生成二维码异常: {e}")
        return None


def make_qrcode_image(scan_url: str) -> bytes:
    """用 scan_url 生成二维码 PNG 图片，返回 bytes"""
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(scan_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    result = buf.read()
    logger.info(f"📱 二维码图片已生成: {len(result)} bytes")
    return result


def poll_login(qrcode_key: str) -> Tuple[str, Optional[List[dict]]]:
    """
    轮询扫码状态（同步阻塞，在 executor 中运行）。
    返回 (status, cookies_list)
    status: "success" | "expired" | "timeout"
    cookies_list: 成功时返回完整cookie列表，否则None
    """
    start = time.time()
    last_code = None

    logger.info(f"⏳ 开始轮询扫码状态，最长等待 {MAX_WAIT_SECONDS}s...")

    while time.time() - start < MAX_WAIT_SECONDS:
        try:
            data, cookies = _http_get_with_cookies(
                f"{BILIBILI_QR_POLL}?qrcode_key={qrcode_key}"
            )
        except Exception as e:
            logger.warning(f"轮询请求异常: {e}，3秒后重试...")
            time.sleep(POLL_INTERVAL)
            continue

        qr_code = data.get("data", {}).get("code", -1)
        qr_msg = data.get("data", {}).get("message", "")

        # 状态变化时打印
        if qr_code != last_code:
            status_label = {
                QR_NOT_SCANNED: "等待扫码",
                QR_SCANNED_UNCONFIRMED: "已扫码，请在手机上确认登录",
                QR_EXPIRED: "二维码已过期",
            }
            label = status_label.get(qr_code, f"未知状态({qr_code})")
            logger.info(f"📱 {label} | {qr_msg}")
            last_code = qr_code

        # 登录成功
        if qr_code == QR_SUCCESS:
            logger.info(f"🎉 扫码登录成功，获取到 {len(cookies)} 个cookie")
            if cookies:
                return "success", cookies
            return "timeout", None

        # 二维码过期
        if qr_code == QR_EXPIRED:
            return "expired", None

        time.sleep(POLL_INTERVAL)

    logger.warning("⏰ 等待扫码超时")
    return "timeout", None


def save_cookies_to_file(cookies: List[dict]) -> bool:
    """
    保存 cookies 到 cookies.json，兼容 Playwright 格式。
    保留旧文件中的非登录 cookie（如 buvid3、bili_ticket）。
    """
    try:
        import config
        cookie_file = Path(config.COOKIE_FILE)

        # 尝试从旧文件保留设备指纹 cookie
        old_cookies = {}
        if cookie_file.exists():
            try:
                with open(cookie_file, "r", encoding="utf-8") as f:
                    old_list = json.load(f)
                for c in old_list:
                    name = c.get("name", "")
                    # 保留与登录无关的设备指纹 cookie
                    if name in ("buvid3", "buvid4", "bili_ticket", "b_nut", "_uuid"):
                        old_cookies[name] = c
            except Exception:
                pass

        # 构建新的 cookie 列表
        new_cookie_names = {c["name"] for c in cookies}
        merged = []

        # 先放登录 cookie
        for c in cookies:
            merged.append({
                "name": c["name"],
                "value": c["value"],
                "domain": c.get("domain", ".bilibili.com"),
                "path": c.get("path", "/"),
                "expires": c.get("expires", int(time.time()) + 30 * 24 * 3600),
                "httpOnly": c["name"] == "SESSDATA",
                "secure": True,
                "sameSite": "None" if c["name"] == "SESSDATA" else "Lax",
            })

        # 补充旧 cookie 中没有被覆盖的
        for name, c in old_cookies.items():
            if name not in new_cookie_names:
                merged.append(c)

        # 写入文件
        cookie_file.parent.mkdir(parents=True, exist_ok=True)
        with open(cookie_file, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)

        logger.info(f"✅ Cookies 已保存到 {cookie_file}（{len(merged)}个字段）")
        return True
    except Exception as e:
        logger.error(f"❌ 保存 cookies 失败: {e}")
        return False


def send_qrcode_email(qrcode_img_bytes: bytes) -> bool:
    """
    发送内嵌二维码的管理邮件（同步阻塞，在 executor 中运行）。
    """
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.image import MIMEImage
    from email.mime.text import MIMEText
    from email.header import Header
    from config_email import SMTP_SERVER, SMTP_PORT, EMAIL_USER, EMAIL_PASSWORD

    # 收件人：仅日报邮箱（STATUS_MONITOR_EMAILS），不发推送收件人
    recipients = []
    try:
        from config_email import STATUS_MONITOR_EMAILS
        recipients = STATUS_MONITOR_EMAILS if isinstance(STATUS_MONITOR_EMAILS, list) else [STATUS_MONITOR_EMAILS]
        recipients = [r for r in recipients if r]  # 过滤空字符串
    except Exception:
        pass

    if not recipients:
        logger.error("❌ 未配置收件邮箱")
        return False

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 构建 multipart/related 邮件（内嵌图片）
    msg = MIMEMultipart("related")
    msg["Subject"] = Header("🔐 BTCE - B站登录二维码", "utf-8")
    msg["From"] = EMAIL_USER
    msg["To"] = ", ".join(recipients)

    html = f"""\
<html>
<body style="font-family: 'Microsoft YaHei', Arial, sans-serif; max-width: 500px; margin: 0 auto; background: #fff; padding: 20px;">
    <h2 style="color: #fb7299; margin-bottom: 10px;">🔐 B站登录二维码</h2>
    <p style="color: #333; font-size: 14px;">请在 <b>3分钟内</b> 用 <b>B站APP</b> 扫码登录，过期需重新发送指令。</p>
    <div style="text-align: center; padding: 15px; background: #f9f9f9; border-radius: 12px;">
        <img src="cid:qrcode" alt="B站登录二维码" style="max-width: 280px; width: 100%; border-radius: 8px;">
    </div>
    <p style="color: #999; font-size: 12px; margin-top: 15px;">⏰ 生成时间: {now}</p>
    <hr style="border: none; border-top: 1px solid #eee; margin: 20px 0;">
    <p style="color: #999; font-size: 11px;">此邮件由 BTCE 系统自动发送。</p>
</body>
</html>
"""
    msg.attach(MIMEText(html, "html", "utf-8"))

    img = MIMEImage(qrcode_img_bytes, _subtype="png")
    img.add_header("Content-ID", "<qrcode>")
    img.add_header("Content-Disposition", "inline", filename="bilibili_qrcode.png")
    msg.attach(img)

    try:
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, timeout=15) as server:
            server.login(EMAIL_USER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_USER, recipients, msg.as_string())
        logger.info(f"✅ 二维码已发送到邮箱: {recipients}")
        return True
    except Exception as e:
        logger.error(f"❌ 发送邮件失败: {e}")
        return False


async def run_cookie_renewal(on_status_update=None) -> str:
    """
    执行完整的 cookie 更新流程（异步入口）。

    Args:
        on_status_update: 可选回调，接收状态字符串用于推送到QQ群
                          async def on_status_update(message: str)

    Returns:
        "success" | "expired" | "timeout" | "error"
    """
    import asyncio

    retries = 0
    while retries <= MAX_RETRIES:
        # ① 生成二维码
        result = generate_qrcode()
        if not result:
            if on_status_update:
                await on_status_update("❌ 生成二维码失败，请检查网络")
            return "error"

        qrcode_key, scan_url = result
        logger.info(f"📱 二维码第{retries + 1}次生成: key={qrcode_key[:16]}...")

        # ② 生成二维码图片
        img_bytes = make_qrcode_image(scan_url)

        # ③ 发送邮件
        loop = asyncio.get_event_loop()
        email_ok = await loop.run_in_executor(
            None, send_qrcode_email, img_bytes
        )
        if not email_ok:
            if on_status_update:
                await on_status_update("❌ 发送邮件失败，请检查邮箱配置")
            return "error"

        # ④ 轮询扫码
        if on_status_update:
            await on_status_update(
                f"📧 二维码已发送到管理邮箱\n⏳ 请在3分钟内用B站APP扫码"
            )

        status, cookies = await loop.run_in_executor(
            None, poll_login, qrcode_key
        )

        if status == "success" and cookies:
            # ⑤ 保存 cookies
            save_ok = save_cookies_to_file(cookies)
            if save_ok:
                if on_status_update:
                    await on_status_update("✅ B站凭证已更新，已写入 cookies.json")
                return "success"
            else:
                if on_status_update:
                    await on_status_update("⚠️ Cookie获取成功但写入文件失败，请检查日志")
                return "error"

        elif status == "expired":
            retries += 1
            if retries <= MAX_RETRIES:
                logger.info(f"🔄 二维码已过期，第{retries}次重试...")
                if on_status_update:
                    await on_status_update(
                        f"⏰ 二维码已过期，正在重新生成（{retries}/{MAX_RETRIES}）..."
                    )
                continue
            else:
                if on_status_update:
                    await on_status_update("❌ 二维码多次过期，请重新发送指令")
                return "expired"

        elif status == "timeout":
            if on_status_update:
                await on_status_update("⏰ 等待超时（3分钟），请重新发送指令")
            return "timeout"

    return "error"
