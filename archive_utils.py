# archive_utils.py
"""
通知归档模块

作用：
    把各类通知（置顶评论、开播/下播/标题修改、新动态）
    整理成可搜索的 Markdown + CSV，并把正文图片一起提交到 GitHub 私有仓库。

用法：
    archive_event(
        event_type="pinned_comment",
        content_html=cur_html,
        image_urls=cur_img,
        screenshot_path=screenshot_path,
        source_url="https://t.bilibili.com/123",
        event_time=ct,
        event_key=make_event_key("pinned_comment", dynamic_id, rpid, cur_html),
        extra={"dynamic_id": dynamic_id, "rpid": rpid},
        channels=["QQ", "邮件"],
    )

设计原则：
    1. 归档失败绝不影响通知推送：只做入队，后台线程处理
    2. 先落本地队列再提交，GitHub 失败可重试，不丢数据
    3. 文字是主数据（可搜索），图片是补充；截图只作视觉快照
    4. 一次事件 = 一次原子提交（MD + CSV + 图片）
"""

import base64
import csv
import hashlib
import io
import json
import os
import queue
import re
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

from logger_config import logger

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

# 原图超过 4000 万像素直接跳过，避免超长图解码时占满内存
MAX_SOURCE_PIXELS = 40_000_000
if PIL_AVAILABLE:
    Image.MAX_IMAGE_PIXELS = MAX_SOURCE_PIXELS * 2


# ===== 配置（优先读 config_github.py，没有就用安全默认值）=====
try:
    import config_github as _cfg
except ImportError:
    _cfg = None

GITHUB_ARCHIVE_ENABLED = bool(getattr(_cfg, "GITHUB_ARCHIVE_ENABLED", False))
GITHUB_ARCHIVE_OWNER = str(getattr(_cfg, "GITHUB_ARCHIVE_OWNER", ""))
GITHUB_ARCHIVE_REPO = str(getattr(_cfg, "GITHUB_ARCHIVE_REPO", ""))
GITHUB_ARCHIVE_BRANCH = str(getattr(_cfg, "GITHUB_ARCHIVE_BRANCH", "main"))
GITHUB_ARCHIVE_TOKEN = str(getattr(_cfg, "GITHUB_ARCHIVE_TOKEN", ""))
GITHUB_ARCHIVE_TIMEOUT = int(getattr(_cfg, "GITHUB_ARCHIVE_TIMEOUT", 20))
GITHUB_ARCHIVE_DRY_RUN = bool(getattr(_cfg, "GITHUB_ARCHIVE_DRY_RUN", False))
GITHUB_ARCHIVE_IMAGE_MAX_SIZE = int(getattr(_cfg, "GITHUB_ARCHIVE_IMAGE_MAX_SIZE", 1280))
GITHUB_ARCHIVE_WEBP_QUALITY = int(getattr(_cfg, "GITHUB_ARCHIVE_WEBP_QUALITY", 80))


# ===== 路径与常量 =====
_BASE_DIR = Path(__file__).parent
DATA_DIR = _BASE_DIR / "data"
ARCHIVE_QUEUE_DIR = DATA_DIR / "archive_queue"
EMOJI_CACHE_DIR = DATA_DIR / "archive_emoji_cache"
PREVIEW_DIR = DATA_DIR / "archive_preview"

_log = logger.getChild("archive")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

HEADER_START = "<!-- ARCHIVE_HEADER_START -->"
HEADER_END = "<!-- ARCHIVE_HEADER_END -->"
SECTIONS_START = "<!-- ARCHIVE_SECTIONS_START -->"
SECTIONS_END = "<!-- ARCHIVE_SECTIONS_END -->"
INDEX_START = "<!-- ARCHIVE_INDEX_START -->"
INDEX_END = "<!-- ARCHIVE_INDEX_END -->"

# README 索引的类型顺序
INDEX_TYPES = [
    ("pinned", "置顶评论"),
    ("live", "直播"),
    ("dynamics", "新动态"),
]

# 事件类型 -> 归档目录分组
GROUP_DIRS = {
    "pinned_comment": "pinned",
    "live_start": "live",
    "live_end": "live",
    "title_change": "live",
    "new_dynamic": "dynamics",
}

# 分组 -> 中文名（用于月度文件标题）
GROUP_NAMES = {
    "pinned": "置顶评论",
    "live": "直播",
    "dynamics": "新动态",
}

EVENT_TITLES = {
    "pinned_comment": "置顶评论更新",
    "live_start": "开播",
    "live_end": "下播",
    "title_change": "标题修改",
    "new_dynamic": "新动态",
}

CSV_FIELDS = [
    "event_key",
    "backup_time",
    "event_type",
    "event_title",
    "dynamic_id",
    "rpid",
    "summary",
    "content",
    "emojis",
    "image_count",
    "image_paths",
    "source_url",
    "channels",
    "extra_json",
]

# 空仓库第一次提交用（Git Data API 在空仓库上会返回 409）
ARCHIVE_README = """# BTCE 通知归档

本仓库由 BTCE 监控系统自动维护，按事件类型和月份归档通知。

## 归档索引（自动维护）

<!-- ARCHIVE_INDEX_START -->
<!-- ARCHIVE_INDEX_END -->

## 目录

- `pinned/`：置顶评论
- `live/`：开播、下播、标题修改
- `dynamics/`：新动态
- `assets/`：图片资源（表情、评论图、封面、截图）

每个月份对应一个 Markdown 文件和一个 CSV 文件：

- `.md`：GitHub 页面直接阅读，支持页面内搜索
- `.csv`：可用 Excel 筛选、统计

## 搜索

- 在 GitHub 页面上按 Ctrl+F 搜索当前月份
- 使用仓库搜索框搜索全部文字
- 本地 clone 后可用 `rg "关键词"` 搜索
"""

_EVENT_HEAD_RE = re.compile(
    r"<!-- event-key: .*? -->\s*\n"
    r"<!-- event-summary: (?P<summary>.*?) -->\s*\n"
    r"## (?P<time>[^\n]+?) · (?P<title>[^\n]+)\n"
)


