# monitor.py
import asyncio
import json
import time
import os
import re
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

from config import (
    CHECK_INTERVAL, COOKIE_FILE, HISTORY_FILE,
    MAIL_SAVE_DIR, UP_NAME, PINNED_DYNAMIC_ID, BROWSER_CONFIG, BROWSER_RESTART_INTERVAL,
    P1_TOTAL_FAILURE_THRESHOLD, P2_SUCCESS_RATE_THRESHOLD
)
from render_comment import CommentRenderer
from email_utils import send_email
from config_email import TO_EMAILS, EMAIL_USER
from health_check import HealthChecker
from logger_config import logger
from retry_decorator import BROWSER_RETRY_CONFIG, async_retry
from performance_monitor import performance_monitor
from qq_utils import send_qq_message
from config_qq import QQ_GROUP_IDS
from dynamic import MONITOR_LIST
from bili_api import BiliAPI


class Monitor:
    """动态置顶评论监控类"""

    def __init__(self):
        self.check_interval = CHECK_INTERVAL
        self.cookie_file = COOKIE_FILE
        self.history_file = HISTORY_FILE
        self.mail_save_dir = MAIL_SAVE_DIR
        self.pinned_dynamic_id = PINNED_DYNAMIC_ID
        self.status_monitor = None
        self.comment_renderer = CommentRenderer()
        self.health_checker = HealthChecker()
        self.loop_count = 0
        self.is_running = True

        if os.path.exists(self.history_file):
            raw = json.loads(Path(self.history_file).read_text(encoding="utf-8"))
            self.history_data = raw if self._is_new_format(raw) else self._migrate_old_format(raw)
        else:
            self.history_data = {"pinned_comments": {}, "seen_dynamics": [], "last_pinned_dynamic_id": None}

        self.playwright = None
        self.browser = None
        self.context = None

    def _is_new_format(self, data):
        return "pinned_comments" in data and "seen_dynamics" in data

    def _migrate_old_format(self, old_data):
        new_data = {"pinned_comments": {}, "seen_dynamics": [], "last_pinned_dynamic_id": None}
        for key, value in old_data.items():
            if isinstance(value, dict) and "html" in value:
                new_data["pinned_comments"][f"legacy_{key}"] = {
                    "html": value.get("html", ""), "images": value.get("images", []),
                    "last_updated": time.strftime("%Y-%m-%d %H:%M:%S")
                }
        return new_data

    @async_retry(BROWSER_RETRY_CONFIG)
    async def initialize_browser(self):
        logger.info("🔄 初始化浏览器...")
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(**BROWSER_CONFIG)
        self.context = await self.browser.new_context()
        if not self.cookie_file.exists():
            raise FileNotFoundError("Cookie 文件不存在")
        cookies = json.loads(self.cookie_file.read_text(encoding="utf-8"))
        await self.context.add_cookies(cookies)
        logger.info("✅ 浏览器初始化完成")

    async def safe_close_browser(self):
        try:
            if self.context: await self.context.close()
            if self.browser: await self.browser.close()
            if self.playwright: await self.playwright.stop()
            self.context = self.browser = self.playwright = None
        except Exception as e:
            logger.error(f"❌ 关闭浏览器失败: {e}")

    async def restart_browser_if_needed(self):
        if self.loop_count % BROWSER_RESTART_INTERVAL == 0:
            logger.info("♻️ 浏览器重启")
            await self.safe_close_browser()
            await asyncio.sleep(2)
            await self.initialize_browser()
            return True
        return False

    def _clean_html_emojis(self, html_text):
        if not html_text: return ""
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html_text, 'html.parser')
            for img in soup.find_all('img'):
                alt = img.get('alt', '')
                img.replace_with(alt if alt else "[表情]")
            return str(soup)
        except:
            return re.sub(r'<img[^>]*>', '', html_text)

    # ------------------------------------------------------------------
    # API 获取动态列表
    # ------------------------------------------------------------------
    async def get_api_dynamics(self, uid):
        bili = BiliAPI(self.cookie_file)
        result = await bili.get_dynamics(uid)
        await bili.close()
        if result:
            ids = [d["dynamic_id"][-8:] for d in result[:5]]
            logger.info(f"📡 API: {len(result)}条动态 ID尾号={ids}...")
        return result

    # ------------------------------------------------------------------
    # 新动态批量通知
    # ------------------------------------------------------------------
    async def _handle_new_dynamics_batch(self, new_dynamics, up_name):
        if not new_dynamics: return
        try:
            current_time = time.strftime("%Y-%m-%d %H:%M:%S")
            logger.info(f"🆕 批量通知 {len(new_dynamics)} 条")

            # 最多截前3张图，避免截图过多撑爆浏览器
            for idx, dyn in enumerate(new_dynamics):
                dyn_id = dyn["dynamic_id"]
                if idx >= 3: break
                try:
                    dyn_page = await self.context.new_page()
                    await dyn_page.set_viewport_size({"width": 1080, "height": 1920})
                    await dyn_page.goto(f"https://t.bilibili.com/{dyn_id}", wait_until="domcontentloaded", timeout=15000)
                    await asyncio.sleep(3)
                    # 强制滚回顶部，避免B站页面自动滚动导致截图偏移
                    await dyn_page.evaluate("window.scrollTo(0, 0)")
                    await asyncio.sleep(0.5)
                    # 隐藏B站固定导航栏，防止元素截图被遮挡
                    await dyn_page.evaluate("""
                        () => {
                            const selectors = [
                                '.bili-header', '.bili-header-m', '#biliMainHeader',
                                '.international-header', '.primary-channel', '.bili-header__bar',
                                '.bili-header__channel', '.top-header', '.bili-pendant',
                                '.bili-header__banner', '.bili-header__notice'
                            ];
                            selectors.forEach(sel => {
                                document.querySelectorAll(sel).forEach(el => {
                                    el.style.display = 'none';
                                });
                            });
                            // 隐藏所有 position:fixed 且 z-index>100 的顶层元素（通常为导航/浮层）
                            document.querySelectorAll('*').forEach(el => {
                                const style = window.getComputedStyle(el);
                                if (style.position === 'fixed' && parseInt(style.zIndex) > 100) {
                                    el.style.display = 'none';
                                }
                            });
                        }
                    """)
                    await asyncio.sleep(0.3)
                    card = dyn_page.locator('.bili-dyn-item, [class*="dyn-card"]').first
                    if await card.count() > 0:
                        await card.scroll_into_view_if_needed()
                        await asyncio.sleep(0.3)
                        ts = time.strftime("%Y%m%d%H%M%S")
                        path = str(Path(self.mail_save_dir) / f"new_{dyn_id}_{ts}.png")
                        await card.screenshot(path=path)
                        dyn["screenshot_path"] = path
                        logger.info(f"📸 截图: {path}")
                    await dyn_page.close()
                except Exception as e:
                    logger.warning(f"⚠️ 截图失败: {e}")

            email_body = self.comment_renderer.render_new_dynamics_batch_email(up_name, new_dynamics, current_time)
            ts = time.strftime("%Y%m%d%H%M%S")
            fpath = os.path.join(self.mail_save_dir, f"new_batch_{up_name}-{ts}.html")
            Path(self.mail_save_dir).mkdir(parents=True, exist_ok=True)
            with open(fpath, "w", encoding="utf-8") as f: f.write(email_body)
            asyncio.create_task(asyncio.to_thread(send_email, subject=f"【{up_name}】发布了 {len(new_dynamics)} 条新动态", content=email_body))
            logger.info("📧 新动态邮件已提交")

            # QQ合并发送（带截图的最多3张）
            if len(new_dynamics) == 1:
                dyn = new_dynamics[0]
                sp = dyn.get("screenshot_path", "")
                parts = [f"【{up_name}】发布了新动态~"]
                if sp: parts.append(f"[CQ:image,file=file:///{sp.replace(chr(92), '/')}]")
                parts.append(f"🔗 https://t.bilibili.com/{dyn['dynamic_id']}")
                parts.append(f"📅 {current_time}")
                await send_qq_message('\n'.join(parts))
            else:
                lines = [f"【{up_name}】发布了 {len(new_dynamics)} 条新动态~"]
                for d in new_dynamics[:5]:
                    lines.append(f"🔗 https://t.bilibili.com/{d['dynamic_id']}")
                lines.append(f"📅 {current_time}")
                await send_qq_message('\n'.join(lines))

            if self.status_monitor: self.status_monitor.record_change()
        except Exception as e:
            logger.error(f"❌ 批量通知失败: {e}")

    # ------------------------------------------------------------------
    # 置顶评论检测
    # ------------------------------------------------------------------
    async def check_dynamic_changes(self, dynamic_id, is_new_pinned=False):
        try:
            if not self.context: return False
            page = await self.context.new_page()
            try:
                current_html, current_images = await asyncio.wait_for(
                    self.comment_renderer.get_pinned_comment(page, dynamic_id), timeout=20)
            except (asyncio.TimeoutError, PlaywrightTimeoutError):
                logger.error(f"⏰ 超时 {dynamic_id}")
                await page.close(); return False
            if not current_html or "未找到置顶评论" in current_html:
                logger.warning(f"⚠️ 无置顶评论 {dynamic_id}")
                await page.close(); return False

            pc = self.history_data.get("pinned_comments", {})
            last_html = ""; last_images = []
            if is_new_pinned and self.history_data.get("last_pinned_dynamic_id"):
                old = pc.get(self.history_data["last_pinned_dynamic_id"], {})
                last_html = old.get("html", ""); last_images = old.get("images", [])
                logger.info(f"🔔 置顶更换: {self.history_data['last_pinned_dynamic_id']} → {dynamic_id}")
            else:
                last = pc.get(dynamic_id, {})
                last_html = last.get("html", ""); last_images = last.get("images", [])

            cur_text = self.comment_renderer.extract_text_from_html(self._clean_html_emojis(current_html))
            last_text = self.comment_renderer.extract_text_from_html(self._clean_html_emojis(last_html))
            logger.info(f"📝 当前: {cur_text} | 上次: {last_text or '无'}")
            should = (is_new_pinned and self.history_data.get("last_pinned_dynamic_id")) or (not last_text or cur_text != last_text)
            await page.close()

            if should:
                await self._send_notification(dynamic_id, current_html, current_images, last_html, last_images)
                if self.status_monitor: self.status_monitor.record_change()

            pc[dynamic_id] = {"html": current_html, "images": current_images, "last_updated": time.strftime("%Y-%m-%d %H:%M:%S")}
            if len(pc) > 10:
                for k in sorted(pc.keys())[:len(pc)-10]: del pc[k]
            self.history_data["pinned_comments"] = pc
            self.history_data["last_pinned_dynamic_id"] = dynamic_id
            self.health_checker.increment_success()
            return True
        except Exception as e:
            logger.error(f"❌ 检测评论异常: {e}")
            self.health_checker.increment_failure()
            return False

    async def _send_notification(self, dynamic_id, cur_html, cur_img, last_html, last_img):
        try:
            ct = time.strftime("%Y-%m-%d %H:%M:%S")
            body = self.comment_renderer.render_email_content(dynamic_id, cur_html, cur_img, last_html, last_img, ct)
            ts = time.strftime("%Y%m%d%H%M%S")
            fp = os.path.join(self.mail_save_dir, f"{UP_NAME}-{ts}.html")
            Path(self.mail_save_dir).mkdir(parents=True, exist_ok=True)
            with open(fp, "w", encoding="utf-8") as f: f.write(body)
            asyncio.create_task(asyncio.to_thread(send_email, subject=f"【{UP_NAME}】瞳瞳空间更新啦", content=body))
            logger.info("📧 置顶评论邮件已提交")
            qq = self.comment_renderer.generate_qq_message(UP_NAME, dynamic_id, cur_html, ct, cur_img)
            await send_qq_message(qq, {"up_name": UP_NAME, "dynamic_id": dynamic_id, "current_html": cur_html, "current_time": ct, "current_images": cur_img})
        except Exception as e:
            logger.error(f"❌ 通知失败: {e}")

    def _save_history(self):
        try: Path(self.history_file).write_text(json.dumps(self.history_data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e: logger.error(f"❌ 保存历史失败: {e}")

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    async def run_monitoring_cycle(self):
        self.loop_count += 1
        logger.info(f"🔍 第 {self.loop_count} 轮")
        self.health_checker.last_health_check = time.time()
        await self.restart_browser_if_needed()
        await performance_monitor.record_memory_usage()
        start = time.time()
        success = False

        try:
            for up in MONITOR_LIST:
                uid, name = up["uid"], up["name"]

                # 1) 优先检测置顶评论（核心功能）
                pinned_id = self.pinned_dynamic_id
                is_new = (self.history_data.get("last_pinned_dynamic_id") and pinned_id != self.history_data.get("last_pinned_dynamic_id"))
                if await self.check_dynamic_changes(pinned_id, is_new_pinned=is_new):
                    success = True

                # 2) API 拿动态列表 → 检测新动态
                api_list = await self.get_api_dynamics(uid)
                if api_list:
                    seen = set(self.history_data.get("seen_dynamics", []))
                    is_cold = len(seen) == 0
                    new_dynamics = []
                    for d in api_list:
                        dyn_id = d["dynamic_id"]
                        if dyn_id not in seen:
                            seen.add(dyn_id)
                            if not is_cold:
                                new_dynamics.append({"dynamic_id": dyn_id, "url": f"https://t.bilibili.com/{dyn_id}"})
                    if new_dynamics:
                        logger.info(f"🆕 检测到 {len(new_dynamics)} 条新动态")
                        await self._handle_new_dynamics_batch(new_dynamics, name)
                    if is_cold and seen:
                        logger.info(f"🧊 冷启动：静默记录 {len(seen)} 条")
                    self.history_data["seen_dynamics"] = list(seen)[-500:]

            self._save_history()
        except Exception as e:
            logger.error(f"❌ 循环异常: {e}")

        duration = time.time() - start
        performance_monitor.record_cycle(cycle_number=self.loop_count, success=success, duration=duration)
        stats = self.health_checker.get_stats(total_loops=self.loop_count)
        logger.info(f"📊 本轮完成 - {stats}")
        if self.status_monitor:
            logger.info(f"📈 状态监控: {self.status_monitor.get_status_info()}")

    async def run(self):
        logger.info(f"=== {UP_NAME} 监控 UID={MONITOR_LIST[0]['uid']} 置顶ID={self.pinned_dynamic_id} 间隔={self.check_interval}s ===")
        try:
            await self.initialize_browser()
            perf_task = asyncio.create_task(performance_monitor.periodic_report(interval_minutes=60))
            while self.is_running:
                cs = time.time()
                try:
                    await self.run_monitoring_cycle()
                    wait = max(0, self.check_interval - (time.time() - cs))
                    logger.info(f"⏰ 下次: {time.strftime('%H:%M:%S', time.localtime(time.time() + wait))} (等{wait:.1f}s)")
                    await asyncio.sleep(wait)
                except KeyboardInterrupt: break
                except Exception as e:
                    logger.error(f"❌ 循环错误: {e}")
                    performance_monitor.record_cycle(cycle_number=self.loop_count, success=False, duration=0)
                    await asyncio.sleep(5)
        finally:
            self.is_running = False
            if 'perf_task' in locals(): perf_task.cancel()
            await self.safe_close_browser()


if __name__ == "__main__":
    monitor = Monitor()
    asyncio.run(monitor.run())
