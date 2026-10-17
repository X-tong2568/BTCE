# monitor_scheduler.py
import asyncio
import json
import time
from datetime import datetime
from logger_config import logger
from live_monitor import live_monitor
from qq_message_generator import qq_message_generator
from config import (LIVE_ROOM_ID, LIVE_CHECK_INTERVAL, COOKIE_FILE, UP_NAME,
                    AUTO_PUBLISH_TOPIC_ID, AUTO_PUBLISH_TOPIC_NAME,
                    LIVE_BILI_PUBLISH_ENABLED)
from email_utils import send_email
from qq_utils import send_qq_message
from config_email import TO_EMAILS, STATUS_MONITOR_EMAILS
import auto_publish


class LiveMonitorScheduler:
    """直播间监控调度器"""

    def __init__(self):
        self.logger = logger.getChild('live_scheduler')
        self.is_running = False
        self.last_successful_check = None
        self.check_count = 0

    @staticmethod
    def _load_cookies_list() -> list:
        """从cookie文件加载Playwright格式的cookie列表，供auto_publish使用"""
        try:
            if not COOKIE_FILE.exists():
                logger.warning("⚠️ Cookie文件不存在，无法发布B站动态")
                return []
            with open(COOKIE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"❌ 加载Cookie失败: {e}")
            return []

    async def start_monitoring(self):
        """开始监控直播间"""
        self.is_running = True
        self.logger.info(f"📺📺 直播间监控启动 - 房间号: {LIVE_ROOM_ID}, 间隔: {LIVE_CHECK_INTERVAL}秒")

        try:
            while self.is_running:
                start_time = time.time()

                # 执行监控检查
                await self.execute_live_check()

                # 计算等待时间
                elapsed = time.time() - start_time
                wait_time = max(0, LIVE_CHECK_INTERVAL - elapsed)

                if wait_time > 0:
                    next_check = datetime.fromtimestamp(time.time() + wait_time).strftime('%H:%M:%S')
                    self.logger.debug(f"⏰⏰ 下次直播检查: {next_check} (等待{wait_time:.1f}秒)")
                    await asyncio.sleep(wait_time)
                else:
                    self.logger.warning(f"⏱⏱ 直播检查耗时({elapsed:.1f}秒)超过间隔，立即开始下一轮")

        except asyncio.CancelledError:
            self.logger.info("⏹⏹ 直播间监控任务被取消")
        except Exception as e:
            self.logger.error(f"❌❌ 直播间监控任务异常: {e}")
        finally:
            await self.cleanup()

    async def execute_live_check(self):
        """执行单次直播检查"""
        self.check_count += 1

        try:
            live_info = await live_monitor.check_live_status(LIVE_ROOM_ID)

            if live_info:
                self.last_successful_check = time.time()

                # 检测到状态变化时发送通知（排除首次检测）
                if live_info.get('status_changed') and live_info.get('change_type') != 'initial':
                    await self.send_live_notification(live_info)

                self.logger.debug(f"✅✅ 直播检查完成 - 第{self.check_count}轮")
            else:
                self.logger.warning(f"⚠️⚠️ 第{self.check_count}轮直播检查失败")

        except Exception as e:
            self.logger.error(f"❌❌ 执行直播检查异常: {e}")

    async def send_live_notification(self, live_info: dict):
        """发送直播状态变化通知（邮件+QQ+B站动态）"""
        try:
            # 生成邮件内容
            subject, email_content = live_monitor.format_email_content(live_info)

            # 发送邮件通知
            email_success = await asyncio.to_thread(
                send_email,
                subject=subject,
                content=email_content,
                to_emails=TO_EMAILS
            )

            if email_success:
                self.logger.info("✅ 直播状态邮件发送成功")
            else:
                self.logger.error("❌❌❌❌ 直播状态邮件发送失败")

            # 生成QQ消息
            qq_message = qq_message_generator.generate_live_qq_message(live_info)
            qq_results = await send_qq_message(qq_message)

            qq_success_count = sum(1 for r in qq_results if r is True)
            if qq_results:
                self.logger.info(f"✅ QQ直播通知发送结果: {qq_success_count}/{len(qq_results)} 成功")

            # ── B站动态发布：标题更新时同步到话题 ──
            # 开播同轮改标题时，QQ/邮件只发一条开播提醒，但B站渠道仍需发布
            title_changed_on_start = (
                live_info.get("change_type") == "live_start"
                and live_info.get("title_changed")
            )
            if (LIVE_BILI_PUBLISH_ENABLED
                    and (live_info.get("change_type") == "title_change"
                         or title_changed_on_start)):
                cookies = self._load_cookies_list()
                if cookies:
                    status_tags = qq_message_generator.build_live_status_tags(live_info) or ""
                    asyncio.create_task(
                        auto_publish.publish_live_update(
                            cover_url=live_info.get("cover", ""),
                            title=live_info.get("title", "无标题"),
                            room_id=live_info["room_id"],
                            status_tags=status_tags,
                            cookies=cookies,
                            topic_id=AUTO_PUBLISH_TOPIC_ID,
                            topic_name=AUTO_PUBLISH_TOPIC_NAME,
                            up_name=UP_NAME,
                            event_type="live_start" if title_changed_on_start else "title_change",
                        )
                    )
                    self.logger.info(
                        "📤 开播B站动态已提交" if title_changed_on_start
                        else "📤 直播标题更新B站动态已提交"
                    )
                else:
                    self.logger.warning("⚠️ 无Cookie，跳过B站直播动态发布")

        except Exception as e:
            self.logger.error(f"❌❌❌❌ 发送直播通知异常: {e}")

    async def stop_monitoring(self):
        """停止监控"""
        self.is_running = False
        self.logger.info("🛑🛑 直播间监控停止")

    async def cleanup(self):
        """清理资源"""
        await live_monitor.close_session()
        self.logger.info("✅✅ 直播间监控资源清理完成")

    def get_scheduler_stats(self) -> dict:
        """获取调度器统计信息"""
        return {
            'is_running': self.is_running,
            'check_count': self.check_count,
            'last_successful_check': self.last_successful_check,
            'live_monitor_stats': live_monitor.get_monitor_stats()
        }


# 全局实例
live_scheduler = LiveMonitorScheduler()