# ===== 后台队列（单线程串行，避免两个事件同时改同一个月度文件）=====
_job_queue = queue.Queue()
_worker_started = False
_worker_lock = threading.Lock()
_inflight = set()
_inflight_lock = threading.Lock()
_demo_mode = False
_last_pending_log = 0.0


class _GithubError(Exception):
    """GitHub API 错误"""

    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(f"GitHub API {status}: {message}")

    @property
    def permanent(self) -> bool:
        """401/403/404 视为配置问题，重试间隔拉长"""
        return self.status in (401, 403, 404)


# ======================================================================
# 对外入口
# ======================================================================

def make_event_key(prefix: str, *parts: Any) -> str:
    """生成稳定的事件唯一键（重试去重用）"""
    raw = "|".join("" if p is None else str(p) for p in parts)
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    return f"{_sanitize_key(prefix)}_{digest}"


def archive_event(
    event_type: str,
    content_html: str = "",
    content_text: str = "",
    image_urls: Optional[List[str]] = None,
    screenshot_path: Optional[str] = None,
    source_url: str = "",
    event_time: str = "",
    event_key: str = "",
    extra: Optional[Dict[str, Any]] = None,
    channels: Optional[List[str]] = None,
    title: str = "",
) -> None:
    """
    归档一条通知（只入队，立即返回，不阻塞推送）

    参数：
        event_type: 事件类型，见 EVENT_TITLES
        content_html: 原始 HTML（置顶评论/新动态）
        content_text: 纯文本（告警/直播）
        image_urls: 正文图片 URL 列表
        screenshot_path: 本地截图路径（卡片/动态截图）
        source_url: 原帖/直播间链接
        event_time: 事件时间（不带时区会自动补 +0800）
        event_key: 去重键；不传则自动生成
        extra: 类型专属字段
        channels: 推送渠道展示
        title: 覆盖默认事件标题
    """
    if not _is_active():
        return

    try:
        job = _build_job(
            event_type=event_type,
            content_html=content_html,
            content_text=content_text,
            image_urls=image_urls,
            source_url=source_url,
            event_time=event_time,
            event_key=event_key,
            extra=extra,
            channels=channels,
            title=title,
        )
        job_dir = _persist_job(job, screenshot_path)
        _ensure_worker()
        _enqueue(job_dir)
        _log.info(f"归档已入队: {event_type} {job['event_key']}")
    except Exception as e:
        # 归档永远不能影响通知主流程
        _log.warning(f"归档入队失败（不影响通知）: {e}")


def is_archive_enabled() -> bool:
    """供通知侧判断：归档启用时需要卡片截图"""
    return _is_active()


def _is_active() -> bool:
    """是否启用归档（演示模式强制启用）"""
    return GITHUB_ARCHIVE_ENABLED or GITHUB_ARCHIVE_DRY_RUN or _demo_mode


def _build_job(
    event_type: str,
    content_html: str,
    content_text: str,
    image_urls: Optional[List[str]],
    source_url: str,
    event_time: str,
    event_key: str,
    extra: Optional[Dict[str, Any]],
    channels: Optional[List[str]],
    title: str,
) -> Dict[str, Any]:
    """组装任务字典"""
    backup_time = _with_tz(event_time)
    if not event_key:
        event_key = make_event_key(event_type, backup_time, content_html, content_text, source_url)

    # 图片 URL 去重保序
    clean_urls = []
    for u in image_urls or []:
        if not u:
            continue
        norm = _normalize_url(u)
        if norm and norm not in clean_urls:
            clean_urls.append(norm)

    return {
        "event_type": event_type,
        "title": title or EVENT_TITLES.get(event_type, event_type),
        "content_html": content_html or "",
        "content_text": content_text or "",
        "image_urls": clean_urls,
        "source_url": source_url or "",
        "backup_time": backup_time,
        "event_key": _sanitize_key(event_key),
        "extra": extra or {},
        "channels": channels or [],
        "attempt": 0,
        "next_retry": 0,
        "last_error": "",
        "created_at": time.time(),
    }


def _persist_job(job: Dict[str, Any], screenshot_path: Optional[str]) -> Path:
    """把任务写入本地队列目录，截图先复制进来（防源文件被清理）"""
    job_dir = ARCHIVE_QUEUE_DIR / job["event_key"]
    job_dir.mkdir(parents=True, exist_ok=True)

    if screenshot_path:
        src = Path(screenshot_path)
        if src.exists():
            suffix = src.suffix.lower() or ".png"
            dest = job_dir / ("card" + suffix)
            try:
                shutil.copy2(str(src), str(dest))
                job["screenshot_file"] = dest.name
            except Exception as e:
                _log.warning(f"截图复制失败: {e}")

    _write_json(job_dir / "job.json", job)
    return job_dir


# ======================================================================
# 后台 Worker
# ======================================================================

def _ensure_worker() -> None:
    """懒启动后台归档线程"""
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        _worker_started = True
    t = threading.Thread(target=_worker_loop, name="archive-worker", daemon=True)
    t.start()
    _log.info("归档后台线程已启动")


def _enqueue(job_dir: Path) -> None:
    """加入内存队列（同一任务不重复入队）"""
    key = str(job_dir)
    with _inflight_lock:
        if key in _inflight:
            return
        _inflight.add(key)
    _job_queue.put(job_dir)


