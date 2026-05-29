# render_comment.py
import time
import asyncio
from bs4 import BeautifulSoup
from config import UP_NAME
from logger_config import logger
from color_config import ColorConfig
from email_renderer import EmailRenderer
from qq_message_generator import QQMessageGenerator


class CommentRenderer:
    """评论渲染和变化检测类"""

    def __init__(self):
        """初始化颜色生成器和渲染器"""
        self.color_config = ColorConfig()
        self.email_renderer = EmailRenderer(self.color_config)
        self.qq_generator = QQMessageGenerator()

    def _get_random_gradient(self):
        """获取随机双色渐变（对比色）"""
        return self.color_config.get_random_gradient()

    @staticmethod
    def extract_text_from_html(html_content: str) -> str:
        """从HTML提取纯文字"""
        if not html_content:
            return ""

        soup = BeautifulSoup(html_content, "html.parser")
        return soup.get_text(strip=True)

    async def get_pinned_comment(self, page, dynamic_id):
        """
        抓取置顶评论：
        - pinned_comment_html: 评论 HTML（含文字+表情）
        - comment_images: 评论区上传的图片 URL 列表
        """
        await page.goto(f"https://t.bilibili.com/{dynamic_id}")

        try:
            await page.wait_for_selector("bili-comment-thread-renderer", timeout=15000)
        except:
            return "未找到置顶评论", []

        # 模拟滚动加载更多评论
        for _ in range(5):
            await page.evaluate("window.scrollBy(0, 1000)")
            await asyncio.sleep(1)

        pinned_comment_html = None
        comment_images = []

        comment_items = await page.query_selector_all("bili-comment-thread-renderer")
        for item in comment_items:
            top_tag = await item.query_selector("i#top")
            if top_tag:
                # 文字+表情 HTML
                content_element = await item.query_selector("bili-rich-text p#contents")
                if content_element:
                    pinned_comment_html = await content_element.inner_html()

                # 评论区上传图片 - 修复图片获取逻辑
                pics_renderer = await item.query_selector("bili-comment-pictures-renderer")
                if pics_renderer:
                    try:
                        # 使用 evaluate 方法访问 shadow DOM
                        img_src_list = await pics_renderer.evaluate(
                            """(el) => {
                                const imgs = [];
                                const shadow = el.shadowRoot;
                                if (shadow) {
                                    const img_tags = shadow.querySelectorAll('img');
                                    img_tags.forEach(img => {
                                        let src = img.src;
                                        if (src.startsWith('//')) {
                                            src = 'https:' + src;
                                        }
                                        // 移除图片参数，获取原始图片
                                        if (src.includes('@')) {
                                            src = src.split('@')[0];
                                        }
                                        imgs.push(src);
                                    });
                                }
                                return imgs;
                            }"""
                        )
                        comment_images.extend(img_src_list)
                    except Exception as e:
                        logger.error(f"❌❌ 通过shadow DOM获取图片失败: {e}")

                        # 备用方法：尝试直接获取图片元素
                        try:
                            img_elements = await pics_renderer.query_selector_all('img')
                            for img in img_elements:
                                src = await img.get_attribute('src')
                                if src:
                                    if src.startswith('//'):
                                        src = 'https:' + src
                                    if '@' in src:
                                        src = src.split('@')[0]
                                    if src not in comment_images:
                                        comment_images.append(src)
                        except Exception as e2:
                            logger.error(f"❌❌ 直接获取图片元素失败: {e2}")

                break

        if pinned_comment_html:
            return pinned_comment_html.strip(), comment_images
        return "未找到置顶评论", []

    async def detect_comment_change(self, current_html, current_images, last_html, last_images):
        """检测评论变化"""
        try:
            current_text = self.extract_text_from_html(current_html)
            last_text = self.extract_text_from_html(last_html)

            logger.info(f"当前置顶评论: {current_text}")
            logger.info(f"上次记录: {last_text if last_text else '无记录'}")

            # 检测文字变化
            if last_text and current_text != last_text:
                logger.info("🔔 检测到置顶评论文字变化！")
                return True

            # 检测图片变化
            if set(current_images) != set(last_images):
                logger.info("🔔 检测到置顶评论图片变化！")
                return True

            return False

        except Exception as e:
            logger.error(f"❌ 检测评论变化失败: {e}")
            return False

    def render_email_content(self, dynamic_id, current_html, current_images, last_html, last_images, current_time=None, screenshot_path=None):
        """渲染邮件内容 - 调用EmailRenderer"""
        return self.email_renderer.render_email_content(
            dynamic_id, current_html, current_images, last_html, last_images, current_time, screenshot_path
        )

    def generate_qq_message(self, up_name: str, dynamic_id: str, current_html: str, current_time: str,
                            current_images: list, screenshot_path: str = None) -> str:
        """生成QQ群推送消息 - 调用QQMessageGenerator"""
        return self.qq_generator.generate_qq_message(
            up_name, dynamic_id, current_html, current_time, current_images, screenshot_path
        )

    # ------------------------------------------------------------------
    # 新动态通知
    # ------------------------------------------------------------------
    def render_new_dynamic_email(self, up_name: str, dynamic_id: str, dynamic_url: str, current_time: str, content_text: str = "") -> str:
        """生成新动态邮件 HTML"""
        return self.email_renderer.render_new_dynamic_email(up_name, dynamic_id, dynamic_url, current_time, content_text)

    def generate_new_dynamic_qq_message(self, up_name: str, dynamic_id: str, dynamic_url: str, current_time: str, content_text: str = "") -> str:
        """生成新动态 QQ 消息"""
        return self.qq_generator.generate_new_dynamic_qq_message(up_name, dynamic_id, dynamic_url, current_time, content_text)

    def render_new_dynamics_batch_email(self, up_name: str, new_dynamics: list, current_time: str) -> str:
        """生成批量新动态邮件"""
        return self.email_renderer.render_new_dynamics_batch_email(up_name, new_dynamics, current_time)

    def generate_new_dynamics_batch_qq_message(self, up_name: str, new_dynamics: list, current_time: str) -> str:
        """生成批量新动态 QQ 消息"""
        return self.qq_generator.generate_new_dynamics_batch_qq_message(up_name, new_dynamics, current_time)