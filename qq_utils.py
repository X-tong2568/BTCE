# qq_utils.py
import aiohttp
import asyncio
from typing import List
from logger_config import logger
from config_qq import QQ_BOT_API_URL, QQ_BOT_ACCESS_TOKEN, QQ_GROUP_IDS, QQ_PUSH_ENABLED, MAX_MESSAGE_LENGTH
from qq_message_generator import qq_message_generator


class QQMessageSender:
    """QQ群消息发送器"""

    def __init__(self):
        self.api_url = QQ_BOT_API_URL
        self.access_token = QQ_BOT_ACCESS_TOKEN
        self.headers = {}

        if self.access_token:
            self.headers = {"Authorization": f"Bearer {self.access_token}"}

    async def send_group_message(self, group_id: str, message: str, is_degraded: bool = False) -> bool:
        """发送群消息（带重试机制）"""
        if not QQ_PUSH_ENABLED:
            logger.info("QQ推送已禁用，跳过发送")
            return True

        # 截断过长的消息
        if len(message) > MAX_MESSAGE_LENGTH:
            message = message[:MAX_MESSAGE_LENGTH - 3] + "..."

        # 重试机制
        max_retries = 3 if not is_degraded else 1
        message_type = "降级消息" if is_degraded else "普通消息"

        for attempt in range(max_retries):
            try:
                async with aiohttp.ClientSession() as session:
                    payload = {
                        "group_id": group_id,
                        "message": message,
                        "auto_escape": False
                    }

                    async with session.post(
                        f"{self.api_url}/send_group_msg",
                        json=payload,
                        headers=self.headers,
                        timeout=30
                    ) as response:

                        if response.status == 200:
                            result = await response.json()
                            if result.get("status") == "ok":
                                logger.info(f"✅ QQ群 {group_id} {message_type}发送成功")
                                return True
                            else:
                                logger.error(f"❌❌ QQ群 {group_id} {message_type}第{attempt + 1}次发送失败: {result}")
                        else:
                            logger.error(f"❌❌ QQ群 {group_id} {message_type}第{attempt + 1}次API请求失败: {response.status}")

                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    logger.info(f"等待{wait_time}秒后重试...")
                    await asyncio.sleep(wait_time)

            except asyncio.TimeoutError:
                logger.error(f"❌❌ QQ群 {group_id} {message_type}第{attempt + 1}次消息发送超时")
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    logger.info(f"等待{wait_time}秒后重试...")
                    await asyncio.sleep(wait_time)

            except Exception as e:
                logger.error(f"❌❌ QQ群 {group_id} {message_type}第{attempt + 1}次消息发送异常: {e}")
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    logger.info(f"等待{wait_time}秒后重试...")
                    await asyncio.sleep(wait_time)

        logger.error(f"❌❌ QQ群 {group_id} {message_type}发送失败，已重试{max_retries}次")
        return False

    async def send_with_degradation(self, original_message: str, message_data: dict) -> List[bool]:
        """带降级机制的消息发送"""

        if not QQ_PUSH_ENABLED:
            logger.info("QQ推送已禁用，跳过发送")
            return [True] * len(QQ_GROUP_IDS)

        # 首先尝试发送原始消息
        original_results = await self.send_to_all_groups(original_message)

        # 统计失败群
        failed_groups = []
        for i, result in enumerate(original_results):
            if not result:
                failed_groups.append(QQ_GROUP_IDS[i])

        if not failed_groups:
            logger.info("✅ 所有QQ群原始消息发送成功")
            return original_results

        logger.warning(f"⚠️ 检测到 {len(failed_groups)} 个群消息发送失败，启用降级机制")

        # 生成降级消息
        degraded_message = qq_message_generator.generate_degraded_message(
            up_name=message_data.get("up_name", ""),
            dynamic_id=message_data.get("dynamic_id", ""),
            current_html=message_data.get("current_html", ""),
            current_time=message_data.get("current_time", ""),
            current_images=message_data.get("current_images", []),
            original_failed=True
        )

        degraded_results = []
        for group_id in failed_groups:
            result = await self.send_group_message(group_id, degraded_message, is_degraded=True)
            degraded_results.append(result)

            if result:
                logger.info(f"✅ QQ群 {group_id} 降级消息发送成功")
            else:
                logger.error(f"❌❌ QQ群 {group_id} 降级消息也发送失败")

        # 合并结果
        final_results = []
        for i, group_id in enumerate(QQ_GROUP_IDS):
            if group_id in failed_groups:
                failed_index = failed_groups.index(group_id)
                final_results.append(degraded_results[failed_index])
            else:
                final_results.append(original_results[i])

        return final_results

    async def send_to_all_groups(self, message: str) -> List[bool]:
        """向所有配置的QQ群发送消息"""
        tasks = [self.send_group_message(group_id, message) for group_id in QQ_GROUP_IDS]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        processed_results = []
        for result in results:
            if isinstance(result, Exception):
                logger.error(f"❌❌ QQ群消息发送异常: {result}")
                processed_results.append(False)
            else:
                processed_results.append(result)

        success_count = sum(1 for r in processed_results if r is True)
        total_count = len(QQ_GROUP_IDS)

        if success_count == total_count:
            logger.info(f"✅ 所有QQ群消息发送成功 ({success_count}/{total_count})")
        else:
            logger.warning(f"⚠️ QQ群消息发送结果: {success_count}成功, {total_count - success_count}失败")

        return processed_results


# 全局实例
qq_sender = QQMessageSender()


async def send_qq_message(message: str, message_data: dict = None) -> List[bool]:
    """发送QQ群消息（便捷函数）"""
    if message_data and any(key in message_data for key in ["up_name", "dynamic_id", "current_html"]):
        return await qq_sender.send_with_degradation(message, message_data)
    else:
        return await qq_sender.send_to_all_groups(message)