def _scan_pending() -> None:
    """扫描本地待办队列，把到时间的任务重新入队；积压时打 ERROR 方便发现"""
    global _last_pending_log
    try:
        if not ARCHIVE_QUEUE_DIR.exists():
            return
        now = time.time()
        stuck = []
        for item in sorted(ARCHIVE_QUEUE_DIR.iterdir()):
            if not item.is_dir():
                continue
            job_file = item / "job.json"
            if not job_file.exists():
                continue
            try:
                job = json.loads(job_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            # 只统计失败过的任务，正常待处理任务不算积压
            if job.get("attempt", 0) > 0:
                stuck.append(job)
            if job.get("next_retry", 0) > now:
                continue
            _enqueue(item)

        if stuck and now - _last_pending_log > 3600:
            _last_pending_log = now
            last_error = stuck[-1].get("last_error", "")[:150]
            _log.error(
                f"归档队列积压 {len(stuck)} 条（最近错误：{last_error or '未知'}）"
            )
    except Exception as e:
        _log.warning(f"扫描归档队列失败: {e}")


def _worker_loop() -> None:
    """后台循环：串行处理，失败按退避重试"""
    _scan_pending()
    while True:
        try:
            job_dir = _job_queue.get(timeout=300)
        except queue.Empty:
            _scan_pending()
            continue
        try:
            _process_job(job_dir)
        except Exception as e:
            _log.error(f"归档任务异常: {e}", exc_info=True)
        finally:
            with _inflight_lock:
                _inflight.discard(str(job_dir))
            _job_queue.task_done()


def _process_job(job_dir: Path) -> None:
    """处理单个归档任务"""
    job_file = job_dir / "job.json"
    try:
        job = json.loads(job_file.read_text(encoding="utf-8"))
    except Exception as e:
        _log.error(f"归档任务文件损坏，丢弃 {job_dir.name}: {e}")
        shutil.rmtree(str(job_dir), ignore_errors=True)
        return

    try:
        _do_archive(job, job_dir)
        shutil.rmtree(str(job_dir), ignore_errors=True)
        _log.info(f"归档成功: {job['event_type']} {job['event_key']}")
    except Exception as e:
        _schedule_retry(job, job_dir, e)


def _schedule_retry(job: Dict[str, Any], job_dir: Path, error: Exception) -> None:
    """失败任务写回队列，按指数退避重试"""
    attempt = int(job.get("attempt", 0)) + 1
    delay = min(21600, 60 * (2 ** min(attempt - 1, 8)))  # 1m,2m,4m...最长6h
    if isinstance(error, _GithubError) and error.permanent:
        delay = 21600  # 配置类错误，6小时后再试，避免刷日志
        _log.error(
            f"归档失败（配置问题，请检查 token/仓库权限）: {error}"
        )
    else:
        _log.warning(f"归档失败（第 {attempt} 次，{delay // 60} 分钟后重试）: {error}")

    job["attempt"] = attempt
    job["next_retry"] = time.time() + delay
    job["last_error"] = str(error)[:500]
    try:
        _write_json(job_dir / "job.json", job)
    except Exception as e:
        _log.error(f"写回归档任务失败: {e}")


def _do_archive(job: Dict[str, Any], job_dir: Path, storage: Optional["_BaseStorage"] = None) -> None:
    """渲染 MD/CSV + 下载图片 + 原子提交；storage 可注入（历史回填用）"""
    event_type = job["event_type"]
    type_dir = GROUP_DIRS.get(event_type, "notices")
    group_name = GROUP_NAMES.get(type_dir, "其他")
    month = job["backup_time"][:7]  # 2026-11

    storage = storage or _get_storage()
    md_path = f"{type_dir}/{month}.md"
    csv_path = f"{type_dir}/{month}.csv"

    existing_md = storage.read_text(md_path) or ""
    if f"<!-- event-key: {job['event_key']} -->" in existing_md:
        _log.info(f"归档已存在，跳过重复: {job['event_key']}")
        return

    collector = _AssetCollector(
        type_dir=type_dir,
        month=month,
        event_key=job["event_key"],
        job_dir=job_dir,
        timeout=GITHUB_ARCHIVE_TIMEOUT,
        max_size=GITHUB_ARCHIVE_IMAGE_MAX_SIZE,
        quality=GITHUB_ARCHIVE_WEBP_QUALITY,
    )

    content_md, emojis = _render_content(job, collector)
    screenshot_path = collector.add_screenshot(job)
    meta_lines = _build_meta(job, emojis)
    summary = _make_summary(content_md or job.get("content_text", ""))
    if not summary:
        summary = "图片动态" if collector.files else "无文字内容"
    content_plain = _make_plain(content_md or job.get("content_text", ""))

    section = _make_section(job, meta_lines, content_md, summary, screenshot_path)
    new_md = _append_md(existing_md, section, group_name, month)

    existing_csv = storage.read_text(csv_path) or ""
    new_csv = _append_csv(existing_csv, job, summary, content_plain, emojis, collector)

    # 新月份第一次归档时，把该月加进 README 索引
    readme_path = "README.md"
    existing_readme = storage.read_text(readme_path) or ARCHIVE_README
    new_readme = _update_readme_index(existing_readme, type_dir, month)

    files = {
        md_path: new_md.encode("utf-8"),
        csv_path: new_csv.encode("utf-8"),
    }
    files.update(collector.files)
    if new_readme != existing_readme:
        files[readme_path] = new_readme.encode("utf-8")

    commit_msg = f"archive: {job['title']} {job['backup_time']}"
    storage.commit(files, commit_msg)

    if job.get("attempt", 0) > 0:
        _log.info(f"归档重试成功（第 {job['attempt'] + 1} 次）: {job['event_key']}")


# ======================================================================
# 内容渲染
# ======================================================================

def _render_content(job: Dict[str, Any], collector: "_AssetCollector") -> Tuple[str, List[str]]:
    """把 HTML / 文本渲染成 Markdown，返回 (正文, 表情名列表)"""
    html = job.get("content_html", "")
    text = job.get("content_text", "")
    image_urls = job.get("image_urls", [])
    event_type = job.get("event_type", "")
    emojis: List[str] = []

    if html:
        body, emojis = _html_to_md(html, collector, image_urls)
    elif text:
        body = _plain_text_to_md(text)
    else:
        body = ""

    # 直播类：没有 HTML 正文，补充标题信息
    if event_type in ("live_start", "live_end", "title_change") and not body:
        extra = job.get("extra", {})
        if event_type == "title_change":
            previous_title = extra.get("previous_title", "")
            title = extra.get("title", "")
            if previous_title:
                body = _plain_text_to_md(
                    f"原标题：{previous_title}\n\n新标题：{title}"
                )
            else:
                body = _plain_text_to_md(f"标题：{title}")
        else:
            body = _plain_text_to_md(f"标题：{extra.get('title', '')}")

    # 把没有出现在 HTML 里的图片补到正文末尾（评论区图片 / 直播封面）
    referenced = set(collector.url_map.keys())
    extra_images = []
    for url in image_urls:
        norm = _normalize_url(url)
        if not norm or norm in referenced:
            continue
        kind = "cover" if event_type in ("live_start", "live_end", "title_change") else "image"
        alt = "直播封面" if kind == "cover" else "图片"
        repo_path = collector.add_url(norm, kind=kind, alt=alt)
        if repo_path:
            extra_images.append(f"![{alt}]({_md_asset(repo_path)})")
            referenced.add(norm)

    if extra_images:
        body = (body.strip() + "\n\n" + "\n\n".join(extra_images)).strip()

    return _normalize_blank_lines(body).strip(), emojis


def _html_to_md(html: str, collector: "_AssetCollector", image_urls: List[str]) -> Tuple[str, List[str]]:
    """B站评论/动态 HTML → Markdown（表情、图片保留为内联图片）"""
    soup = BeautifulSoup(html, "html.parser")
    comment_urls = {_normalize_url(u) for u in image_urls if u}
    placeholders = {}
    emojis: List[str] = []

    for idx, img in enumerate(list(soup.find_all("img"))):
        src = _normalize_url(img.get("src", ""))
        alt = (img.get("alt") or "").strip()
        cls = " ".join(img.get("class") or [])
        is_emoji = ("emoji" in cls.lower() or alt.startswith("[")) and src not in comment_urls

        if not src:
            img.replace_with("")
            continue

        if is_emoji:
            name = alt.strip("[]").strip() or "表情"
            repo_path = collector.add_url(src, kind="emoji", alt=name)
            if repo_path:
                md_img = f"![{_md_alt(name)}]({_md_asset(repo_path)})"
                emojis.append(name)
            else:
                md_img = f"[表情：{name}]({src})"
        else:
            name = alt.strip("[]").strip() or "图片"
            repo_path = collector.add_url(src, kind="image", alt=name)
            if repo_path:
                md_img = f"![{_md_alt(name)}]({_md_asset(repo_path)})"
            else:
                md_img = f"[图片]({src})"

        token = f"\x00IMG{idx}\x00"
        placeholders[token] = md_img
        img.replace_with(token)

    for br in soup.find_all("br"):
        br.replace_with("\x00BR\x00")
    for tag in soup.find_all(["p", "div", "li", "blockquote", "h1", "h2", "h3"]):
        tag.append("\x00NL\x00")

    text = soup.get_text("")
    text = _escape_md_text(text)
    for token, md_img in placeholders.items():
        text = text.replace(token, md_img)
    text = text.replace("\x00BR\x00", "<br>\n")
    text = text.replace("\x00NL\x00", "\n\n")

    return _normalize_blank_lines(text).strip(), emojis


def _build_meta(job: Dict[str, Any], emojis: List[str]) -> List[str]:
    """生成事件头部信息（纯文字，方便搜索）"""
    lines = []
    extra = job.get("extra", {})
    event_type = job.get("event_type", "")

    if event_type == "pinned_comment":
        lines.append(f"- 动态 ID：{extra.get('dynamic_id', '')}")
        lines.append(f"- 评论 rpid：{extra.get('rpid', '') or '未获取'}")
    elif event_type in ("live_start", "live_end", "title_change"):
        if extra.get("status_tags"):
            lines.append(f"- 特别状态：{_single_line(str(extra['status_tags']))}")
    elif event_type == "new_dynamic":
        lines.append(f"- 动态 ID：{extra.get('dynamic_id', '')}")

    if emojis:
        unique = []
        for name in emojis:
            if name not in unique:
                unique.append(name)
        lines.append(f"- 表情：{'、'.join(unique)}")

    if job.get("channels"):
        lines.append(f"- 推送：{' / '.join(job['channels'])}")
    if job.get("source_url"):
        lines.append(f"- 来源：{job['source_url']}")

    return lines


def _make_section(
    job: Dict[str, Any],
    meta_lines: List[str],
    content_md: str,
    summary: str,
    screenshot_path: Optional[str],
) -> str:
    """生成月度 MD 中的单个事件小节"""
    parts = [
        f"<!-- event-key: {job['event_key']} -->",
        f"<!-- event-summary: {_comment_safe(summary)} -->",
        f"## {job['backup_time']} · {job['title']}",
        "",
    ]
    if meta_lines:
        parts.extend(["\n".join(meta_lines), ""])
    if content_md:
        parts.extend([content_md, ""])
    if screenshot_path:
        parts.extend([
            "<details><summary>查看截图</summary>",
            "",
            f"![截图]({_md_asset(screenshot_path)})",
            "",
            "</details>",
            "",
        ])
    parts.extend(["---", ""])
    return "\n".join(parts)


# ======================================================================
# 月度文件追加
# ======================================================================

def _append_md(existing: str, section: str, group_name: str, month: str) -> str:
    """把新事件追加到月度 MD，重建顶部索引表"""
    prefix, sections, suffix = _extract_md_blocks(existing)
    sections = (sections.rstrip() + "\n\n" + section.strip() + "\n").strip()

    rows = []
    for m in _EVENT_HEAD_RE.finditer(sections):
        rows.append((m.group("time").strip(), m.group("title").strip(), m.group("summary").strip()))

    last_time = max((r[0] for r in rows), default="")
    header_lines = [
        f"# {group_name}归档 · {month}",
        "",
        f"> 本月共 {len(rows)} 条 · 最后更新 {last_time}",
        "",
        "| 时间 | 类型 | 摘要 |",
        "| --- | --- | --- |",
    ]
    for row_time, row_title, row_summary in rows:
        header_lines.append(
            f"| {_table_cell(row_time)} | {_table_cell(row_title)} | {_table_cell(row_summary)} |"
        )
    header = "\n".join(header_lines)

    return (
        f"{prefix}{HEADER_START}\n{header}\n{HEADER_END}\n\n"
        f"{SECTIONS_START}\n{sections}\n{SECTIONS_END}\n{suffix}"
    )


def _extract_md_blocks(existing: str) -> Tuple[str, str, str]:
    """拆出前缀 / 正文区 / 后缀，兼容手动改过的文件"""
    if not existing or not existing.strip():
        return "", "", ""
    hs = existing.find(HEADER_START)
    he = existing.find(HEADER_END)
    ss = existing.find(SECTIONS_START)
    se = existing.find(SECTIONS_END)
    if min(hs, he, ss, se) == -1:
        # 没有标记（旧文件或手写文件）：整体当正文区
        return "", existing.strip(), ""
    prefix = existing[:hs]
    suffix = existing[se + len(SECTIONS_END):]
    sections = existing[ss + len(SECTIONS_START):se].strip()
    return prefix, sections, suffix


def _update_readme_index(readme: str, type_dir: str, month: str) -> str:
    """把 type/month 写进 README 索引；已存在则原样返回"""
    if not readme:
        readme = ARCHIVE_README

    start = readme.find(INDEX_START)
    end = readme.find(INDEX_END)
    if start == -1 or end == -1 or end < start:
        block = f"{INDEX_START}\n{INDEX_END}"
        insert_at = readme.find("## 目录")
        if insert_at == -1:
            readme = readme.rstrip() + f"\n\n## 归档索引（自动维护）\n\n{block}\n"
        else:
            readme = readme[:insert_at] + f"## 归档索引（自动维护）\n\n{block}\n\n" + readme[insert_at:]
        start = readme.find(INDEX_START)
        end = readme.find(INDEX_END)

    block = readme[start + len(INDEX_START):end]
    if f"]({type_dir}/{month}.md)" in block:
        return readme

    # 解析已有条目
    entries = {name: set() for name, _ in INDEX_TYPES}
    for match in re.finditer(
        r"- \[(\d{4}-\d{2})\]\((pinned|live|dynamics)/\1\.md\)", block
    ):
        entries.setdefault(match.group(2), set()).add(match.group(1))
    entries.setdefault(type_dir, set()).add(month)

    # 重建索引块（月份倒序）
    lines = []
    for name, label in INDEX_TYPES:
        months = sorted(entries.get(name, set()), reverse=True)
        lines.append(f"### {label}")
        if months:
            for item in months:
                lines.append(f"- [{item}]({name}/{item}.md) · [CSV]({name}/{item}.csv)")
        else:
            lines.append("- 暂无")
        lines.append("")

    new_block = "\n" + "\n".join(lines) + "\n"
    return readme[:start + len(INDEX_START)] + new_block + readme[end:]


def _append_csv(
    existing: str,
    job: Dict[str, Any],
    summary: str,
    content_plain: str,
    emojis: List[str],
    collector: "_AssetCollector",
) -> str:
    """把新事件追加到月度 CSV（UTF-8 BOM，方便 Excel 打开）"""
    existing = existing.lstrip("\ufeff")
    rows = []
    if existing.strip():
        try:
            rows = list(csv.DictReader(io.StringIO(existing)))
        except Exception:
            rows = []

    if any(r.get("event_key") == job["event_key"] for r in rows):
        rows = [r for r in rows if r.get("event_key") != job["event_key"]]

    extra = job.get("extra", {})
    image_paths = list(collector.files.keys())
    row = {
        "event_key": job["event_key"],
        "backup_time": job["backup_time"],
        "event_type": job["event_type"],
        "event_title": job["title"],
        "dynamic_id": str(extra.get("dynamic_id", "")),
        "rpid": str(extra.get("rpid", "")),
        "summary": _single_line(summary),
        "content": _single_line(content_plain),
        "emojis": "、".join(_unique(emojis)),
        "image_count": str(len(image_paths)),
        "image_paths": " | ".join(image_paths),
        "source_url": job.get("source_url", ""),
        "channels": " / ".join(job.get("channels", [])),
        "extra_json": json.dumps(extra, ensure_ascii=False),
    }
    rows.append(row)

    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow({k: r.get(k, "") for k in CSV_FIELDS})
    return "\ufeff" + out.getvalue()


# ======================================================================
# 图片处理
# ======================================================================

class _AssetCollector:
    """收集一次事件需要的所有图片，转成 WebP 并生成仓库相对路径"""

    def __init__(
        self,
        type_dir: str,
        month: str,
        event_key: str,
        job_dir: Path,
        timeout: int,
        max_size: int,
        quality: int,
    ):
        self.type_dir = type_dir
        self.month = month
        self.event_key = event_key
        self.job_dir = job_dir
        self.timeout = timeout
        self.max_size = max_size
        self.quality = quality
        self.files: Dict[str, bytes] = {}  # 仓库路径 -> 文件内容
        self.url_map: Dict[str, Tuple[str, str, str]] = {}  # 归一化 URL -> (仓库路径, kind, alt)
        self._seq = 0
        self.screenshot_path: Optional[str] = None

    def add_url(self, url: str, kind: str = "image", alt: str = "") -> Optional[str]:
        """下载/转换一张图片，返回仓库相对路径；失败返回 None"""
        norm = _normalize_url(url)
        if not norm:
            return None
        if norm in self.url_map:
            return self.url_map[norm][0]

        try:
            if kind == "emoji":
                digest = hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]
                cache_path = EMOJI_CACHE_DIR / f"{digest}.webp"
                if PIL_AVAILABLE and cache_path.exists():
                    data = cache_path.read_bytes()
                else:
                    raw = _load_bytes(norm, self.timeout)
                    if raw is None:
                        return None
                    converted = _to_webp(raw, self.quality, max_size=None)
                    if converted is None:
                        return None
                    data = converted
                    if PIL_AVAILABLE:
                        EMOJI_CACHE_DIR.mkdir(parents=True, exist_ok=True)
                        cache_path.write_bytes(data)
                repo_path = f"assets/emoji/{digest}.webp"
                self.files[repo_path] = data
                self.url_map[norm] = (repo_path, kind, alt)
                return repo_path

            # 普通图片（评论图/封面）
            raw = _load_bytes(norm, self.timeout)
            if raw is None:
                return None
            if PIL_AVAILABLE:
                try:
                    with Image.open(io.BytesIO(raw)) as probe:
                        width, height = probe.size
                    if width * height > MAX_SOURCE_PIXELS:
                        _log.warning(
                            f"图片像素过大({width}x{height})，已跳过: {norm[:120]}"
                        )
                        return None
                except Exception as e:
                    _log.warning(f"图片尺寸检查失败，已跳过: {norm[:120]} {e}")
                    return None
            self._seq += 1
            converted = _to_webp(raw, self.quality, max_size=self.max_size)
            if converted is not None:
                data, ext = converted, "webp"
            else:
                # Pillow 不可用时保留原格式，归档不中断
                data, ext = raw, _guess_ext(norm)
            repo_path = (
                f"assets/{self.type_dir}/{self.month}/{self.event_key}/"
                f"{kind}_{self._seq:02d}.{ext}"
            )
            self.files[repo_path] = data
            self.url_map[norm] = (repo_path, kind, alt)
            return repo_path
        except Exception as e:
            _log.warning(f"图片处理失败 {norm[:80]}: {e}")
            return None

    def add_screenshot(self, job: Dict[str, Any]) -> Optional[str]:
        """把本地截图转 WebP 并加入提交"""
        name = job.get("screenshot_file")
        if not name:
            return None
        src = self.job_dir / name
        if not src.exists():
            return None

        try:
            raw = src.read_bytes()
            if PIL_AVAILABLE:
                try:
                    with Image.open(io.BytesIO(raw)) as probe:
                        width, height = probe.size
                    if width * height > MAX_SOURCE_PIXELS:
                        _log.warning(
                            f"截图像素过大({width}x{height})，已跳过: {src.name}"
                        )
                        return None
                except Exception as e:
                    _log.warning(f"截图尺寸检查失败，已跳过: {src.name} {e}")
                    return None
            converted = _to_webp(raw, self.quality, max_size=self.max_size)
            if converted is not None:
                data, ext = converted, "webp"
            else:
                data, ext = raw, (src.suffix.lower().lstrip(".") or "png")
            repo_path = (
                f"assets/{self.type_dir}/{self.month}/{self.event_key}/card.{ext}"
            )
            self.files[repo_path] = data
            self.screenshot_path = repo_path
            return repo_path
        except Exception as e:
            _log.warning(f"截图处理失败: {e}")
            return None


