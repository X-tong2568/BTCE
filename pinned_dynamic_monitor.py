# pinned_dynamic_monitor.py
# 通过 API 自动发现置顶动态，检测置顶是否更换
# 纯检测模块：不发送邮件、不截图，只返回变更信息，由调用方（monitor.py）处理通知
import hashlib
import json
import threading
import time
import asyncio
from pathlib import Path
from typing import Optional

from config import UP_UID, UP_NAME, COOKIE_FILE
from logger_config import logger

STATE_FILE = Path(__file__).parent / "pinned_dynamic_state.json"

# 置顶消失后的复核节奏：每 60 秒一次，最多 3 次；3 次成功为空才确认取消。
CONFIRM_RECHECK_SECONDS = 60
CONFIRM_RECHECK_ATTEMPTS = 3
_confirm_task: Optional[asyncio.Task] = None

# 归档仓里的置顶游标：EchoWatch 据此判断“哪条评论是当前置顶”
CURSOR_PATH = "_state/pinned_cursor.json"
_cursor_cache: Optional[dict] = None
_cursor_lock = threading.Lock()
_cursor_last_fail = 0.0


def _cursor_stable_digest(payload: dict) -> str:
    """只对语义字段做摘要；updated_at 每次都变，不能参与去重判断"""
    stable = {k: v for k, v in payload.items() if k != "updated_at"}
    raw = json.dumps(stable, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _load_cursor_locked() -> dict:
    """首次调用时读远端游标；读失败按空游标处理，不抛异常"""
    global _cursor_cache
    if _cursor_cache is not None:
        return _cursor_cache
    try:
        from archive_utils import read_archive_text
        raw = read_archive_text(CURSOR_PATH)
        _cursor_cache = json.loads(raw) if raw else {}
    except Exception as e:
        logger.warning(f"置顶游标读取失败，按空游标处理: {e}")
        _cursor_cache = {}
    return _cursor_cache


def write_pinned_cursor(dynamic_id: Optional[str], rpid: Optional[str] = None,
                        reason: str = "") -> bool:
    """
    写归档仓 _state/pinned_cursor.json（独立 commit）。

    规则：
    - 只在 pinned_dynamic_id / pinned_rpid 真正变化时写，主循环 6 秒一轮不会刷盘
    - 置顶动态更换时，旧 rpid 进 prev_rpid/prev_since
    - 新动态第一次拿到 rpid 时，保留上一任置顶的 prev 信息
    - 任何异常只记日志，绝不影响推送主链路

    返回 True 表示有一次成功写入，False 表示无变化/未启用/失败。
    """
    global _cursor_cache, _cursor_last_fail

    try:
        from archive_utils import is_archive_enabled, write_archive_text
        if not is_archive_enabled():
            return False
    except Exception:
        return False

    with _cursor_lock:
        old = _load_cursor_locked()
        old_dyn = str(old.get("pinned_dynamic_id") or "")
        old_rpid = str(old.get("pinned_rpid") or "")
        old_since = str(old.get("since") or "")
        new_dyn = str(dynamic_id or "")
        rpid_known = rpid is not None
        new_rpid = str(rpid or "")

        # rpid=None 表示“还不知道”，不能当成“置顶评论被删了”
        if new_dyn == old_dyn:
            if not rpid_known or new_rpid == old_rpid:
                return False

        now = time.strftime("%Y-%m-%d %H:%M:%S")
        if new_dyn != old_dyn:
            # 动态换了：旧评论整体退到 prev
            prev_rpid = old_rpid
            prev_since = old_since
            pinned_rpid = new_rpid
            since = now
        elif old_rpid == "" and new_rpid:
            # 同一动态第一次拿到 rpid：保留上一任置顶的 prev 信息
            prev_rpid = str(old.get("prev_rpid") or "")
            prev_since = str(old.get("prev_since") or "")
            pinned_rpid = new_rpid
            since = old_since or now
        else:
            # 同一动态置顶评论被替换，或被清空（rpid=""）
            prev_rpid = old_rpid
            prev_since = old_since
            pinned_rpid = new_rpid
            since = now

        payload = {
            "pinned_dynamic_id": new_dyn,
            "pinned_rpid": pinned_rpid,
            "since": since,
            "prev_rpid": prev_rpid,
            "prev_since": prev_since,
            "written_by": "btce-bot",
            "updated_at": now,
        }
        digest = _cursor_stable_digest(payload)
        if old.get("_stable") == digest:
            return False

        # 写失败后 60 秒内不重试，避免 6 秒主循环反复打 GitHub
        if _cursor_last_fail and time.time() - _cursor_last_fail < 60:
            return False

        message = f"btce: pinned cursor → {pinned_rpid or '-'} (dyn {new_dyn or '-'})"
        try:
            ok = write_archive_text(
                CURSOR_PATH,
                json.dumps(payload, ensure_ascii=False, indent=2),
                message,
            )
        except Exception as e:
            logger.warning(f"置顶游标写入异常（不影响推送）: {e}")
            ok = False

        if not ok:
            _cursor_last_fail = time.time()
            logger.warning(f"置顶游标写入失败，60 秒后重试: {message}")
            return False

        payload["_stable"] = digest
        _cursor_cache = payload
        _cursor_last_fail = 0.0
        logger.info(f"置顶游标已更新（{reason or 'change'}）: {message}")
        return True


def _load_state() -> dict:
    """加载本地状态文件"""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_state(state: dict):
    """保存状态到本地"""
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _find_pinned(raw_items: list[dict]) -> Optional[dict]:
    """从 API 原始 items 列表中找出置顶动态（检查 modules.module_tag.text）"""
    for item in raw_items:
        modules = item.get("modules", {})
        tag = modules.get("module_tag") or {}
        if tag.get("text") == "置顶":
            return item
    return None


def _now() -> str:
    """统一事件时间格式。"""
    return time.strftime("%Y-%m-%d %H:%M:%S")


async def _fetch_items(uid: str):
    """拉一次动态列表，供检查与后台复核共用。"""
    from bili_api import BiliAPI

    bili = BiliAPI(COOKIE_FILE)
    try:
        items, ok = await bili._fetch_raw(uid)
    finally:
        await bili.close()
    return items, ok


def _pinned_info(pinned: Optional[dict]) -> dict:
    """提取写进 state 的置顶摘要。"""
    if not pinned:
        return {}
    modules = pinned.get("modules") or {}
    desc = (modules.get("module_dynamic") or {}).get("desc")
    return {
        "dynamic_id": pinned.get("id_str", ""),
        "type": pinned.get("type", ""),
        "pub_time": (modules.get("module_author") or {}).get("pub_time", ""),
        "text": (desc.get("text", "") if desc else "")[:100],
    }


async def _apply_change(
    monitored_id: str,
    api_id: str,
    pinned: dict,
    reason: str,
) -> dict:
    """写入新的监控目标并返回事件；取消状态也随之解除。"""
    now = _now()
    state = _load_state()
    state["pinned_dynamic_id"] = api_id
    state["pin_status"] = "ok"
    state["pin_cancelled_at"] = ""
    state["last_change"] = now
    state["last_pinned_info"] = _pinned_info(pinned)
    _save_state(state)
    # 只写动态 ID，rpid 等 monitor.check_dynamic_changes 抓到后再补
    await asyncio.to_thread(write_pinned_cursor, api_id, None, reason)
    logger.info("置顶监测: 监控目标 %s -> %s (%s)", monitored_id or "-", api_id, reason)
    return {
        "event": "changed" if monitored_id else "first_seen",
        "current_id": api_id,
        "previous_id": monitored_id or None,
        "time": now,
        "reason": reason,
    }


def _monitor_target_unchanged(monitored_id: str) -> bool:
    """复核期间确认没人手动改过监控目标或切到手动模式。"""
    state = _load_state()
    if state.get("mode") == "manual":
        return False
    return str(state.get("pinned_dynamic_id") or "") == str(monitored_id)


def _schedule_confirmation(uid: str, monitored_id: str, event_sink) -> bool:
    """启动后台复核任务；已有任务在跑时不重复发起。"""
    global _confirm_task
    if _confirm_task is not None and not _confirm_task.done():
        logger.info("置顶复核任务已在运行，跳过重复调度")
        return True
    try:
        _confirm_task = asyncio.create_task(
            _confirm_pin_missing(uid, monitored_id, event_sink)
        )
    except RuntimeError as e:
        logger.warning("置顶复核任务启动失败: %s", e)
        return False
    return True


async def _confirm_pin_missing(uid: str, monitored_id: str, event_sink) -> None:
    """
    作用：API 首次没返回置顶后，每 60 秒复核一次，最多 3 次。
    说明：复核期间原 ID 回来视为 API 抖动，静默；3 次成功为空才确认取消。
          取消后仍保留 monitored_id，BTCE 继续监控原动态。
    """
    global _confirm_task
    empty_count = 0
    fail_count = 0
    try:
        for _ in range(CONFIRM_RECHECK_ATTEMPTS):
            await asyncio.sleep(CONFIRM_RECHECK_SECONDS)
            if not _monitor_target_unchanged(monitored_id):
                logger.info("置顶复核: 监控目标已变化，放弃本轮复核")
                return
            items, ok = await _fetch_items(uid)
            if not ok:
                fail_count += 1
                continue
            pinned = _find_pinned(items)
            api_id = pinned.get("id_str") if pinned else None
            if api_id is None:
                empty_count += 1
                continue
            if str(api_id) == str(monitored_id):
                logger.info("置顶复核: API 抖动，原置顶仍在 (%s)，本轮静默", api_id)
                return
            # 复核期间直接换成了另一条动态
            event = await _apply_change(monitored_id, api_id, pinned, "recheck_change")
            if event_sink:
                event_sink(event)
            return

        if empty_count < CONFIRM_RECHECK_ATTEMPTS:
            logger.warning(
                "置顶复核: 成功为空 %s 次、请求失败 %s 次，未达到 %s 次空结果，不判取消",
                empty_count,
                fail_count,
                CONFIRM_RECHECK_ATTEMPTS,
            )
            return
        if not _monitor_target_unchanged(monitored_id):
            logger.info("置顶复核: 监控目标已变化，放弃确认取消")
            return

        state = _load_state()
        cancelled_at = _now()
        state["pin_status"] = "cancelled"
        state["pin_cancelled_at"] = cancelled_at
        state["last_change"] = cancelled_at
        _save_state(state)
        logger.info(
            "置顶复核: 连续 %s 次为空，确认取消置顶；继续监控原动态 %s",
            empty_count,
            monitored_id,
        )
        if event_sink:
            event_sink(
                {
                    "event": "cancelled",
                    "current_id": monitored_id,
                    "previous_id": monitored_id,
                    "time": cancelled_at,
                    "reason": "pinned_cancelled",
                }
            )
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning("置顶复核异常，保持原状: %s", e)
    finally:
        _confirm_task = None


async def check_pinned_dynamic(uid: str = None, event_sink=None) -> dict:
    """
    通过 API 发现当前置顶动态，检测是否更换。
    只检测和记录状态，不发送邮件；后台复核事件通过 event_sink 回传。

    Args:
        uid: B站 UID，默认用 config.UP_UID
        event_sink: 可选同步回调，接收后台复核产生的 cancelled / changed 事件
    Returns:
        {"changed": bool, "event": str|None, "current_id": str|None,
         "previous_id": str|None, "is_new": bool, "api_id": str|None,
         "auto_restored": bool, "pending_confirm": bool}
    """
    uid = uid or UP_UID

    result = {
        "changed": False,
        "event": None,
        "current_id": None,
        "previous_id": None,
        "is_new": False,
        "api_id": None,
        "auto_restored": False,
        "pending_confirm": False,
    }

    if not COOKIE_FILE.exists():
        logger.warning("置顶监测: Cookie 文件不存在，跳过")
        return result

    raw_items, ok = await _fetch_items(uid)
    if not ok:
        logger.warning("置顶监测: API 请求失败，跳过本轮")
        return result

    pinned = _find_pinned(raw_items)
    api_pinned_id = pinned.get("id_str") if pinned else None
    state = _load_state()
    monitored_id = str(state.get("pinned_dynamic_id") or "")  # 系统正在监控的ID
    pin_status = str(state.get("pin_status") or "ok")

    result["api_id"] = api_pinned_id
    result["current_id"] = monitored_id or api_pinned_id
    result["previous_id"] = monitored_id or None

    # 手动模式：API照查，但不自动切换（除非检测到手动设定已与B站一致）
    if state.get("mode") == "manual":
        if api_pinned_id and monitored_id and api_pinned_id == monitored_id:
            # 手动设定的ID与B站API实际置顶一致 → 用户操作已生效，自动切回自动模式
            set_mode_auto()
            result["auto_restored"] = True
            logger.info(f"置顶监测: 手动模式，B站置顶与手动设定一致({api_pinned_id})，自动切回自动模式")
        elif api_pinned_id and api_pinned_id != monitored_id:
            logger.info(f"置顶监测: 手动模式，B站置顶={api_pinned_id}，监控中={monitored_id}，不自动切换")
        else:
            logger.info(f"置顶监测: 手动模式，B站置顶={api_pinned_id} (一致)")
        return result

    # 自动模式 -------------------------------------------------
    # API 没返回置顶：不立即改状态，先后台复核三次
    if api_pinned_id is None:
        if not monitored_id:
            return result
        if pin_status == "cancelled":
            logger.info("置顶监测: 仍处于取消置顶状态，继续监控原动态 %s", monitored_id)
            return result
        result["pending_confirm"] = _schedule_confirmation(uid, monitored_id, event_sink)
        if result["pending_confirm"]:
            logger.info(
                "置顶监测: API 未返回置顶，%s 秒后开始复核（最多 %s 次）",
                CONFIRM_RECHECK_SECONDS,
                CONFIRM_RECHECK_ATTEMPTS,
            )
        return result

    # 之前处于取消状态：判断是恢复还是换成了另一条
    if pin_status == "cancelled":
        if str(api_pinned_id) == monitored_id:
            now = _now()
            state["pin_status"] = "ok"
            state["pin_cancelled_at"] = ""
            state["last_change"] = now
            state["last_pinned_info"] = _pinned_info(pinned)
            _save_state(state)
            logger.info("置顶监测: 置顶已恢复 -> %s", api_pinned_id)
            result["changed"] = True
            result["event"] = "recovered"
            result["current_id"] = api_pinned_id
            return result
        await _apply_change(monitored_id, api_pinned_id, pinned, "recovered_as_change")
        result["changed"] = True
        result["event"] = "changed"
        result["current_id"] = api_pinned_id
        return result

    # 首次发现
    if not monitored_id:
        await _apply_change(monitored_id, api_pinned_id, pinned, "first_seen")
        result["changed"] = True
        result["event"] = "first_seen"
        result["is_new"] = True
        result["current_id"] = api_pinned_id
        return result

    # 无变化
    if str(api_pinned_id) == monitored_id:
        if pin_status != "ok":
            state["pin_status"] = "ok"
            _save_state(state)
        return result

    # 自动更换
    await _apply_change(monitored_id, api_pinned_id, pinned, "auto_change")
    result["changed"] = True
    result["event"] = "changed"
    result["current_id"] = api_pinned_id
    return result


def set_mode_manual(dynamic_id: str = None):
    """
    切换到手动模式，可同时指定置顶ID。
    API 自动检测暂停，监控目标保持不变。
    """
    state = _load_state()
    state["mode"] = "manual"
    if dynamic_id:
        state["pinned_dynamic_id"] = dynamic_id
    state["last_change"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _save_state(state)
    current = state.get("pinned_dynamic_id", "?")
    logger.info(f"置顶监测: 已切换为手动模式 (当前ID={current})")


def set_mode_auto():
    """
    切换到自动模式，API 检测恢复，下次循环自动同步 B站置顶。
    """
    state = _load_state()
    state["mode"] = "auto"
    state["last_change"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _save_state(state)
    logger.info(f"置顶监测: 已切换为自动模式，下次API检测将同步B站置顶")


def get_mode() -> str:
    """返回当前模式 'auto' 或 'manual'"""
    state = _load_state()
    return state.get("mode", "auto")


# ---- 独立运行入口 ----
if __name__ == "__main__":
    async def main():
        print(f"pinned check - {UP_NAME} (UID={UP_UID})")
        result = await check_pinned_dynamic()
        if result["changed"]:
            print(f"Changed! new={result['current_id']} old={result['previous_id']} is_new={result['is_new']}")
        else:
            print(f"No change, current={result['current_id']}")

    asyncio.run(main())
