# qbot_errors.py
# 官方错误码分类和异常类型；只放纯逻辑，不发网络请求。
from typing import Optional, Tuple

# ===== 错误码分类 =====
# 依据 QQ 官方文档“发送群聊消息 - 错误码”整理。
# 不可重试：权限/群状态/内容本身有问题，重试只会重复撞墙。
NON_RETRYABLE_CODES = {
    40034101,  # 机器人非群成员
    40034105,  # 主动消息发送失败，无权限（群/用户关闭主动发送）
    40034106,  # 消息不支持该指令类型
    40034127,  # 无 markdown 模板权限
    40034128,  # 被动回复时间或次数超限
    40054002,  # 机器人被禁言
    40054003,  # 机器人不是群成员
    40054007,  # 消息长度超限
    40054010,  # 不允许发送 URL
    40034124,  # markdown 参数错误
    40034008,  # markdown 参数有空值
    40034009,  # markdown 参数有换行符
    40034010,  # 模板参数含 markdown 语法
    40034011,  # 无效的 markdown 内容
    40011021,  # 卡片参数无效（title/description/pic_url 等）
    40011028,  # 请求的资源不存在（用户/群已注销）
    304004,    # 无权限使用 ARK 模板
    304036,    # 无 Markdown 模板权限
}

# 可重试：网络/服务端/频控/媒体转存/去重。
# 40054005 比较特殊：换一个 msg_seq 再试一次即可。
RETRYABLE_CODES = {
    40034100,  # 主动消息超过频控
    40054005,  # 消息被去重
    40034004,  # 富媒体信息转存失败
    50055001,  # 消息发送异常
    50055006,  # ARK 消息发送异常
}

# 媒体上传失败后允许“URL 上传失败 -> 下载后二进制上传”回退。
MEDIA_FALLBACK_CODES = {304080, 304082, 304083, 40034004}


def classify_error(
    code: Optional[int],
    message: str,
    http_status: Optional[int] = None,
) -> Tuple[bool, str]:
    """
    作用：把官方错误码/HTTP 状态归类成“是否可重试 + 中文原因”。
    入参：code QQ 业务错误码；message 原始错误信息；http_status HTTP 状态码。
    出参：(retryable, reason)。
    """
    # botpy 在 HTTP 500 时只保留 message，会丢掉业务 code；
    # 先用原始文案兜底识别“权限/群状态”这类不可重试错误。
    text = message or ""
    if "主动消息失败" in text or ("主动消息" in text and "无权限" in text):
        return False, "主动消息无权限（群/用户关闭主动发送，或机器人未获主动消息权限）"
    if "IP不在白名单" in text or "IP 不在白名单" in text:
        return False, "接口访问源 IP 不在白名单"
    if "机器人非群成员" in text or "机器人不是群成员" in text or "不是群成员" in text:
        return False, "机器人不是群成员"
    if "被禁言" in text:
        return False, "机器人被禁言"
    if "消息被去重" in text:
        return True, "消息被去重，换 msg_seq 重试"
    if "不允许发送URL" in text or "不允许发送 URL" in text:
        return False, "群消息不允许发送 URL"
    if "长度超限" in text:
        return False, "消息长度超限"
    if "被动回复" in text and ("超" in text or "过期" in text):
        return False, "被动回复超时或次数超限"

    if http_status in (401, 403):
        return False, "鉴权失败或 IP 白名单未通过"
    if http_status == 404:
        return False, "接口或群不存在"
    if http_status == 405:
        return False, "接口方法不允许"
    if http_status == 429:
        return True, "HTTP 429 发送超频"
    if http_status is not None and http_status >= 500:
        return True, "官方服务端错误"

    if code in NON_RETRYABLE_CODES:
        reason_map = {
            40034101: "机器人不在群内",
            40034105: "主动消息无权限，群或用户在客户端关闭了主动发送",
            40034106: "消息不支持该指令类型",
            40034127: "没有 markdown 模板权限",
            40034128: "被动回复超时或次数超限",
            40054002: "机器人被禁言",
            40054003: "机器人不是群成员",
            40054007: "消息长度超限",
            40054010: "群消息不允许发送 URL",
        }
        return False, reason_map.get(code, "官方返回不可重试错误")

    if code in RETRYABLE_CODES:
        reason_map = {
            40034100: "主动消息超过频控限制",
            40054005: "消息被去重，换 msg_seq 重试",
            40034004: "富媒体转存失败",
            50055001: "官方发送异常",
            50055006: "ARK 消息发送异常",
        }
        return True, reason_map.get(code, "官方返回可重试错误")

    if code is None:
        return True, "网络无返回或未知业务错误"
    return True, "未知错误码 " + str(code) + "，按可重试处理"


class QBotAPIError(Exception):
    """官方 API 调用失败；retryable 决定队列是否重试。"""

    def __init__(self, code: Optional[int], message: str, retryable: bool):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