def _load_bytes(src: str, timeout: int) -> Optional[bytes]:
    """读取本地文件或下载网络图片"""
    try:
        if src.startswith("file://"):
            path = src[7:]
            if re.match(r"^/[A-Za-z]:/", path):
                path = path[1:]
            return Path(path).read_bytes()
        if os.path.isabs(src) and Path(src).exists():
            return Path(src).read_bytes()

        headers = {
            "User-Agent": USER_AGENT,
            "Referer": "https://www.bilibili.com/",
        }
        for attempt in range(3):
            try:
                resp = requests.get(src, headers=headers, timeout=timeout)
                if resp.status_code == 200 and resp.content:
                    return resp.content
                if resp.status_code in (403, 404):
                    break
            except Exception:
                if attempt >= 2:
                    break
                time.sleep(1)
    except Exception as e:
        _log.warning(f"读取图片失败 {src[:80]}: {e}")
    return None


def _to_webp(raw: bytes, quality: int, max_size: Optional[int] = None) -> Optional[bytes]:
    """图片转 WebP；Pillow 不可用或失败时返回 None"""
    if not PIL_AVAILABLE:
        return None
    try:
        img = Image.open(io.BytesIO(raw))
        if img.width * img.height > MAX_SOURCE_PIXELS:
            _log.warning(f"图片像素过大，已跳过: {img.width}x{img.height}")
            return None
        if getattr(img, "is_animated", False):
            img.seek(0)
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
        else:
            img = img.convert("RGB")
        if max_size:
            img.thumbnail((max_size, max_size), Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="WEBP", quality=quality, method=4)
        return out.getvalue()
    except Exception as e:
        _log.warning(f"WebP 转换失败: {e}")
        return None


