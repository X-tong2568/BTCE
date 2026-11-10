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


async def check_pinned_dynamic(uid: str = None) -> dict:
    """
    通过 API 发现当前置顶动态，检测是否更换。
    只检测和记录状态，不发送邮件。

    Args:
        uid: B站 UID，默认用 config.UP_UID
    Returns:
        {"changed": bool, "current_id": str|None, "previous_id": str|None,
         "is_new": bool, "api_id": str|None, "auto_restored": bool}
    """
    from bili_api import BiliAPI

    uid = uid or UP_UID
    cookie_file = COOKIE_FILE

    result = {"changed": False, "current_id": None, "previous_id": None, "is_new": False, "api_id": None, "auto_restored": False}

    if not cookie_file.exists():
        logger.warning("置顶监测: Cookie 文件不存在，跳过")
        return result

    bili = BiliAPI(cookie_file)
    raw_items, ok = await bili._fetch_raw(uid)
    await bili.close()

    if not ok:
        logger.warning("置顶监测: API 请求失败，跳过本轮")
        return result

    pinned = _find_pinned(raw_items)
    api_pinned_id = pinned.get("id_str") if pinned else None
    state = _load_state()
    monitored_id = state.get("pinned_dynamic_id")  # 系统正在监控的ID

    result["api_id"] = api_pinned_id
    result["current_id"] = monitored_id or api_pinned_id
    result["previous_id"] = monitored_id

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
    if api_pinned_id is None:
        if monitored_id:
            logger.info(f"置顶监测: 置顶动态已消失 (上次: {monitored_id})")
            state["pinned_dynamic_id"] = None
            state["last_change"] = time.strftime("%Y-%m-%d %H:%M:%S")
            _save_state(state)
            await asyncio.to_thread(write_pinned_cursor, None, None, "pinned_disappeared")
            result["changed"] = True
        return result

    # 无变化
    if api_pinned_id == monitored_id:
        return result

    # 有变化（首次发现 或 自动更换）
    is_new = (monitored_id is None)
    logger.info(f"置顶监测: {'首次发现' if is_new else '已更换'} 置顶动态 -> {api_pinned_id}")

    # 更新状态文件
    state["pinned_dynamic_id"] = api_pinned_id
    state["last_change"] = time.strftime("%Y-%m-%d %H:%M:%S")
    mod_dyn = (pinned.get("modules", {}).get("module_dynamic") or {})
    desc = mod_dyn.get("desc")
    state["last_pinned_info"] = {
        "dynamic_id": api_pinned_id,
        "type": pinned.get("type", ""),
        "pub_time": (pinned.get("modules", {}).get("module_author", {}).get("pub_time", "")),
        "text": (desc.get("text", "") if desc else "")[:100],
    }
    _save_state(state)
    # 只写动态 ID，rpid 等 monitor.check_dynamic_changes 抓到后再补
    await asyncio.to_thread(write_pinned_cursor, api_pinned_id, None, "auto_change")

    result["changed"] = True
    result["is_new"] = is_new
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
