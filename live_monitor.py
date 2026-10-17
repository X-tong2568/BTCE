# live_monitor.py
import aiohttp
import time
import json
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any, Tuple
from logger_config import logger
from self_monitor import live_failure_counter
from retry_decorator import NETWORK_RETRY_CONFIG, async_retry
from config import LIVE_API_TIMEOUT, LIVE_ROOM_ID, COOKIE_FILE, UP_NAME
from qq_message_generator import qq_message_generator

# 状态持久化文件，重启后能对比上次记录
LIVE_STATE_FILE = Path(__file__).parent / "live_state.json"
'''
| 场景     | status_changed | change_type  | 是否发通知 |
| ------ | -------------- | ------------ | ----- |
| 首次启动   | ❌              | initial      | ❌     |
| 无变化轮询  | ❌              | no_change    | ❌     |
| 标题变化   | ✅              | title_change | ✅     |
| 开播     | ✅              | live_start   | ✅     |
| 下播     | ✅              | live_end     | ✅     |
| API 抖动 | ❌              | –            | ❌     |
| 网络失败   | ❌              | –            | ❌     |
'''
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

class LiveMonitor:
    """B站直播间状态监控器（展示封面，但不监测封面变化）"""

    def __init__(self):
        self.logger = logger.getChild("live_monitor")
        self.last_live_status: Optional[Dict[str, Any]] = None
        self.last_check_time: Optional[float] = None
        self.session: Optional[aiohttp.ClientSession] = None
        self.cookies = self.load_cookies()
        self._load_state()  # 从磁盘恢复上次检查状态，重启后也能对比

    # ------------------------------------------------------------------
    # 状态持久化（磁盘 ↔ 内存）
    # ------------------------------------------------------------------
    def _load_state(self):
        """从 live_state.json 恢复上次检查状态，重启后不丢失"""
        try:
            if not LIVE_STATE_FILE.exists():
                self.logger.info("📄 无历史状态文件，首次启动不通知")
                return
            data = json.loads(LIVE_STATE_FILE.read_text(encoding="utf-8"))
            self.last_live_status = data
            self.logger.info(
                f"📄 已恢复上次直播状态: live_status={data.get('live_status')}, "
                f"title={data.get('title', '')[:30]}"
            )
        except Exception as e:
            self.logger.warning(f"⚠️ 加载直播状态文件失败: {e}")

    def _save_state(self):
        """将当前直播状态写入 live_state.json，供下次重启恢复"""
        try:
            LIVE_STATE_FILE.write_text(
                json.dumps(self.last_live_status, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            self.logger.warning(f"⚠️ 保存直播状态文件失败: {e}")

    # ------------------------------------------------------------------
    # Cookie & Session
    # ------------------------------------------------------------------
    def load_cookies(self) -> Dict[str, str]:
        try:
            if not COOKIE_FILE.exists():
                return {}
            with open(COOKIE_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, list):
                return {c["name"]: c["value"] for c in raw if "name" in c}
            return raw if isinstance(raw, dict) else {}
        except Exception as e:
            self.logger.error(f"cookies 加载失败: {e}")
            return {}

    async def init_session(self):
        if self.session and not self.session.closed:
            return
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=LIVE_API_TIMEOUT),
            headers={
                "User-Agent": USER_AGENT,
                "Referer": f"https://live.bilibili.com/{LIVE_ROOM_ID}",
                "Accept": "application/json",
            },
        )
        if self.cookies:
            self.session.cookie_jar.update_cookies(self.cookies)

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------
    @async_retry(NETWORK_RETRY_CONFIG)
    async def fetch_live_status(self, room_id: int) -> Optional[Dict[str, Any]]:
        await self.init_session()

        urls = [
            f"https://api.live.bilibili.com/room/v1/Room/get_info?room_id={room_id}",
            f"https://api.live.bilibili.com/xlive/web-room/v1/index/getInfoByRoom?room_id={room_id}",
        ]

        result = None
        for idx, url in enumerate(urls, 1):
            try:
                async with self.session.get(url) as resp:
                    if resp.status != 200:
                        continue
                    payload = await resp.json()
                    if payload.get("code") != 0:
                        continue

                    data = payload.get("data", {})
                    if idx == 2:
                        data = data.get("room_info", {})

                    result = {
                        "room_id": room_id,
                        "live_status": data.get("live_status", 0),
                        "title": data.get("title", ""),
                        "cover": data.get("user_cover") or data.get("cover", ""),
                        "anchor_name": UP_NAME,
                        "check_time": datetime.now().isoformat(),
                    }
                    break
            except Exception as e:
                self.logger.error(f"API {idx} 异常: {e}")

        if result is None:
            return None

        # 补充 room_init 返回的特殊状态字段（加密/锁房/隐藏/付费等），先给默认值
        result.setdefault("encrypted", False)
        result.setdefault("is_locked", False)
        result.setdefault("is_hidden", False)
        result.setdefault("special_type", 0)
        await self._enrich_room_init(result)
        return result

    async def _enrich_room_init(self, result: Dict[str, Any]):
        """从 room_init 接口补充 encrypted/is_locked/is_hidden/special_type"""
        try:
            url = f"https://api.live.bilibili.com/room/v1/Room/room_init?id={result['room_id']}"
            async with self.session.get(url) as resp:
                if resp.status != 200:
                    return
                payload = await resp.json()
                if payload.get("code") != 0:
                    return
                data = payload.get("data", {})
                result["encrypted"] = data.get("encrypted", False)
                result["is_locked"] = data.get("is_locked", False)
                result["is_hidden"] = data.get("is_hidden", False)
                result["special_type"] = data.get("special_type", 0)
        except Exception as e:
            self.logger.error(f"room_init 异常: {e}")
            # room_init 失败不影响主流程，字段留默认值

    # ------------------------------------------------------------------
    # Core Logic
    # ------------------------------------------------------------------
    async def check_live_status(self, room_id: int) -> Optional[Dict[str, Any]]:
        self.last_check_time = time.time()
        current = await self.fetch_live_status(room_id)

        if not current:
            live_failure_counter.record_failure("状态获取失败")
            return None

        live_failure_counter.record_success()

        changed, change_type, title_changed = self.detect_status_change(current)
        current["status_changed"] = changed
        current["change_type"] = change_type
        current["title_changed"] = title_changed

        self.last_live_status = current
        self._save_state()  # 持久化，重启后能对比变化
        return current

    def detect_status_change(self, current: Dict[str, Any]) -> Tuple[bool, str, bool]:
        """检测状态变化，返回 (是否有变化, 变化类型, 标题是否变化)"""
        if self.last_live_status is None:
            return False, "initial", False

        old = self.last_live_status

        # 标题变化单独记录：即使同一轮开播优先，B站渠道仍需知道标题变了
        title_changed = bool(
            old.get("title") != current.get("title") and current.get("title")
        )

        if old["live_status"] != current["live_status"]:
            change_type = "live_start" if current["live_status"] == 1 else "live_end"
            return True, change_type, title_changed

        if title_changed:
            return True, "title_change", True

        return False, "no_change", False

    # ------------------------------------------------------------------
    # 状态标签（加密/锁房/隐藏/付费等，纯展示不触发通知）
    # ------------------------------------------------------------------
    @staticmethod
    def _build_status_tags(live_info: Dict[str, Any]) -> Optional[str]:
        """根据 room_init 字段生成标签文本（委托至 qq_message_generator）"""
        return qq_message_generator.build_live_status_tags(live_info)

    # ------------------------------------------------------------------
    # Email (完整样式 + 封面)
    # ------------------------------------------------------------------
    def format_email_content(self, live_info: Dict[str, Any]) -> (str, str):
        ct = live_info["change_type"]
        title = live_info.get("title", "无标题")
        cover = live_info.get("cover")
        room_id = live_info["room_id"]
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        icon, text = {
            "live_start": ("🎉", "开播啦"),
            "live_end": ("💤", "下播了"),
            "title_change": ("✏️", "标题更新"),
        }.get(ct, ("📺", "状态更新"))

        subject = f"【{UP_NAME}直播监控】{text}"

        status_tags = self._build_status_tags(live_info)

        html = f"""
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<style>
body {{ background:#f5f5f5; font-family:Microsoft YaHei; padding:20px }}
.container {{ max-width:600px;margin:auto;background:#fff;border-radius:10px;overflow:hidden }}
.header {{ background:linear-gradient(135deg,#ff6699,#ff3366);color:#fff;padding:20px;text-align:center }}
.cover img {{ width:100%;display:block }}
.content {{ padding:24px }}
.status {{ text-align:center;font-size:20px;color:#ff3366;font-weight:bold }}
.info {{ background:#f9f9f9;padding:15px;border-radius:8px;margin-top:15px }}
.btn {{ display:inline-block;margin-top:20px;background:#ff3366;color:#fff;
       padding:10px 20px;border-radius:5px;text-decoration:none }}
.footer {{ text-align:center;font-size:12px;color:#999;padding:20px }}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h2>{icon} {UP_NAME} 直播提醒</h2>
  </div>

  {"<div class='cover'><img src='" + cover + "'></div>" if cover else ""}

  <div class="content">
    <div class="status">{text}</div>
    <div class="info">
      <p><b>标题：</b>{title}</p>
      <p><b>时间：</b>{current_time}</p>
      {"<p><b>⚠️ 特别状态：</b>" + status_tags + "</p>" if status_tags else ""}
    </div>
    <div style="text-align:center">
      <a class="btn" href="https://live.bilibili.com/{room_id}">
        进入直播间
      </a>
    </div>
  </div>

  <div class="footer">
    <p>此邮件由直播监控系统自动发送，请勿回复</p>
    <p>监控时间：{current_time}</p>
  </div>
</div>
</body>
</html>
"""
        return subject, html

    # ------------------------------------------------------------------
    # QQ 消息（带封面图片）
    # ------------------------------------------------------------------
    def generate_qq_message(self, live_info: Dict[str, Any]) -> str:
        """生成直播状态变更 QQ 消息（委托至 qq_message_generator）"""
        return qq_message_generator.generate_live_qq_message(live_info)

    # ------------------------------------------------------------------
    # 监控统计信息
    # ------------------------------------------------------------------
    def get_monitor_stats(self) -> Dict[str, Any]:
        return {
            "last_check_time": self.last_check_time,
            "last_live_status": self.last_live_status,
            "cookies_loaded": bool(self.cookies),
            "failure_stats": live_failure_counter.get_stats(),
        }


    async def close_session(self):
        """关闭 aiohttp session"""
        if self.session and not self.session.closed:
            await self.session.close()
            self.session = None


live_monitor = LiveMonitor()