# ======================================================================
# 存储（GitHub / 本地预览）
# ======================================================================

def _get_storage() -> "_BaseStorage":
    """按配置返回存储后端"""
    if _demo_mode or GITHUB_ARCHIVE_DRY_RUN:
        return _PreviewStorage()
    return _GithubStorage()


class _BaseStorage:
    def read_text(self, rel_path: str) -> Optional[str]:
        raise NotImplementedError

    def commit(self, files: Dict[str, bytes], message: str) -> None:
        raise NotImplementedError


class _PreviewStorage(_BaseStorage):
    """本地预览/回填：写到指定目录，不碰 GitHub"""

    def __init__(self, root: Optional[Path] = None):
        self.root = Path(root) if root else PREVIEW_DIR

    def read_text(self, rel_path: str) -> Optional[str]:
        path = self.root / rel_path
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    def commit(self, files: Dict[str, bytes], message: str) -> None:
        for rel_path, data in files.items():
            path = self.root / rel_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        _log.info(f"归档本地写入: {self.root}")


class _GithubStorage(_BaseStorage):
    """GitHub 仓库存储（Git Data API，一次提交多个文件）"""

    API = "https://api.github.com"

    def __init__(self):
        self.owner = GITHUB_ARCHIVE_OWNER
        self.repo = GITHUB_ARCHIVE_REPO
        self.branch = GITHUB_ARCHIVE_BRANCH
        self.token = GITHUB_ARCHIVE_TOKEN
        self.timeout = GITHUB_ARCHIVE_TIMEOUT
        if not self.token:
            raise _GithubError(401, "未配置 GITHUB_ARCHIVE_TOKEN")

    def _headers(self, accept: str = "application/vnd.github+json") -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "BTCE-notice-archive",
        }

    def _api(self, method: str, path: str, allow_404: bool = False, **kwargs) -> Optional[requests.Response]:
        url = f"{self.API}{path}"
        try:
            resp = requests.request(
                method, url, headers=self._headers(), timeout=self.timeout, **kwargs
            )
        except Exception as e:
            raise RuntimeError(f"GitHub 请求失败: {e}")

        if resp.status_code == 404 and allow_404:
            return None
        if resp.status_code >= 400:
            try:
                message = resp.json().get("message", "")
            except Exception:
                message = (resp.text or "")[:200]
            raise _GithubError(resp.status_code, message)
        return resp

    def read_text(self, rel_path: str) -> Optional[str]:
        url = f"{self.API}/repos/{self.owner}/{self.repo}/contents/{rel_path}"
        try:
            resp = requests.get(
                url,
                headers=self._headers(accept="application/vnd.github.raw"),
                params={"ref": self.branch},
                timeout=self.timeout,
            )
        except Exception as e:
            raise RuntimeError(f"GitHub 读取失败: {e}")
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            try:
                message = resp.json().get("message", "")
            except Exception:
                message = (resp.text or "")[:200]
            raise _GithubError(resp.status_code, message)
        return resp.content.decode("utf-8")

    def ensure_initialized(self) -> None:
        """空仓库没有初始提交时 Git Data API 会 409，先用 Contents API 建 README"""
        if self._get_head():
            return

        payload = {
            "message": "docs: add archive README",
            "content": base64.b64encode(ARCHIVE_README.encode("utf-8")).decode("ascii"),
            "branch": self.branch,
        }
        try:
            self._api(
                "PUT",
                f"/repos/{self.owner}/{self.repo}/contents/README.md",
                json=payload,
            )
        except _GithubError as e:
            # 422 一般是并发初始化时 README 已存在，继续检查分支即可
            if e.status != 422:
                raise

        for _ in range(10):
            if self._get_head():
                return
            time.sleep(1)
        raise RuntimeError("空仓库初始化后仍未找到分支，请检查仓库设置")

    def commit(self, files: Dict[str, bytes], message: str) -> None:
        """创建 blob → tree → commit → 更新 ref，冲突时重试"""
        if not files:
            return

        self.ensure_initialized()

        last_error = None
        for attempt in range(4):
            try:
                head_sha = self._get_head()
                base_tree = None
                parents = []
                if head_sha:
                    commit_info = self._api(
                        "GET", f"/repos/{self.owner}/{self.repo}/git/commits/{head_sha}"
                    ).json()
                    base_tree = commit_info["tree"]["sha"]
                    parents = [head_sha]

                tree_entries = []
                for rel_path, data in files.items():
                    blob = self._api(
                        "POST",
                        f"/repos/{self.owner}/{self.repo}/git/blobs",
                        json={
                            "content": base64.b64encode(data).decode("ascii"),
                            "encoding": "base64",
                        },
                    ).json()
                    tree_entries.append({
                        "path": rel_path,
                        "mode": "100644",
                        "type": "blob",
                        "sha": blob["sha"],
                    })

                tree_payload: Dict[str, Any] = {"tree": tree_entries}
                if base_tree:
                    tree_payload["base_tree"] = base_tree
                tree = self._api(
                    "POST",
                    f"/repos/{self.owner}/{self.repo}/git/trees",
                    json=tree_payload,
                ).json()

                commit_payload: Dict[str, Any] = {
                    "message": message,
                    "tree": tree["sha"],
                }
                if parents:
                    commit_payload["parents"] = parents
                new_commit = self._api(
                    "POST",
                    f"/repos/{self.owner}/{self.repo}/git/commits",
                    json=commit_payload,
                ).json()

                if head_sha:
                    self._api(
                        "PATCH",
                        f"/repos/{self.owner}/{self.repo}/git/refs/heads/{self.branch}",
                        json={"sha": new_commit["sha"]},
                    )
                else:
                    self._api(
                        "POST",
                        f"/repos/{self.owner}/{self.repo}/git/refs",
                        json={
                            "ref": f"refs/heads/{self.branch}",
                            "sha": new_commit["sha"],
                        },
                    )
                return
            except _GithubError as e:
                last_error = e
                if e.status in (409, 422) and attempt < 3:
                    time.sleep(1 + attempt)
                    continue
                raise
            except Exception as e:
                last_error = e
                if attempt < 3:
                    time.sleep(1 + attempt)
                    continue
                raise

        raise RuntimeError(f"GitHub 提交失败: {last_error}")

    def _get_head(self) -> Optional[str]:
        try:
            resp = self._api(
                "GET",
                f"/repos/{self.owner}/{self.repo}/git/ref/heads/{self.branch}",
                allow_404=True,
            )
        except _GithubError as e:
            # 空仓库首次提交时 GitHub 可能返回 409
            if e.status == 409:
                return None
            raise
        if resp is None:
            return None
        return resp.json().get("object", {}).get("sha")


