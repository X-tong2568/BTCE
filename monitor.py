# monitor.py
import asyncio
import json
import time
import os
import re
from pathlib import Path
from typing import Optional
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

from config import (
    CHECK_INTERVAL, COOKIE_FILE, HISTORY_FILE,
    MAIL_SAVE_DIR, UP_NAME, UP_UID, PINNED_DYNAMIC_ID, PINNED_CHECK_INTERVAL,
    BROWSER_CONFIG, BROWSER_RESTART_INTERVAL,
    P1_TOTAL_FAILURE_THRESHOLD, P2_SUCCESS_RATE_THRESHOLD,
    AUTO_PUBLISH_ENABLED, AUTO_PUBLISH_TOPIC_ID, AUTO_PUBLISH_TOPIC_NAME,
    QQ_MODE, EMAIL_MODE, BILI_MODE,
    DYNAMIC_SKIP_TYPES,
    QQ_BATCH_LINK_LABEL, QQ_BATCH_TIME_LABEL,
    EMAIL_PINNED_SUBJECT,
)
from render_comment import CommentRenderer
from email_utils import send_email
from config_email import TO_EMAILS, EMAIL_USER, STATUS_MONITOR_EMAILS
from health_check import HealthChecker
from logger_config import logger
from retry_decorator import BROWSER_RETRY_CONFIG, async_retry
from performance_monitor import performance_monitor
from qq_utils import send_qq_message
from config_qq import QQ_GROUP_IDS
from bili_api import BiliAPI
from pinned_dynamic_monitor import check_pinned_dynamic, set_mode_manual, set_mode_auto, get_mode
from color_config import ColorConfig
from pinned_card_renderer import generate_pinned_card
import auto_publish


def _update_config_pinned_id(new_id: str):
    """同步写入 config.py 的 PINNED_DYNAMIC_ID（正则替换，不破坏其他内容）"""
    try:
        config_path = Path(__file__).parent / "config.py"
        content = config_path.read_text(encoding="utf-8")
        new_content = re.sub(
            r'PINNED_DYNAMIC_ID\s*=\s*"[^"]*"',
            f'PINNED_DYNAMIC_ID = "{new_id}"',
            content
        )
        if new_content != content:
            config_path.write_text(new_content, encoding="utf-8")
            logger.info(f"📝 config.py PINNED_DYNAMIC_ID 已更新为 {new_id}")
    except Exception as e:
        logger.warning(f"⚠️ config.py PINNED_DYNAMIC_ID 更新失败: {e}")


