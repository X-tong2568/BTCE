import asyncio
import psutil
import time
from datetime import datetime
from logger_config import logger
from email_utils import send_email
from config_email import STATUS_MONITOR_EMAILS
from config import (
    P1_TOTAL_FAILURE_THRESHOLD,
    P2_SUCCESS_RATE_THRESHOLD,
    API_P1_FAILURE_THRESHOLD,
    API_P2_SUCCESS_RATE_THRESHOLD,
    PERFORMANCE_REPORT_CYCLE_INTERVAL
)


#| 邮件类型      | 主题语义    | 主色            |
#| --------- | ------- | ------------- |
#| **P1 告警** | 严重 / 紧急 | 深橙色 `#E65100` |
#| **P2 告警** | 警告 / 风险 | 琥珀色 `#F9A825` |
#| **性能报告**  | 稳定 / 中性 | 青绿色 `#00796B` |
class PerformanceMonitor:
    """性能监控器：修复P1/P2告警触发问题，卡片式邮件保留详细指标"""

    def __init__(self):
        self.total_cycles = 0
        self.cumulative_success = 0
        self.cumulative_failure = 0
        self.memory_peak = 0
        self.cycle_durations = []
        self.start_time = time.time()
        self.last_alert_time = 0
        self.last_report_cycle = 0
        self.p1_alert_sent = False
        self.p2_alert_sent = False
        self.report_sent = False

        # API 独立统计
        self.api_total = 0
        self.api_success = 0
        self.api_failure = 0
        self.api_consecutive_failures = 0  # API当前连续失败次数（用于P1）
        self.api_p1_alert_sent = False
        self.api_p2_alert_sent = False

        logger.info("📊 性能监控器初始化完成（双通道P1/P2告警）")
        logger.info(f"  - 报告间隔: 每{PERFORMANCE_REPORT_CYCLE_INTERVAL}轮")
        logger.info(f"  - 浏览器 P1: 失败≥{P1_TOTAL_FAILURE_THRESHOLD}  P2: 成功率<{P2_SUCCESS_RATE_THRESHOLD*100:.0f}%")
        logger.info(f"  - API    P1: 失败≥{API_P1_FAILURE_THRESHOLD}  P2: 成功率<{API_P2_SUCCESS_RATE_THRESHOLD*100:.0f}%")

    async def record_memory_usage(self):
        try:
            process = psutil.Process()
            memory_mb = process.memory_info().rss / 1024 / 1024
            if memory_mb > self.memory_peak:
                self.memory_peak = memory_mb
            return memory_mb
        except Exception as e:
            logger.error(f"❌ 记录内存使用失败: {e}")
            return 0

    def record_cycle(self, cycle_number, success, duration=None):
        """记录每轮整体（置顶评论）结果，触发浏览器P1/P2检查"""
        try:
            self.total_cycles = cycle_number
            if success:
                self.cumulative_success += 1
            else:
                self.cumulative_failure += 1
            if duration is not None:
                self.cycle_durations.append({
                    'cycle': cycle_number,
                    'duration': duration,
                    'timestamp': datetime.now(),
                    'success': success
                })
            total = self.total_cycles
            success_count = self.cumulative_success
            failure_count = self.cumulative_failure
            success_rate = success_count / total if total > 0 else 1.0
            logger.debug(
                f"📊 监控状态: 总轮次={total}, 成功={success_count}, 失败={failure_count}, 成功率={success_rate:.2%}")
            self._check_conditions(total, success_count, failure_count, success_rate)
        except Exception as e:
            logger.error(f"❌ 记录轮次结果失败: {e}")

    def record_api_result(self, success):
        """记录API动态列表每次请求结果，触发API独立P1/P2检查"""
        try:
            self.api_total += 1
            if success:
                self.api_success += 1
                self.api_consecutive_failures = 0  # 成功后重置连续失败计数
            else:
                self.api_failure += 1
                self.api_consecutive_failures += 1

            api_rate = self.api_success / self.api_total if self.api_total > 0 else 1.0
            logger.debug(
                f"📡 API状态: 总{self.api_total}, 成功{self.api_success}, "
                f"失败{self.api_failure}, 连续失败{self.api_consecutive_failures}, "
                f"成功率{api_rate:.2%}")
            self._check_api_conditions(self.api_total, self.api_success,
                                       self.api_failure, self.api_consecutive_failures, api_rate)
        except Exception as e:
            logger.error(f"❌ 记录API结果失败: {e}")

    def _check_conditions(self, total, success, failure, success_rate):
        try:
            logger.debug(
                f"🔍 检查条件: 失败={failure}/{P1_TOTAL_FAILURE_THRESHOLD}, 成功率={success_rate:.2%}/{P2_SUCCESS_RATE_THRESHOLD:.0%}")

            if failure >= P1_TOTAL_FAILURE_THRESHOLD and not self.p1_alert_sent:
                logger.error(f"🚨 P1告警条件满足: 失败次数={failure}")
                asyncio.create_task(self._send_p1_alert(total, failure))
                self.p1_alert_sent = True
                self.last_alert_time = time.time()
            elif failure < P1_TOTAL_FAILURE_THRESHOLD and self.p1_alert_sent:
                logger.info(f"🔄 P1告警重置: 失败次数={failure} < 阈值")
                self.p1_alert_sent = False

            if success_rate < P2_SUCCESS_RATE_THRESHOLD and not self.p2_alert_sent:
                logger.error(f"🚨 P2告警条件满足: 成功率={success_rate:.2%}")
                asyncio.create_task(self._send_p2_alert(total, success_rate))
                self.p2_alert_sent = True
                self.last_alert_time = time.time()
            elif success_rate >= P2_SUCCESS_RATE_THRESHOLD and self.p2_alert_sent:
                logger.info(f"🔄 P2告警重置: 成功率={success_rate:.2%} >= 阈值")
                self.p2_alert_sent = False

            if total - self.last_report_cycle >= PERFORMANCE_REPORT_CYCLE_INTERVAL and not self.report_sent:
                logger.info(f"📧 满足报告发送条件: 第{total}轮")
                asyncio.create_task(self._send_report(total))
                self.report_sent = True
                self.last_report_cycle = total
            elif total < self.last_report_cycle + PERFORMANCE_REPORT_CYCLE_INTERVAL and self.report_sent:
                self.report_sent = False

        except Exception as e:
            logger.error(f"❌ 检查条件失败: {e}")

    def _check_api_conditions(self, total, success, failure, consecutive, success_rate):
        """检查API独立P1/P2告警条件"""
        try:
            if consecutive >= API_P1_FAILURE_THRESHOLD and not self.api_p1_alert_sent:
                logger.error(f"🚨 API P1告警: 连续失败={consecutive}")
                asyncio.create_task(self._send_api_p1_alert(total, success, failure, consecutive, success_rate))
                self.api_p1_alert_sent = True
                self.last_alert_time = time.time()
            elif consecutive < API_P1_FAILURE_THRESHOLD and self.api_p1_alert_sent:
                logger.info(f"🔄 API P1告警重置: 连续失败={consecutive} < 阈值")
                self.api_p1_alert_sent = False

            if total >= 10 and success_rate < API_P2_SUCCESS_RATE_THRESHOLD and not self.api_p2_alert_sent:
                logger.error(f"🚨 API P2告警: 成功率={success_rate:.2%}")
                asyncio.create_task(self._send_api_p2_alert(total, success, failure, success_rate))
                self.api_p2_alert_sent = True
                self.last_alert_time = time.time()
            elif success_rate >= API_P2_SUCCESS_RATE_THRESHOLD and self.api_p2_alert_sent:
                logger.info(f"🔄 API P2告警重置: 成功率={success_rate:.2%} >= 阈值")
                self.api_p2_alert_sent = False

        except Exception as e:
            logger.error(f"❌ 检查API条件失败: {e}")

    async def _send_p1_alert(self, total_cycles, failure_count):
        subject = f"🚨 P1告警: 失败次数达 {failure_count} 次 (第{total_cycles}轮)"
        content = self._generate_p1_alert_content(total_cycles, failure_count)
        logger.info(f"📤 正在发送P1告警邮件: {subject}")
        success = await asyncio.to_thread(send_email, subject=subject, content=content, to_emails=STATUS_MONITOR_EMAILS)
        logger.info("✅ P1告警邮件发送成功" if success else "❌ P1告警邮件发送失败")

    async def _send_p2_alert(self, total_cycles, success_rate):
        subject = f"⚠️ P2告警: 成功率过低 {success_rate:.1%} (第{total_cycles}轮)"
        content = self._generate_p2_alert_content(total_cycles, success_rate)
        logger.info(f"📤 正在发送P2告警邮件: {subject}")
        success = await asyncio.to_thread(send_email, subject=subject, content=content, to_emails=STATUS_MONITOR_EMAILS)
        logger.info("✅ P2告警邮件发送成功" if success else "❌ P2告警邮件发送失败")

    # ── API 独立告警（P1连续失败 / P2成功率低）──

    async def _send_api_p1_alert(self, total, success, failure, consecutive, success_rate):
        """发送API P1告警邮件（连续失败）"""
        subject = f"🚨 API P1告警: 连续失败 {consecutive} 次"
        content = self._generate_api_p1_alert_content(total, success, failure, consecutive, success_rate)
        logger.info(f"📤 正在发送API P1告警邮件: {subject}")
        result = await asyncio.to_thread(send_email, subject=subject, content=content, to_emails=STATUS_MONITOR_EMAILS)
        logger.info("✅ API P1告警邮件发送成功" if result else "❌ API P1告警邮件发送失败")

    async def _send_api_p2_alert(self, total, success, failure, success_rate):
        """发送API P2告警邮件（成功率低）"""
        subject = f"⚠️ API P2告警: 成功率过低 {success_rate:.1%}"
        content = self._generate_api_p2_alert_content(total, success, failure, success_rate)
        logger.info(f"📤 正在发送API P2告警邮件: {subject}")
        result = await asyncio.to_thread(send_email, subject=subject, content=content, to_emails=STATUS_MONITOR_EMAILS)
        logger.info("✅ API P2告警邮件发送成功" if result else "❌ API P2告警邮件发送失败")

    def _generate_api_p1_alert_content(self, total, success, failure, consecutive, success_rate):
        """生成API P1告警邮件HTML"""
        theme = "#E65100"
        return f"""
        <!DOCTYPE html>
        <html>
        <head><meta charset="UTF-8">
        <style>
        body {{ font-family:'Microsoft YaHei',Arial; background:#f5f5f5; padding:20px; }}
        .card {{ max-width:650px; margin:auto; background:#fff; border-radius:10px; box-shadow:0 4px 12px rgba(0,0,0,0.12); overflow:hidden; }}
        .header {{ background:linear-gradient(135deg,{theme},#BF360C); color:white; padding:20px; text-align:center; }}
        .content {{ padding:24px; }}
        .stat {{ background:#fff3e0; padding:12px; border-radius:6px; margin-bottom:10px; }}
        table {{ width:100%; border-collapse:collapse; margin-top:15px; }}
        th,td {{ border:1px solid #ddd; padding:8px; text-align:left; }}
        th {{ background:#FFE0B2; }}
        </style></head>
        <body>
        <div class="card">
            <div class="header"><h2>🚨 API 严重告警</h2><p>B站 API 连续调用失败</p></div>
            <div class="content">
                <div class="stat"><strong>连续失败次数：</strong>{consecutive}</div>
                <div class="stat"><strong>总请求次数：</strong>{total}</div>
                <div class="stat"><strong>成功：</strong>{success} | 失败：{failure}</div>
                <div class="stat"><strong>成功率：</strong>{success_rate:.2%}</div>
                <table>
                    <tr><th>类型</th><th>阈值</th><th>当前</th><th>状态</th></tr>
                    <tr><td>P1连续失败</td><td>{API_P1_FAILURE_THRESHOLD}</td><td>{consecutive}</td><td>{'🚨 已触发' if self.api_p1_alert_sent else '✅ 正常'}</td></tr>
                    <tr><td>P2成功率</td><td>{API_P2_SUCCESS_RATE_THRESHOLD:.0%}</td><td>{success_rate:.2%}</td><td>{'⚠️ 已触发' if self.api_p2_alert_sent else '✅ 正常'}</td></tr>
                </table>
                <p><strong>⚠️ B站 API 接口可能异常，请检查！</strong></p>
                <p>不影响置顶评论监控（走Playwright浏览器），但新动态检测将失效。</p>
            </div>
        </div>
        </body></html>"""

    def _generate_api_p2_alert_content(self, total, success, failure, success_rate):
        """生成API P2告警邮件HTML"""
        theme = "#F9A825"
        return f"""
        <!DOCTYPE html>
        <html>
        <head><meta charset="UTF-8">
        <style>
        body {{ font-family:'Microsoft YaHei',Arial; background:#f5f5f5; padding:20px; }}
        .card {{ max-width:650px; margin:auto; background:#fff; border-radius:10px; box-shadow:0 4px 12px rgba(0,0,0,0.1); overflow:hidden; }}
        .header {{ background:linear-gradient(135deg,{theme},#F57F17); color:white; padding:20px; text-align:center; }}
        .content {{ padding:24px; }}
        .stat {{ background:#fffde7; padding:12px; border-radius:6px; margin-bottom:10px; }}
        table {{ width:100%; border-collapse:collapse; margin-top:15px; }}
        th,td {{ border:1px solid #ddd; padding:8px; text-align:left; }}
        th {{ background:#FFF9C4; }}
        </style></head>
        <body>
        <div class="card">
            <div class="header"><h2>⚠️ API 性能告警</h2><p>B站 API 成功率低于阈值</p></div>
            <div class="content">
                <div class="stat"><strong>总请求次数：</strong>{total}</div>
                <div class="stat"><strong>成功：</strong>{success} | 失败：{failure}</div>
                <div class="stat"><strong>成功率：</strong>{success_rate:.2%}</div>
                <table>
                    <tr><th>类型</th><th>阈值</th><th>当前</th><th>状态</th></tr>
                    <tr><td>P1连续失败</td><td>{API_P1_FAILURE_THRESHOLD}</td><td>{self.api_consecutive_failures}</td><td>{'🚨 已触发' if self.api_p1_alert_sent else '✅ 正常'}</td></tr>
                    <tr><td>P2成功率</td><td>{API_P2_SUCCESS_RATE_THRESHOLD:.0%}</td><td>{success_rate:.2%}</td><td>{'⚠️ 已触发' if self.api_p2_alert_sent else '✅ 正常'}</td></tr>
                </table>
                <h4>建议排查</h4>
                <ul>
                    <li>B站 API 是否限流</li>
                    <li>Cookie 是否过期</li>
                    <li>接口域名是否变更</li>
                </ul>
            </div>
        </div>
        </body></html>"""

    async def _send_report(self, total_cycles):
        """发送综合性能报告（含置顶评论 + API 双通道统计）"""
        subject = f"📊 ttkj-monitor性能报告 - 第{total_cycles}轮"
        content = self._generate_report_content(total_cycles)
        logger.info(f"📤 正在发送性能报告邮件: {subject}")
        success = await asyncio.to_thread(send_email, subject=subject, content=content, to_emails=STATUS_MONITOR_EMAILS)
        if success:
            logger.info("✅ 性能报告邮件发送成功")
            self.report_sent = False
        else:
            logger.error("❌ 性能报告邮件发送失败")

    # ----------------- 邮件内容生成函数 -----------------

    def _generate_p1_alert_content(self, total_cycles, failure_count):
        success = self.cumulative_success
        success_rate = success / total_cycles if total_cycles > 0 else 0
        recent_failures = [r['timestamp'].strftime('%H:%M:%S') for r in reversed(self.cycle_durations) if not r['success']][:5]
        avg_duration = sum(r['duration'] for r in self.cycle_durations) / len(self.cycle_durations) if self.cycle_durations else 0
        recent_avg = sum(r['duration'] for r in self.cycle_durations[-10:]) / min(len(self.cycle_durations), 10) if self.cycle_durations else 0
        theme = "#E65100"

        return f"""
        <!DOCTYPE html>
        <html>
        <head>
        <meta charset="UTF-8">
        <style>
        body {{ font-family:'Microsoft YaHei', Arial; background:#f5f5f5; padding:20px; }}
        .card {{ max-width:700px; margin:auto; background:#fff; border-radius:10px; box-shadow:0 4px 12px rgba(0,0,0,0.12); overflow:hidden; }}
        .header {{ background:linear-gradient(135deg,{theme},#BF360C); color:white; padding:20px; text-align:center; }}
        .content {{ padding:24px; }}
        .stat {{ background:#fff3e0; padding:12px; border-radius:6px; margin-bottom:12px; }}
        table {{ width:100%; border-collapse:collapse; margin-top:15px; }}
        th, td {{ border:1px solid #ddd; padding:8px; text-align:left; }}
        th {{ background:#FFE0B2; }}
        ul {{ padding-left:18px; }}
        </style>
        </head>
        <body>
            <div class="card">
                <div class="header">
                    <h2>🚨 P1 严重告警</h2>
                    <p>累计失败次数超出安全阈值</p>
                </div>
                <div class="content">
                    <div class="stat"><strong>失败次数：</strong>{failure_count}</div>
                    <div class="stat"><strong>当前轮次：</strong>{total_cycles}</div>
                    <div class="stat"><strong>成功率：</strong>{success_rate:.1%}</div>
                    <div class="stat"><strong>平均耗时：</strong>{avg_duration:.1f}s, 最近10轮平均：{recent_avg:.1f}s</div>

                    <h4>最近失败时间</h4>
                    <ul>
                        {''.join(f'<li>{t}</li>' for t in recent_failures)}
                    </ul>

                    <h4>告警状态</h4>
                    <table>
                        <tr><th>类型</th><th>阈值</th><th>当前</th><th>状态</th></tr>
                        <tr><td>P1累计失败</td><td>{P1_TOTAL_FAILURE_THRESHOLD}</td><td>{failure_count}</td><td>{'🚨 已触发' if self.p1_alert_sent else '✅ 正常'}</td></tr>
                        <tr><td>P2成功率</td><td>{P2_SUCCESS_RATE_THRESHOLD:.0%}</td><td>{success_rate:.2%}</td><td>{'⚠️ 已触发' if self.p2_alert_sent else '✅ 正常'}</td></tr>
                    </table>

                    <p><strong>⚠️ 请立即检查系统运行状态！</strong></p>
                </div>
            </div>
        </body>
        </html>
        """

    def _generate_p2_alert_content(self, total_cycles, success_rate):
        success = self.cumulative_success
        failure = self.cumulative_failure
        recent = self.cycle_durations[-10:] if len(self.cycle_durations) >= 10 else self.cycle_durations
        recent_success = sum(1 for r in recent if r['success'])
        recent_rate = recent_success / len(recent) if recent else 0
        avg_duration = sum(r['duration'] for r in self.cycle_durations) / len(self.cycle_durations) if self.cycle_durations else 0
        recent_avg = sum(r['duration'] for r in recent) / len(recent) if recent else 0
        theme = "#F9A825"

        return f"""
        <!DOCTYPE html>
        <html>
        <head>
        <meta charset="UTF-8">
        <style>
        body {{ font-family:'Microsoft YaHei', Arial; background:#f5f5f5; padding:20px; }}
        .card {{ max-width:700px; margin:auto; background:#fff; border-radius:10px; box-shadow:0 4px 12px rgba(0,0,0,0.1); overflow:hidden; }}
        .header {{ background:linear-gradient(135deg,{theme},#F57F17); color:white; padding:20px; text-align:center; }}
        .content {{ padding:24px; }}
        .stat {{ background:#fffde7; padding:12px; border-radius:6px; margin-bottom:10px; }}
        table {{ width:100%; border-collapse:collapse; margin-top:15px; }}
        th, td {{ border:1px solid #ddd; padding:8px; text-align:left; }}
        th {{ background:#FFF9C4; }}
        </style>
        </head>
        <body>
            <div class="card">
                <div class="header">
                    <h2>⚠️ P2 性能告警</h2>
                    <p>成功率低于预期阈值</p>
                </div>
                <div class="content">
                    <div class="stat"><strong>总体成功率：</strong>{success_rate:.2%}</div>
                    <div class="stat"><strong>最近10轮成功率：</strong>{recent_rate:.2%}</div>
                    <div class="stat"><strong>失败轮次：</strong>{failure}</div>
                    <div class="stat"><strong>平均耗时：</strong>{avg_duration:.1f}s, 最近10轮平均：{recent_avg:.1f}s</div>

                    <h4>告警状态</h4>
                    <table>
                        <tr><th>类型</th><th>阈值</th><th>当前</th><th>状态</th></tr>
                        <tr><td>P1累计失败</td><td>{P1_TOTAL_FAILURE_THRESHOLD}</td><td>{failure}</td><td>{'🚨 已触发' if self.p1_alert_sent else '✅ 正常'}</td></tr>
                        <tr><td>P2成功率</td><td>{P2_SUCCESS_RATE_THRESHOLD:.0%}</td><td>{success_rate:.2%}</td><td>{'⚠️ 已触发' if self.p2_alert_sent else '✅ 正常'}</td></tr>
                    </table>

                    <h4>建议排查项</h4>
                    <ul>
                        <li>Cookie 是否失效</li>
                        <li>网络波动</li>
                        <li>反爬策略变化</li>
                        <li>浏览器实例稳定性</li>
                    </ul>
                </div>
            </div>
        </body>
        </html>
        """

    def _generate_report_content(self, total_cycles):
        uptime_hours = (time.time() - self.start_time) / 3600
        success = self.cumulative_success
        failure = self.cumulative_failure
        success_rate = success / total_cycles if total_cycles > 0 else 0
        avg_duration = sum(r['duration'] for r in self.cycle_durations) / len(self.cycle_durations) if self.cycle_durations else 0
        recent = self.cycle_durations[-10:] if len(self.cycle_durations) >= 10 else self.cycle_durations
        recent_avg = sum(r['duration'] for r in recent) / len(recent) if recent else 0
        theme = "#00796B"

        # API 统计
        api_success_rate = self.api_success / self.api_total if self.api_total > 0 else 0

        return f"""
        <!DOCTYPE html>
        <html>
        <head>
        <meta charset="UTF-8">
        <style>
        body {{ font-family:'Microsoft YaHei', Arial; background:#f5f5f5; padding:20px; }}
        .card {{ max-width:750px; margin:auto; background:#fff; border-radius:10px; box-shadow:0 4px 12px rgba(0,0,0,0.1); overflow:hidden; }}
        .header {{ background:linear-gradient(135deg,{theme},#004D40); color:white; padding:20px; text-align:center; }}
        .section-title {{ background:#E0F2F1; color:#00695C; padding:8px 12px; margin-top:20px; border-radius:4px; font-weight:bold; }}
        table {{ width:100%; border-collapse:collapse; margin-top:10px; }}
        th, td {{ border:1px solid #ddd; padding:10px; text-align:left; }}
        th {{ background:#B2DFDB; }}
        </style>
        </head>
        <body>
            <div class="card">
                <div class="header">
                    <h2>📊 性能运行报告 - 第{total_cycles}轮</h2>
                    <p>系统运行时间: {uptime_hours:.1f} 小时</p>
                </div>
                <div class="content" style="padding:24px;">
                    <div class="section-title">🖥️ 置顶评论监控 (Playwright)</div>
                    <table>
                        <tr><th>指标</th><th>数值</th></tr>
                        <tr><td>总轮次数</td><td>{total_cycles}</td></tr>
                        <tr><td>成功轮次</td><td>{success}</td></tr>
                        <tr><td>失败轮次</td><td>{failure}</td></tr>
                        <tr><td>成功率</td><td>{success_rate:.2%}</td></tr>
                        <tr><td>平均耗时</td><td>{avg_duration:.1f}s</td></tr>
                        <tr><td>最近10轮平均耗时</td><td>{recent_avg:.1f}s</td></tr>
                        <tr><td>运行频率</td><td>{total_cycles / uptime_hours:.1f} 轮/小时</td></tr>
                        <tr><td>P1告警状态</td><td>{'🚨 已触发' if self.p1_alert_sent else '✅ 正常'}</td></tr>
                        <tr><td>P2告警状态</td><td>{'⚠️ 已触发' if self.p2_alert_sent else '✅ 正常'}</td></tr>
                    </table>

                    <div class="section-title">📡 API 动态列表 (urllib)</div>
                    <table>
                        <tr><th>指标</th><th>数值</th></tr>
                        <tr><td>API请求次数</td><td>{self.api_total}</td></tr>
                        <tr><td>API成功次数</td><td>{self.api_success}</td></tr>
                        <tr><td>API失败次数</td><td>{self.api_failure}</td></tr>
                        <tr><td>API成功率</td><td>{api_success_rate:.2%}</td></tr>
                        <tr><td>API连续失败</td><td>{self.api_consecutive_failures}</td></tr>
                        <tr><td>API P1告警状态</td><td>{'🚨 已触发' if self.api_p1_alert_sent else '✅ 正常'}</td></tr>
                        <tr><td>API P2告警状态</td><td>{'⚠️ 已触发' if self.api_p2_alert_sent else '✅ 正常'}</td></tr>
                    </table>

                    <p><em>报告间隔: 每 {PERFORMANCE_REPORT_CYCLE_INTERVAL} 轮发送一次</em></p>
                </div>
            </div>
        </body>
        </html>
        """

    async def periodic_report(self, interval_minutes=60):
        while True:
            try:
                await asyncio.sleep(interval_minutes * 60)
                memory_mb = psutil.Process().memory_info().rss / 1024 / 1024
                uptime_hours = (time.time() - self.start_time) / 3600
                total = self.total_cycles
                success_rate = self.cumulative_success / total if total > 0 else 0
                logger.info(
                    f"📊 定期性能摘要: 运行{uptime_hours:.1f}小时, 轮次{total}, "
                    f"成功率{success_rate:.1%}, 失败{self.cumulative_failure}次, "
                    f"内存{memory_mb:.1f}MB, P1状态={'🚨' if self.p1_alert_sent else '✅'}, P2状态={'⚠️' if self.p2_alert_sent else '✅'}")
            except Exception as e:
                logger.error(f"❌ 定期报告失败: {e}")


# ----------------- 全局实例 -----------------
performance_monitor = PerformanceMonitor()