# ======================================================================
# 工具函数
# ======================================================================

def _normalize_url(url: str) -> str:
    """补全协议头、剥掉 B站 CDN 尺寸后缀"""
    if not url:
        return ""
    url = str(url).strip()
    if url.startswith("//"):
        url = "https:" + url
    if "hdslb.com" in url:
        url = re.sub(r"@[^/?#]+", "", url)
    return url


def _with_tz(event_time: str) -> str:
    """时间补上本地时区（服务器/本机都是 +0800）"""
    tz = datetime.now().astimezone().strftime("%z")
    text = (event_time or "").strip()
    if not text:
        return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S ") + tz
    if re.search(r"[+-]\d{4}$", text):
        return text
    return f"{text} {tz}"


def _sanitize_key(value: str) -> str:
    """文件/目录名安全化"""
    text = re.sub(r"[^0-9A-Za-z_.-]+", "_", str(value))
    text = text.strip("._-")[:120]
    return text or "event"


def _escape_md_text(text: str) -> str:
    """转义会破坏 Markdown 结构的字符"""
    text = text.replace("\\", "\\\\")
    text = re.sub(r"([`*_\[\]])", r"\\\1", text)
    text = re.sub(r"(?m)^(\s*)([#>])", r"\1\\\2", text)
    text = re.sub(r"(?m)^(\s*)([-+])(\s)", r"\1\\\2\3", text)
    return text