class Monitor:
    """动态置顶评论监控类"""

    def __init__(self):
        self.check_interval = CHECK_INTERVAL
        self.cookie_file = COOKIE_FILE
        self.history_file = HISTORY_FILE
        self.mail_save_dir = MAIL_SAVE_DIR
        # 如果 config 中为空，尝试从 state 文件恢复（重启后兜底）
        if PINNED_DYNAMIC_ID:
            self.pinned_dynamic_id = PINNED_DYNAMIC_ID
        else:
            try:
                state_file = Path(__file__).parent / "pinned_dynamic_state.json"
                if state_file.exists():
                    state = json.loads(state_file.read_text(encoding="utf-8"))
                    self.pinned_dynamic_id = state.get("pinned_dynamic_id", "")
                    if self.pinned_dynamic_id:
                        logger.info(f"📎 从 state 文件恢复置顶ID: {self.pinned_dynamic_id}")
                        _update_config_pinned_id(self.pinned_dynamic_id)  # 同步写入config.py
            except Exception:
                self.pinned_dynamic_id = ""
        # 确保不是 None
        if not self.pinned_dynamic_id:
            self.pinned_dynamic_id = ""
        self.status_monitor = None
        self.comment_renderer = CommentRenderer()
        self.health_checker = HealthChecker()
        self.loop_count = 0
        self.last_pinned_check = 0  # 上次置顶动态更换检测的时间戳
        self.api_pinned_id = self.pinned_dynamic_id  # B站API实际置顶ID（手动模式下可能≠监控目标）
        self.is_running = True

        if os.path.exists(self.history_file):
            raw = json.loads(Path(self.history_file).read_text(encoding="utf-8"))
            self.history_data = raw if self._is_new_format(raw) else self._migrate_old_format(raw)
        else:
            self.history_data = {"pinned_comments": {}, "seen_dynamics": [], "last_pinned_dynamic_id": None}

        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None  # v4.8：持久化页面复用

    def update_pinned_dynamic_id(self, new_id: str):
        """运行时动态更换置顶动态ID（供QQ回调指令调用），同时切换为手动模式"""
        self.pinned_dynamic_id = new_id
        self.api_pinned_id = new_id  # 同步更新API置顶ID，状态消息"B站置顶"行能立即显示新值
        self.history_data["last_pinned_dynamic_id"] = None
        set_mode_manual(new_id)  # 切手动+记录新ID，API不会自动切回
        logger.info(f"🔄 置顶动态ID已更换为 {new_id} (已切换为手动模式)")

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
        """安全关闭浏览器，同时清理持久化页面"""
        try:
            if self.page and not self.page.is_closed():
                try: await self.page.close()
                except: pass
            self.page = None
            if self.context: await self.context.close()
            if self.browser: await self.browser.close()
            if self.playwright: await self.playwright.stop()
            self.context = self.browser = self.playwright = None
        except Exception as e:
            logger.error(f"❌ 关闭浏览器失败: {e}")

    async def restart_browser_if_needed(self):
        """按间隔重启浏览器，v4.8：先清理持久化页面"""
        if self.loop_count % BROWSER_RESTART_INTERVAL == 0:
            logger.info("♻️ 浏览器重启")
            if self.page and not self.page.is_closed():
                try: await self.page.close()
                except: pass
            self.page = None
            await self.safe_close_browser()
            await asyncio.sleep(2)
            await self.initialize_browser()
            return True
        return False

    async def restart_browser(self):
        """无条件重启浏览器，用于凭证更新后热加载新cookie（v4.7）"""
        logger.info("♻️ 强制重启浏览器（凭证更新触发）")
        await self.safe_close_browser()
        await asyncio.sleep(2)
        await self.initialize_browser()
        logger.info("✅ 浏览器已重启，新凭证已生效")

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
        """获取API动态列表，返回 (list[dict], bool) — 列表 + 是否成功"""
        bili = BiliAPI(self.cookie_file)
        result, ok = await bili.get_dynamics(uid)
        await bili.close()
        if ok and result:
            ids = [d["dynamic_id"][-8:] for d in result[:5]]
            logger.info(f"📡 API: {len(result)}条动态 ID尾号={ids}...")
        elif ok:
            logger.info(f"📡 API: 0条动态（无新发布）")
        return result, ok

    # ------------------------------------------------------------------
    # 新动态批量通知
    # ------------------------------------------------------------------
    async def _safe_evaluate(self, page, js, retries=3):
        """安全执行page.evaluate，自动重试页面跳转导致的执行上下文销毁"""
        for attempt in range(retries):
            try:
                return await page.evaluate(js)
            except Exception as e:
                if "Execution context" in str(e) and attempt < retries - 1:
                    logger.warning(f"⚠️ 页面跳转中，等待稳定后重试 ({attempt+1}/{retries})")
                    await asyncio.sleep(1.5)
                    try:
                        await page.wait_for_load_state("load", timeout=5000)
                    except Exception:
                        pass
                else:
                    raise

    async def _take_dynamic_screenshot(self, dynamic_id: str) -> Optional[str]:
        """截取动态页面截图（高DPI），返回文件路径，失败返回None"""
        try:
            shot_ctx = await self.browser.new_context(device_scale_factor=2)
            main_cookies = await self.context.cookies()
            if main_cookies:
                await shot_ctx.add_cookies(main_cookies)
            try:
                dyn_page = await shot_ctx.new_page()
                await dyn_page.set_viewport_size({"width": 1080, "height": 1920})
                await dyn_page.goto(f"https://t.bilibili.com/{dynamic_id}", wait_until="load", timeout=20000)
                try:
                    await dyn_page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                await asyncio.sleep(1)
                await self._safe_evaluate(dyn_page, "window.scrollTo(0, 0)")
                await self._safe_evaluate(dyn_page, """
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
                        document.querySelectorAll('*').forEach(el => {
                            const style = window.getComputedStyle(el);
                            if (style.position === 'fixed' && parseInt(style.zIndex) > 100) {
                                el.style.display = 'none';
                            }
                        });
                    }
                """)
                await asyncio.sleep(0.3)
                ts = time.strftime("%Y%m%d%H%M%S")
                path = str(Path(self.mail_save_dir) / f"dynamic_{dynamic_id}_{ts}.png")
                # 与普通动态通知一致：只截动态卡片本身，不用full_page整页截图（避免截到评论区）
                card = dyn_page.locator('.bili-dyn-item, [class*="dyn-card"]').first
                if await card.count() > 0:
                    await card.scroll_into_view_if_needed()
                    await asyncio.sleep(0.3)
                    await card.screenshot(path=path)
                    logger.info(f"📸 截图: {path}")
                    return path
                logger.warning(f"⚠️ 未找到动态卡片元素，截图跳过: {dynamic_id}")
                return None
            finally:
                await shot_ctx.close()
        except Exception as e:
            logger.warning(f"⚠️ 截图失败: {dynamic_id} {e}")
            return None

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
                    # 高DPI独立context截图，布局不变但分辨率翻倍
                    shot_ctx = await self.browser.new_context(device_scale_factor=2)
                    # 复制主context的cookies，避免B站因未登录跳转导致页面导航
                    main_cookies = await self.context.cookies()
                    if main_cookies:
                        await shot_ctx.add_cookies(main_cookies)
                    try:
                        dyn_page = await shot_ctx.new_page()
                        await dyn_page.set_viewport_size({"width": 1080, "height": 1920})
                        # 用load+networkidle等页面彻底稳定，避免B站二次跳转导致evaluate上下文销毁
                        await dyn_page.goto(f"https://t.bilibili.com/{dyn_id}", wait_until="load", timeout=20000)
                        try:
                            await dyn_page.wait_for_load_state("networkidle", timeout=8000)
                        except Exception:
                            pass  # networkidle超时不阻塞，可能B站长轮询导致
                        await asyncio.sleep(1)
                        # 强制滚回顶部+隐藏B站固定导航栏（包装为安全evaluate，处理残余跳转）
                        await self._safe_evaluate(dyn_page, "window.scrollTo(0, 0)")
                        await self._safe_evaluate(dyn_page, """
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
                    finally:
                        await shot_ctx.close()
                except Exception as e:
                    logger.warning(f"⚠️ 截图失败: {e}")

            email_body = self.comment_renderer.render_new_dynamics_batch_email(up_name, new_dynamics, current_time)
            asyncio.create_task(asyncio.to_thread(send_email, subject=f"【{up_name}】发布了 {len(new_dynamics)} 条新动态", content=email_body))
            logger.info("📧 新动态邮件已提交")

            # QQ推送（走统一的消息生成器）
            if len(new_dynamics) == 1:
                dyn = new_dynamics[0]
                msg = self.comment_renderer.generate_new_dynamic_qq_message(
                    up_name, dyn['dynamic_id'], f"https://t.bilibili.com/{dyn['dynamic_id']}",
                    current_time, dyn.get("content", ""), dyn.get("screenshot_path", ""))
                # 传 message_data:原始消息失败后自动补发纯文本降级消息(图片超时消息不丢失)
                await send_qq_message(msg, {
                    "up_name": up_name,
                    "dynamic_id": dyn['dynamic_id'],
                    "current_html": dyn.get("content", ""),
                    "current_time": current_time,
                    "current_images": dyn.get("images", []),
                    "degraded_type": "new_dynamic",
                })
            else:
                # 多条极少出现，简单拼装
                lines = [f"【{up_name}】发布了 {len(new_dynamics)} 条新动态~"]
                for d in new_dynamics[:5]:
                    lines.append(f"{QQ_BATCH_LINK_LABEL} https://t.bilibili.com/{d['dynamic_id']}")
                lines.append(f"{QQ_BATCH_TIME_LABEL} {current_time}")
                await send_qq_message('\n'.join(lines))

            if self.status_monitor: self.status_monitor.record_change()
        except Exception as e:
            logger.error(f"❌ 批量通知失败: {e}")

    # ------------------------------------------------------------------
    # 置顶评论检测
    # ------------------------------------------------------------------
    async def check_dynamic_changes(self, dynamic_id, is_new_pinned=False):
        """检测置顶评论变化，v4.8：页面复用，不每轮创建/销毁"""
        try:
            if not self.context: return False
            # v4.8：页面复用，仅在首次或页面已关闭时创建新页面
            if self.page is None or self.page.is_closed():
                self.page = await self.context.new_page()
            page = self.page
            try:
                current_html, current_images, rpid = await asyncio.wait_for(
                    self.comment_renderer.get_pinned_comment(page, dynamic_id), timeout=20)
            except (asyncio.TimeoutError, PlaywrightTimeoutError):
                logger.error(f"⏰ 超时 {dynamic_id}")
                return False
            if not current_html or "未找到置顶评论" in current_html:
                logger.warning(f"⚠️ 无置顶评论 {dynamic_id}")
                return False

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

            # v4.8：不复用时不关闭页面

            if should:
                # 通知分流：QQ+邮件默认text模式先推(不阻塞)，再截图推B站
                await self._send_notification(dynamic_id, current_html, current_images, last_html, last_images, rpid)
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

    async def _take_pinned_comment_screenshot(self, dynamic_id):
        """截取置顶评论区域截图（高DPI，供screenshot模式通道使用）"""
        try:
            shot_ctx = await self.browser.new_context(device_scale_factor=2)
            main_cookies = await self.context.cookies()
            if main_cookies:
                await shot_ctx.add_cookies(main_cookies)
            try:
                shot_page = await shot_ctx.new_page()
                await shot_page.set_viewport_size({"width": 1080, "height": 1920})
                await shot_page.goto(f"https://t.bilibili.com/{dynamic_id}", wait_until="load", timeout=20000)
                try:
                    await shot_page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                await asyncio.sleep(1)
                await self._safe_evaluate(shot_page, "window.scrollTo(0, 0)")
                await self._safe_evaluate(shot_page, """
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
                        document.querySelectorAll('*').forEach(el => {
                            const style = window.getComputedStyle(el);
                            if (style.position === 'fixed' && parseInt(style.zIndex) > 100) {
                                el.style.display = 'none';
                            }
                        });
                    }
                """)
                await asyncio.sleep(0.3)
                comment_el = shot_page.locator('#comment').first
                if await comment_el.count() > 0:
                    await comment_el.scroll_into_view_if_needed()
                    await asyncio.sleep(0.3)
                    ts = time.strftime("%Y%m%d%H%M%S")
                    path = str(Path(self.mail_save_dir) / f"pinned_{dynamic_id}_{ts}.png")
                    await comment_el.screenshot(path=path)
                    logger.info(f"📸 置顶评论截图: {path}")
                    return path
                await shot_page.close()
            finally:
                await shot_ctx.close()
        except Exception as e:
            logger.warning(f"⚠️ 置顶评论截图失败: {e}")
        return None

    async def _send_notification(self, dynamic_id, cur_html, cur_img, last_html, last_img, rpid=None):
        """发送通知：按QQ_MODE/EMAIL_MODE/BILI_MODE分流，text模式先推不阻塞截图"""
        try:
            ct = time.strftime("%Y-%m-%d %H:%M:%S")
            screenshot_path = None

            # screenshot 模式统一先生成卡片；任一通道需要图片就生成一次
            need_image = (
                QQ_MODE == "screenshot"
                or EMAIL_MODE == "screenshot"
                or (AUTO_PUBLISH_ENABLED and BILI_MODE == "screenshot")
            )
            if need_image:
                try:
                    screenshot_path = await generate_pinned_card(
                        browser=self.browser,
                        dynamic_id=dynamic_id,
                        current_html=cur_html,
                        current_images=cur_img,
                        rpid=rpid,
                        cookies=await self.context.cookies(),
                        detect_time=ct,
                        output_dir=self.mail_save_dir,
                    )
                except Exception as e:
                    logger.warning(f"卡片生成异常，回退旧截图: {e}")
                    screenshot_path = None

                if screenshot_path:
                    logger.info(f"卡片已生成: {screenshot_path}")
                else:
                    logger.warning("卡片生成失败，回退旧截图")
                    screenshot_path = await self._take_pinned_comment_screenshot(dynamic_id)

            # ── 1) QQ推送 ──
            if QQ_MODE == "screenshot":
                qq_sp = screenshot_path
            else:
                qq_sp = None  # text模式：纯文本+alt属性+评论区图片

            qq = self.comment_renderer.generate_qq_message(UP_NAME, dynamic_id, cur_html, ct, cur_img, qq_sp)
            await send_qq_message(qq, {"up_name": UP_NAME, "dynamic_id": dynamic_id, "current_html": cur_html, "current_time": ct, "current_images": cur_img})
            logger.info(f"✅ QQ消息已发送 (模式={QQ_MODE})")

            # ── 2) 邮件推送 ──
            if EMAIL_MODE == "screenshot":
                email_sp = screenshot_path
            else:
                email_sp = None  # text模式：文字+表情+评论区图片

            body = self.comment_renderer.render_email_content(dynamic_id, cur_html, cur_img, last_html, last_img, ct, email_sp)
            asyncio.create_task(asyncio.to_thread(send_email, subject=f"【{UP_NAME}】{EMAIL_PINNED_SUBJECT}", content=body))
            logger.info(f"📧 置顶评论邮件已提交 (模式={EMAIL_MODE})")

            # ── 3) B站发布 ──
            if AUTO_PUBLISH_ENABLED and BILI_MODE == "screenshot":
                if screenshot_path:
                    # 提取置顶评论纯文本（与QQ推送一致：img替换为alt文本）
                    try:
                        from bs4 import BeautifulSoup
                        soup = BeautifulSoup(cur_html, "html.parser")
                        for img in soup.find_all("img"):
                            alt = img.get("alt", "")
                            if alt:
                                img.replace_with(alt)
                            else:
                                img.decompose()
                        comment_plain = soup.get_text(strip=True)
                    except Exception:
                        comment_plain = ""
                    asyncio.create_task(
                        auto_publish.publish_dynamic(dynamic_id, screenshot_path,
                                                     await self.context.cookies(),
                                                     UP_NAME, AUTO_PUBLISH_TOPIC_ID, AUTO_PUBLISH_TOPIC_NAME, rpid,
                                                     comment_text=comment_plain)
                    )
                    logger.info("📤 自动发布动态已提交")
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
            for uid, name in [(UP_UID, UP_NAME)]:

                # 1) 优先检测置顶评论（核心功能）
                pinned_id = self.pinned_dynamic_id
                is_new = (self.history_data.get("last_pinned_dynamic_id") and pinned_id != self.history_data.get("last_pinned_dynamic_id"))
                if await self.check_dynamic_changes(pinned_id, is_new_pinned=is_new):
                    success = True

                # 2) API 拿动态列表 → 检测新动态
                api_list, api_ok = await self.get_api_dynamics(uid)
                # API独立健康统计
                if api_ok:
                    self.health_checker.increment_api_success()
                else:
                    self.health_checker.increment_api_failure()
                performance_monitor.record_api_result(api_ok)
                if api_list:
                    seen = set(self.history_data.get("seen_dynamics", []))
                    is_cold = len(seen) == 0
                    new_dynamics = []
                    skipped_count = 0
                    for d in api_list:
                        dyn_id = d["dynamic_id"]
                        dyn_type = d.get("type", "")
                        if dyn_id not in seen:
                            seen.add(dyn_id)
                            # 过滤B站自动生成的动态（如直播开播推荐动态，下播后自动消失）
                            if dyn_type in DYNAMIC_SKIP_TYPES:
                                skipped_count += 1
                                continue
                            if not is_cold:
                                new_dynamics.append({"dynamic_id": dyn_id, "url": f"https://t.bilibili.com/{dyn_id}"})
                    if skipped_count:
                        logger.info(f"⏭️ 跳过 {skipped_count} 条自动生成动态（类型: {DYNAMIC_SKIP_TYPES}）")
                    if new_dynamics:
                        logger.info(f"🆕 检测到 {len(new_dynamics)} 条新动态")
                        await self._handle_new_dynamics_batch(new_dynamics, name)
                    if is_cold and seen:
                        logger.info(f"🧊 冷启动：静默记录 {len(seen)} 条（已过滤 {skipped_count} 条自动动态）")
                    self.history_data["seen_dynamics"] = list(seen)[-500:]

                # 3) 定期检测置顶动态是否更换（默认1h一次）
                now = time.time()
                if now - self.last_pinned_check >= PINNED_CHECK_INTERVAL:
                    pr = await check_pinned_dynamic(uid)
                    self.api_pinned_id = pr.get("api_id") or pr.get("current_id") or self.api_pinned_id
                    if pr.get("auto_restored"):
                        # 手动模式下，API检测到B站置顶与手动设定一致 → 自动切回自动模式
                        logger.info(f"✅ 置顶监测已自动切回自动模式 (ID={pr.get('api_id')})")
                        # 发送邮件通知管理邮箱
                        cc = ColorConfig()
                        primary, secondary = cc.get_random_gradient()
                        ct = time.strftime("%Y-%m-%d %H:%M:%S")
                        url = f"https://t.bilibili.com/{pr['api_id']}"
                        html = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8">
<style>
body {{ font-family:'Microsoft YaHei',Arial,sans-serif; margin:0; padding:20px; background-color:#f5f5f5; }}
.container {{ max-width:600px; margin:0 auto; background-color:white; border-radius:10px; box-shadow:0 2px 10px rgba(0,0,0,0.1); overflow:hidden; }}
.header {{ background:linear-gradient(135deg,{primary},{secondary}); color:white; padding:20px; text-align:center; }}
.header h1 {{ margin:0; font-size:22px; }}
.content {{ padding:24px; }}
.info-row {{ background:#f9f9f9; padding:10px 14px; border-radius:6px; margin-bottom:8px; border-left:4px solid {primary}; }}
.info-row strong {{ color:{primary}; }}
.footer {{ text-align:center; padding:16px; color:#999; font-size:12px; }}
</style></head>
<body>
<div class="container">
<div class="header"><h1>【{name}】置顶监测已恢复自动模式</h1></div>
<div class="content">
<div class="info-row"><strong>恢复时间:</strong> {ct}</div>
<div class="info-row"><strong>当前监测动态:</strong> <a href="{url}">{url}</a></div>
<div class="info-row"><strong>原因:</strong> 手动设定的置顶ID已与B站当前置顶一致，系统自动切回自动模式，后续将自动跟踪B站置顶更换。</div>
</div>
<div class="footer">此邮件由 BTCE 自动发送</div>
</div>
</body></html>"""
                        asyncio.create_task(asyncio.to_thread(
                            send_email,
                            subject=f"【{name}】置顶监测已恢复自动模式",
                            content=html,
                            to_emails=STATUS_MONITOR_EMAILS,
                        ))
                        logger.info(f"📧 自动恢复邮件已提交")
                    if pr["changed"] and pr["current_id"]:
                        # 先更新监控目标，让后续检测立即用新置顶
                        self.pinned_dynamic_id = pr["current_id"]
                        self.history_data["last_pinned_dynamic_id"] = None
                        # 同步写入 config.py，持久化到文件（重启不丢失）
                        _update_config_pinned_id(pr["current_id"])
                        logger.info(f"📌 置顶已更换: {pr['previous_id']} -> {pr['current_id']}")

                        # 截图新置顶动态，内嵌base64到邮件
                        shot_path = await self._take_dynamic_screenshot(pr["current_id"])
                        img_tag = ""
                        if shot_path:
                            import base64
                            try:
                                img_bytes = Path(shot_path).read_bytes()
                                b64 = base64.b64encode(img_bytes).decode("ascii")
                                img_tag = f'<img src="data:image/png;base64,{b64}" style="max-width:700px;width:100%;"/>'
                            except Exception:
                                img_tag = '<p>(截图加载失败)</p>'

                        # 生成渐变配色（与置顶评论邮件同款 ColorConfig）
                        cc = ColorConfig()
                        primary, secondary = cc.get_random_gradient()
                        ct = time.strftime("%Y-%m-%d %H:%M:%S")
                        url = f"https://t.bilibili.com/{pr['current_id']}"
                        html = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8">
