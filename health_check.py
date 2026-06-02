# health_check.py
import asyncio
import psutil
import time
from datetime import datetime
from logger_config import logger
from config import MEMORY_THRESHOLD_MB, TASK_TIMEOUT
from retry_decorator import NETWORK_RETRY_CONFIG, async_retry


class HealthChecker:
    """增强的健康检查类"""

    _instance = None

    @classmethod
    def instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        self.loop_count = None
        self.start_time = time.time()
        self.success_count = 0      # 置顶评论抓取成功次数
        self.failure_count = 0      # 置顶评论抓取失败次数
        self.api_success_count = 0  # API动态列表成功次数
        self.api_failure_count = 0  # API动态列表失败次数
        self.last_health_check = time.time()  # 初始化时间戳

    async def check_memory_usage(self):
        """检查内存使用情况"""
        process = psutil.Process()
        memory_mb = process.memory_info().rss / 1024 / 1024

        if memory_mb > MEMORY_THRESHOLD_MB:
            logger.warning(f"⚠️ 内存使用较高: {memory_mb:.2f} MB (阈值: {MEMORY_THRESHOLD_MB} MB)")
            return False
        return True

    async def check_browser_health(self, page):
        """检查浏览器健康状态"""
        try:
            # 更新检查时间戳
            self.last_health_check = time.time()

            # 设置页面超时
            page.set_default_timeout(15000)

            # 测试访问B站首页
            await asyncio.wait_for(
                page.goto("https://www.bilibili.com", wait_until="networkidle"),
                timeout=TASK_TIMEOUT
            )

            # 检查页面标题
            title = await page.title()
            if "bilibili" not in title.lower():
                raise Exception("页面标题异常")

            logger.debug("✅ 浏览器健康检查通过")
            return True

        except asyncio.TimeoutError:
            logger.error("❌ 浏览器健康检查超时")
            return False
        except Exception as e:
            logger.error(f"❌ 浏览器健康检查失败: {e}")
            return False

    async def check_network_connectivity(self):
        """检查网络连通性"""
        try:
            # 更新检查时间戳
            self.last_health_check = time.time()

            # 这里可以添加ping测试或其他网络检查
            return True
        except Exception as e:
            logger.error(f"❌ 网络连通性检查失败: {e}")
            return False

    @async_retry(NETWORK_RETRY_CONFIG)
    async def comprehensive_check(self, page):
        """综合健康检查"""
        # 更新检查时间戳
        self.last_health_check = time.time()

        checks = [
            self.check_memory_usage(),
            self.check_browser_health(page),
            self.check_network_connectivity()
        ]

        results = await asyncio.gather(*checks, return_exceptions=True)

        # 统计成功失败的检查
        success_checks = sum(1 for r in results if r is True)
        failed_checks = len(results) - success_checks

        if failed_checks > 0:
            logger.warning(f"⚠️ 健康检查: {success_checks}成功, {failed_checks}失败")
            return False

        return True

    def get_uptime(self):
        """获取运行时间"""
        uptime_seconds = time.time() - self.start_time
        hours = int(uptime_seconds // 3600)
        minutes = int((uptime_seconds % 3600) // 60)
        return f"{hours}h {minutes}m"

    def increment_success(self):
        """增加置顶评论成功计数"""
        self.success_count += 1

    def increment_failure(self):
        """增加置顶评论失败计数"""
        self.failure_count += 1

    def increment_api_success(self):
        """增加API动态列表成功计数"""
        self.api_success_count += 1

    def increment_api_failure(self):
        """增加API动态列表失败计数"""
        self.api_failure_count += 1

    def get_stats(self, total_loops=None):
        """获取统计信息（含置顶评论 + API 双通道独立统计）"""
        total = total_loops if total_loops is not None else (self.success_count + self.failure_count)
        success_rate = (self.success_count / total * 100) if total > 0 else 0

        api_total = self.api_success_count + self.api_failure_count
        api_success_rate = (self.api_success_count / api_total * 100) if api_total > 0 else 0

        return {
            "重启后运行时间": self.get_uptime(),
            "抓取次数": total,
            "抓取成功次数": self.success_count,
            "抓取失败次数": total - self.success_count,
            "抓取成功率": f"{success_rate:.3f}%",
            "最后一次抓取时间": datetime.fromtimestamp(self.last_health_check).strftime('%H:%M:%S'),
            "API请求次数": api_total,
            "API成功次数": self.api_success_count,
            "API失败次数": self.api_failure_count,
            "API成功率": f"{api_success_rate:.3f}%",
        }


# 全局健康检查函数
async def perform_health_checks():
    """执行健康检查"""
    checker = HealthChecker()
    try:
        # 这里可以添加更多的健康检查逻辑
        memory_ok = await checker.check_memory_usage()
        return {
            "memory_usage_ok": memory_ok,
            "uptime": checker.get_uptime()
        }
    except Exception as e:
        logger.error(f"❌ 健康检查执行失败: {e}")
        return {"error": str(e)}