def _md_alt(name: str) -> str:
    """Markdown 图片 alt 文本安全化"""
    return re.sub(r"[\[\]\n\r]", " ", name).strip() or "图片"


def _plain_text_to_md(text: str) -> str:
    """纯文本转 Markdown，保留人工换行"""
    return _escape_md_text(text or "").replace("\n", "<br>\n")


def _comment_safe(text: str) -> str:
    """HTML 注释里不能出现 -- 和换行"""
    text = _single_line(text)
    return text.replace("--", "- -").strip() or "（无摘要）"


def _table_cell(text: str) -> str:
    """Markdown 表格单元格转义"""
    return _single_line(text).replace("|", "\\|")


def _md_asset(repo_path: str) -> str:
    """月度 MD 在 {type_dir}/ 下一层，图片相对路径要加 ../"""
    return f"../{repo_path}"


def _single_line(text: str) -> str:
    """多行压成一行"""
    return re.sub(r"\s+", " ", text or "").strip()


def _normalize_blank_lines(text: str) -> str:
    """整理空行：行尾空格去掉，连续空行最多 2 个"""
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _unique(items: List[str]) -> List[str]:
    """去重保序"""
    result = []
    for item in items:
        if item and item not in result:
            result.append(item)
    return result


def _make_summary(content_md: str, limit: int = 60) -> str:
    """从正文提取摘要（去掉图片语法）"""
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", content_md or "")
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\\([`*_\[\]])", r"\1", text)
    text = _single_line(text)
    return text[:limit] + ("…" if len(text) > limit else "")


def _make_plain(content_md: str) -> str:
    """正文转纯文本（CSV 用）"""
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"[图片:\1]", content_md or "")
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\\([`*_\[\]])", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    return _single_line(text)