<style>
body {{ font-family:'Microsoft YaHei',Arial,sans-serif; margin:0; padding:20px; background-color:#f5f5f5; }}
.container {{ max-width:600px; margin:0 auto; background-color:white; border-radius:10px; box-shadow:0 2px 10px rgba(0,0,0,0.1); overflow:hidden; }}
.header {{ background:linear-gradient(135deg,{primary},{secondary}); color:white; padding:20px; text-align:center; }}
.header h1 {{ margin:0; font-size:22px; }}
.content {{ padding:24px; }}
.info-row {{ background:#f9f9f9; padding:10px 14px; border-radius:6px; margin-bottom:8px; border-left:4px solid {primary}; }}
.info-row strong {{ color:{primary}; }}
.screenshot {{ text-align:center; margin-top:16px; }}
.screenshot img {{ max-width:100%; border-radius:6px; box-shadow:0 2px 8px rgba(0,0,0,0.1); }}
.footer {{ text-align:center; padding:16px; color:#999; font-size:12px; }}
</style></head>
<body>
<div class="container">
<div class="header"><h1>【{name}】置顶动态已更换</h1></div>
<div class="content">
<div class="info-row"><strong>变更时间:</strong> {ct}</div>
<div class="info-row"><strong>新置顶动态ID:</strong> {pr['current_id']}</div>
<div class="info-row"><strong>旧置顶动态ID:</strong> {pr['previous_id']}</div>
<div class="info-row"><strong>链接:</strong> <a href="{url}">{url}</a></div>
<div class="screenshot">{img_tag if img_tag else '<p>(截图生成中…)</p>'}</div>
</div>
<div class="footer">此邮件由 BTCE 自动发送</div>
</div>
</body></html>"""
                        asyncio.create_task(asyncio.to_thread(
                            send_email,
                            subject=f"【{name}】置顶动态已更换",
                            content=html,
                            to_emails=STATUS_MONITOR_EMAILS,
                        ))
                        logger.info(f"📧 置顶更换邮件已提交")
                    elif pr["current_id"]:
                        logger.info(f"📌 置顶检测完成: {pr['current_id']} (无变化)")
                    self.last_pinned_check = now

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
        logger.info(f"=== {UP_NAME} 监控 UID={UP_UID} 置顶ID={self.pinned_dynamic_id} 间隔={self.check_interval}s ===")
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
