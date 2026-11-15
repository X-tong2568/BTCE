# qq_message_generator.py
from bs4 import BeautifulSoup
from datetime import datetime
from typing import Dict, Any, Optional
from logger_config import logger
from config import QQ_PINNED_UPDATE_TEXT, QQ_SINGLE_LINK_LABEL, QQ_SINGLE_TIME_LABEL, UP_NAME


class QQMessageGenerator:
    """QQ消息生成类"""

    def generate_qq_message(self, up_name: str, dynamic_id: str, current_html: str, current_time: str,
                            current_images: list, screenshot_path: str = None) -> str:
        """生成QQ群推送消息（优先用截图，失败兜底文字+表情+评论区图片）"""
        try:
            qq_message = f"【{up_name}】{QQ_PINNED_UPDATE_TEXT}\n"
            media = ""

            if screenshot_path:
                # 截图模式：图片放消息末尾（文-文-图），官方通道拆成两条而不是三条
                media = f"[CQ:image,file=file:///{screenshot_path.replace(chr(92), '/')}]"
            else:
                # 兜底模式：旧版文字+表情alt+评论区图片
                soup = BeautifulSoup(current_html, "html.parser")
                for img in soup.find_all("img"):
                    alt_text = img.get("alt", "")
                    if alt_text:
                        img.replace_with(alt_text)
                    else:
                        img.decompose()
                text_content = soup.get_text(strip=True)
                qq_message += f"{text_content}\n"

                if current_images:
                    # 图片统一放到文字之后（文-文-图）；CQ 码首尾相接，
                    # 避免官方通道把图片间的换行拆成额外的空文本消息
                    media_lines = ["📸 图片："]
                    if len(current_images) > 9:
                        media_lines.append(f"... 还有 {len(current_images) - 9} 张图片")
                    image_codes = "".join(
                        f"[CQ:image,file={img_url}]" for img_url in current_images[:9]
                    )
                    media = "\n".join(media_lines) + "\n" + image_codes

            qq_message += "----------------\n"
            qq_message += f"检测时间: {current_time}\n"
            qq_message += f"监测动态: https://t.bilibili.com/{dynamic_id}\n"
            qq_message += "----------------"
            if media:
                qq_message += f"\n{media}"

            return qq_message

        except Exception as e:
            logger.error(f" 生成QQ消息失败: {e}")
            # 备用消息格式
            backup_msg = f"【{up_name}】置顶评论更新通知\n动态: {dynamic_id}\n时间: {current_time}"
            if current_images:
                backup_msg += f"\n包含 {len(current_images)} 张图片"
            return backup_msg

    def generate_degraded_message(self, up_name: str, dynamic_id: str, current_html: str, current_time: str,
                                  current_images: list, original_failed: bool = False) -> str:
        """生成降级消息（不含图片CQ码，仅文字提示）"""
        try:
            # 使用BeautifulSoup处理HTML，将表情图片替换为alt文字
            soup = BeautifulSoup(current_html, "html.parser")

            # 找到所有表情图片，替换为alt属性中的文字
            for img in soup.find_all("img"):
                alt_text = img.get("alt", "")
                if alt_text:
                    img.replace_with(alt_text)
                else:
                    img.decompose()

            # 提取纯文本内容
            text_content = soup.get_text(strip=True)

            # 生成降级消息
            degraded_message = f"【{up_name}】{QQ_PINNED_UPDATE_TEXT}\n"
            degraded_message += f"{text_content}\n"

            # 添加图片提示（不含CQ码）
            if current_images:
                if original_failed:
                    degraded_message += "⚠️ 图片推送超时，已启用降级模式\n"
                degraded_message += f"📸 包含 {len(current_images)} 张图片，请自行查看动态\n"

            degraded_message += "----------------\n"
            degraded_message += f"检测时间: {current_time}\n"
            degraded_message += f"监测动态: https://t.bilibili.com/{dynamic_id}\n"

            if original_failed:
                degraded_message += "🚨 消息已降级（图片推送超时）\n"

            degraded_message += "----------------"

            return degraded_message

        except Exception as e:
            logger.error(f"❌ 生成降级消息失败: {e}")
            # 最终的备用消息
            return f"【{up_name}】动态更新通知\n动态ID: {dynamic_id}\n时间: {current_time}\n⚠️ 包含图片，请查看原动态"

    def generate_new_dynamic_degraded_message(self, up_name: str, dynamic_id: str, current_time: str) -> str:
        """生成新动态推送的降级消息(纯文本,不含动态正文,提示自行查看动态)"""
        parts = [f"【{up_name}】发布了新动态~"]
        parts.append("⚠️ 消息推送超时，已启用降级模式，请点击链接自行查看动态")
        parts.append("----------------")
        parts.append(f"{QQ_SINGLE_LINK_LABEL} https://t.bilibili.com/{dynamic_id}")
        parts.append(f"{QQ_SINGLE_TIME_LABEL} {current_time}")
        parts.append("----------------")
        return '\n'.join(parts)

    def generate_new_dynamic_qq_message(self, up_name: str, dynamic_id: str, dynamic_url: str,
                                        current_time: str, content_text: str = "",
                                        screenshot_path: str = None) -> str:
        """生成新动态 QQ 群推送消息（支持截图）"""
        parts = [f"【{up_name}】发布了新动态~"]
        if content_text:
            parts.append(content_text[:300])
        parts.append(f"{QQ_SINGLE_LINK_LABEL} {dynamic_url}")
        parts.append(f"{QQ_SINGLE_TIME_LABEL} {current_time}")
        # 图片放最后（文-文-图），官方通道拆成两条而不是三条
        if screenshot_path:
            parts.append(f"[CQ:image,file=file:///{screenshot_path.replace(chr(92), '/')}]")
        return '\n'.join(parts)

    @staticmethod
    def build_live_status_tags(live_info: Dict[str, Any]) -> Optional[str]:
        """根据 room_init 字段生成直播状态标签文本，无特殊状态返回 None"""
        tags = []
        if live_info.get("encrypted"):
            tags.append("🔒 密码保护")
        if live_info.get("is_locked"):
            tags.append("🔐 房间已锁定")
        if live_info.get("is_hidden"):
            tags.append("👻 房间已隐藏")
        st = live_info.get("special_type", 0)
        if st == 1:
            tags.append("💰 付费直播")
        elif st == 2:
            tags.append("🎊 拜年纪")
        return " | ".join(tags) if tags else None

    def generate_live_qq_message(self, live_info: Dict[str, Any]) -> str:
        """生成直播状态变更 QQ 群推送消息（开播/下播/标题更新）"""
        ct = live_info["change_type"]
        title = live_info.get("title", "无标题")
        cover = live_info.get("cover", "")
        room_id = live_info["room_id"]
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        prefix = {
            "live_start": "🎉 开播提醒",
            "live_end": "💤 下播提醒",
            "title_change": "✏️ 标题更新",
        }.get(ct, "📺 状态更新")

        qq_msg = f"【{UP_NAME}直播监控】{prefix}\n"
        qq_msg += f"标题：{title}\n"
        qq_msg += f"链接：https://live.bilibili.com/{room_id}\n"
        qq_msg += f"时间：{current_time}\n"

        status_tags = self.build_live_status_tags(live_info)
        if status_tags:
            qq_msg += f"{status_tags}\n"

        qq_msg += "----------------"

        # 封面放最后（文-文-图），官方通道拆成两条而不是三条
        if cover:
            qq_msg += f"\n封面：\n[CQ:image,file={cover}]"

        return qq_msg


# 全局实例
qq_message_generator = QQMessageGenerator()