def _guess_ext(url: str) -> str:
    """从 URL 猜图片扩展名（Pillow 不可用时的兜底）"""
    path = (url or "").split("?", 1)[0].split("#", 1)[0].lower()
    for ext in ("png", "jpg", "jpeg", "gif", "webp", "bmp"):
        if path.endswith("." + ext):
            return "jpg" if ext == "jpeg" else ext
    return "jpg"


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    """原子写 JSON"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(tmp), str(path))


# ======================================================================
# 本地演示：python archive_utils.py
# ======================================================================

def _check_github() -> None:
    """只读检查 GitHub 配置（不提交任何文件）"""
    try:
        storage = _GithubStorage()
        resp = storage._api("GET", f"/repos/{storage.owner}/{storage.repo}")
        info = resp.json()
        print(f"仓库: {info.get('full_name')}")
        print(f"私有: {info.get('private')}")
        readme = storage.read_text("README.md")
        print("README.md:", "存在" if readme is not None else "不存在")
        head = storage._get_head()
        print(f"分支 {storage.branch}: {head or '空仓库'}")
        print("只读检查通过。写权限需要实际提交一次才能验证。")
    except Exception as e:
        print(f"检查失败: {e}")


def _demo() -> None:
    """生成 5 类事件的本地预览，不联网、不提交 GitHub"""
    global _demo_mode
    _demo_mode = True

    demo_dir = DATA_DIR / "archive_demo"
    demo_dir.mkdir(parents=True, exist_ok=True)

    def _make_png(name: str, color: Tuple[int, int, int], size: Tuple[int, int] = (160, 160)) -> Optional[str]:
        if not PIL_AVAILABLE:
            return None
        try:
            img = Image.new("RGB", size, color)
            path = demo_dir / name
            img.save(path)
            return path.as_uri()
        except Exception:
            return None

    emoji_url = _make_png("star.png", (255, 210, 70), (96, 96))
    comment_url = _make_png("comment.png", (120, 180, 255), (640, 360))
    cover_url = _make_png("cover.png", (255, 140, 180), (960, 540))
    card_url = _make_png("card.png", (235, 235, 245), (1080, 700))
    card_path = None
    if PIL_AVAILABLE:
        try:
            card_path = str(demo_dir / "card.png")
        except Exception:
            card_path = None

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    emoji_tag = (
        f'<img class="emoji" alt="[星星]" src="{emoji_url}">'
        if emoji_url else "[星星]"
    )
    comment_tag = (
        f'<img alt="图片" src="{comment_url}">'
        if comment_url else ""
    )

    archive_event(
        event_type="pinned_comment",
        title="置顶评论更新",
        content_html=f"今天也很开心 {emoji_tag} 谢谢大家<br>这是第二条评论，包含图片。{comment_tag}",
        image_urls=[comment_url] if comment_url else [],
        screenshot_path=card_path,
        source_url="https://t.bilibili.com/100000000000000001",
        event_time=now,
        event_key=make_event_key("pinned_comment", "demo", now),
        extra={"dynamic_id": "100000000000000001", "rpid": "200000000001"},
        channels=["QQ", "邮件", "B站"],
    )

    archive_event(
        event_type="new_dynamic",
        title="新动态",
        content_html=f"发一条新动态 {emoji_tag} 配图如下{comment_tag}",
        image_urls=[comment_url] if comment_url else [],
        screenshot_path=card_path,
        source_url="https://t.bilibili.com/100000000000000002",
        event_time=now,
        event_key=make_event_key("new_dynamic", "100000000000000002"),
        extra={"dynamic_id": "100000000000000002"},
        channels=["QQ", "邮件"],
    )

    for event_type, title, previous in (
        ("live_start", "开播", ""),
        ("title_change", "标题修改", "旧的直播标题"),
        ("live_end", "下播", ""),
    ):
        archive_event(
            event_type=event_type,
            title=title,
            image_urls=[cover_url] if cover_url else [],
            source_url="https://live.bilibili.com/123456",
            event_time=now,
            event_key=make_event_key("live", event_type, now),
            extra={
                "title": "今天的直播标题",
                "previous_title": previous,
                "room_id": 123456,
                "status_tags": "付费直播",
            },
            channels=["QQ", "邮件"],
        )

    # 等待后台线程处理完
    deadline = time.time() + 60
    while time.time() < deadline:
        remaining = 0
        if ARCHIVE_QUEUE_DIR.exists():
            remaining = sum(1 for d in ARCHIVE_QUEUE_DIR.iterdir() if (d / "job.json").exists())
        if remaining == 0:
            break
        time.sleep(0.5)

    print(f"预览目录: {PREVIEW_DIR}")
    for path in sorted(PREVIEW_DIR.rglob("*")):
        if path.is_file():
            print(f"  {path.relative_to(PREVIEW_DIR)}")


if __name__ == "__main__":
    import sys
    if "--check" in sys.argv:
        _check_github()
    else:
        _demo()
