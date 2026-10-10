"""Deterministic browser collection; the Agent only reads the resulting new_items.json."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4
from zoneinfo import ZoneInfo

from sourceflow.db import (
    DEFAULT_CONFIG,
    DEFAULT_DB,
    ROOT,
    connect,
    clear_pending_item,
    due_pending_items,
    existing_external_ids,
    insert_candidates,
    load_sources,
    record_source_seen,
    record_source_check,
    save_pending_item,
    seen_external_ids,
    source_cursor,
    update_source_cursor,
    utc_time,
)


SCRIPTS = ROOT / "scripts"


class BrowserError(RuntimeError):
    pass


class BrowserAct:
    """Small subprocess wrapper that keeps verbose browser output away from the model."""

    def __init__(self, browser_id: str | None, session: str) -> None:
        if not shutil.which("browser-act"):
            raise BrowserError("browser-act executable not found")
        self.browser_id = browser_id or self._discover_browser_id()
        self.session = session
        self.opened = False

    @staticmethod
    def _base() -> list[str]:
        return ["browser-act"]

    def _run(
        self,
        args: list[str],
        *,
        stdin: str | None = None,
        timeout: int = 45,
        check: bool = True,
    ) -> str:
        result = subprocess.run(
            [*self._base(), *args],
            input=stdin,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        output = (result.stdout or "").strip()
        error = (result.stderr or "").strip()
        if check and result.returncode:
            raise BrowserError(error or output or f"browser-act exited {result.returncode}")
        return output

    def _discover_browser_id(self) -> str:
        output = self._run(["browser", "list"])
        for line in output.splitlines():
            if "type=chrome-direct" in line:
                match = re.search(r"\bid=([^\s]+)", line)
                if match:
                    return match.group(1)
        raise BrowserError("no configured chrome-direct browser; set SOURCEFLOW_BROWSER_ID")

    def open(self, url: str) -> None:
        self._run(["--session", self.session, "browser", "open", self.browser_id, url], timeout=60)
        self.opened = True

    def navigate(self, url: str) -> None:
        if not self.opened:
            self.open(url)
            return
        try:
            self._run(["--session", self.session, "navigate", url], timeout=45)
        except BrowserError as exc:
            message = str(exc).lower()
            if "210101" not in message and "health check" not in message and "daemon is not reachable" not in message:
                raise
            # This process owns the unique session, so one clean reconnect is safe.
            self._run(["session", "close", self.session], timeout=20, check=False)
            self.opened = False
            self.open(url)

    def eval_file(self, path: Path) -> Any:
        output = self._run(
            ["--session", self.session, "eval", "--stdin"],
            stdin=path.read_text(encoding="utf-8"),
            timeout=45,
        )
        try:
            return json.loads(output)
        except json.JSONDecodeError as exc:
            raise BrowserError(f"invalid JSON from {path.name}: {output[:300]}") from exc

    def wait_selector(self, selector: str, timeout_ms: int = 6000) -> None:
        self._run(
            ["--session", self.session, "wait", "selector", "--selector", selector, "--timeout", str(timeout_ms)],
            timeout=max(10, timeout_ms // 1000 + 5),
            check=False,
        )

    def wait_stable(self, timeout_ms: int = 5000) -> None:
        self._run(
            ["--session", self.session, "wait", "stable", "--timeout", str(timeout_ms)],
            timeout=max(10, timeout_ms // 1000 + 5),
            check=False,
        )

    def scroll(self, amount: int = 1500) -> None:
        self._run(
            ["--session", self.session, "scroll", "down", "--amount", str(amount)],
            timeout=15,
        )

    def close(self) -> None:
        if self.opened:
            self._run(["session", "close", self.session], timeout=20, check=False)
            self.opened = False


@dataclass
class SourceResult:
    source_key: str
    platform: str
    status: str
    items: list[dict[str, Any]]
    skipped: list[dict[str, Any]]
    cursor_id: str | None
    cursor_at: str | None
    error: str | None = None
    pages_checked: int = 0
    oldest_checked_at: str | None = None
    stop_reason: str | None = None
    pending: list[dict[str, Any]] | None = None
    resolved_pending: list[str] | None = None
    pending_total: int = 0


def _iso_from_millis(value: int | str | None) -> str | None:
    if not value:
        return None
    return datetime.fromtimestamp(int(value) / 1000, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _iso_from_seconds(value: int | str | None) -> str | None:
    if not value:
        return None
    return datetime.fromtimestamp(int(value), timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _newer(value: str | None, cutoff: str) -> bool:
    return bool(value and utc_time(value) > utc_time(cutoff))


def _since_at(conn: Any, source_key: str) -> str:
    row = conn.execute("SELECT since_at FROM sources WHERE source_key=?", (source_key,)).fetchone()
    if row is None:
        raise ValueError(f"unknown source: {source_key}")
    return row["since_at"]


def _retry_after(observed_at: str, *, hours: int) -> str:
    return (datetime.fromisoformat(utc_time(observed_at).replace("Z", "+00:00"))
            + timedelta(hours=hours)).isoformat(timespec="seconds").replace("+00:00", "Z")


def _eval_after_ready(browser: BrowserAct, script: Path, selector: str) -> Any:
    try:
        data = browser.eval_file(script)
        if data:
            return data
    except BrowserError:
        pass
    browser.wait_selector(selector)
    return browser.eval_file(script)


def collect_x(
    browser: BrowserAct,
    conn: Any,
    source: dict[str, Any],
    observed_at: str,
    max_scrolls: int,
) -> SourceResult:
    key = source["key"]
    handle = source["external_id"]
    since_at = _since_at(conn, key)
    existing = existing_external_ids(conn, key)
    cursor_state = source_cursor(conn, key)
    cursor = cursor_state["last_seen_external_id"]
    verified = bool(cursor_state["coverage_verified"])
    boundary_at = cursor_state["last_seen_at"] if verified else since_at
    rows_by_id: dict[str, dict[str, Any]] = {}
    pages_checked = 0
    stops: list[str] = []
    try:
        for suffix in ("", "/with_replies"):
            browser.navigate(f"https://x.com/{handle}{suffix}")
            stop = "page_limit"
            for scroll_index in range(max_scrolls + 1):
                rows = _eval_after_ready(browser, SCRIPTS / "extract-x-timeline.js", "article[data-testid='tweet']")
                pages_checked += 1
                own = [row for row in rows if row.get("handle", "").lower() == handle.lower()
                       and row.get("id") and row.get("published_at")]
                for row in own:
                    rows_by_id[row["id"]] = row
                tail = own[-1] if own else None
                # A pinned old post near the top is not evidence that the lower part was checked.
                if tail and (verified and tail["id"] in existing and not _newer(tail["published_at"], boundary_at)
                             or not _newer(tail["published_at"], since_at)):
                    stop = "known_boundary" if tail["id"] in existing else "since_boundary"
                    break
                if scroll_index == max_scrolls:
                    break
                browser.scroll()
                browser.wait_stable(3500)
            stops.append(f"{suffix or '/posts'}:{stop}")
    except Exception as exc:
        return SourceResult(key, "x", "failed", [], [], None, None, str(exc), pages_checked,
                            stop_reason="browser_error")

    ordered = sorted(rows_by_id.values(), key=lambda row: utc_time(row["published_at"]), reverse=True)
    items = []
    for row in ordered:
        if row["id"] in existing or not _newer(row.get("published_at"), since_at) and utc_time(row["published_at"]) != since_at:
            continue
        items.append(
            {
                "source_key": key,
                "platform": "x",
                "item_type": "post",
                "external_id": row["id"],
                "url": row["url"],
                "title": None,
                "content_text": row.get("text", ""),
                "published_at": utc_time(row["published_at"]),
                "event_at": utc_time(row["published_at"]),
                "observed_at": observed_at,
                "content_status": "complete",
                "metadata": {
                    "author_handle": handle,
                    "timeline": row.get("timeline"),
                    "source_method": "browseract_scripted",
                },
            }
        )
    newest = ordered[0] if ordered else None
    return SourceResult(
        key,
        "x",
        "complete" if all("page_limit" not in stop for stop in stops) else "partial",
        items,
        [],
        newest["id"] if newest else cursor,
        newest.get("published_at") if newest else cursor_state["last_seen_at"],
        pages_checked=pages_checked,
        oldest_checked_at=ordered[-1]["published_at"] if ordered else None,
        stop_reason=",".join(stops),
    )


def collect_xiaohongshu(
    browser: BrowserAct,
    conn: Any,
    source: dict[str, Any],
    observed_at: str,
    pending_rows: list[dict[str, Any]] | None = None,
) -> SourceResult:
    key = source["key"]
    author_id = source["external_id"]
    since_at = _since_at(conn, key)
    existing = existing_external_ids(conn, key)
    cursor = source_cursor(conn, key)
    verified = bool(cursor["coverage_verified"])
    try:
        browser.navigate(source["url"])
        page_rows = _eval_after_ready(
            browser,
            SCRIPTS / "extract-xiaohongshu-space.js",
            "a[href*='/explore/'], a[href*='/user/profile/']",
        )
    except Exception as exc:
        return SourceResult(key, "xiaohongshu", "failed", [], [], None, None, str(exc))

    page_rows = [row for row in page_rows if row.get("author") == author_id and row.get("id") and row.get("time")]
    rows_by_id = {row["id"]: row for row in pending_rows or [] if row.get("id")}
    rows_by_id.update({row["id"]: row for row in page_rows})
    ordered = sorted(rows_by_id.values(), key=lambda row: int(row.get("time") or 0), reverse=True)
    items: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    resolved: list[str] = []
    pending_ids = {row.get("id") for row in pending_rows or []}
    for row in ordered:
        published_at = _iso_from_millis(row.get("time"))
        if row["id"] in existing and row["id"] not in pending_ids:
            continue
        if not published_at or utc_time(published_at) < since_at:
            continue
        canonical = f"https://www.xiaohongshu.com/user/profile/{author_id}/{row['id']}"
        detail_url = canonical
        if row.get("xsec_token"):
            detail_url += f"?xsec_token={quote(row['xsec_token'])}&xsec_source=pc_user"
        detail: dict[str, Any] = {}
        try:
            browser.navigate(detail_url)
            detail = _eval_after_ready(
                browser,
                SCRIPTS / "extract-xiaohongshu-note.js",
                "body",
            )
        except Exception as exc:
            warnings.append({"external_id": row["id"], "reason": f"detail_partial: {exc}"})
        if detail.get("id") != row["id"] or not (detail.get("title") or detail.get("desc")):
            pending.append({
                "external_id": row["id"], "url": canonical,
                "payload": {"id": row["id"], "title": row.get("title"), "time": row.get("time"),
                            "type": row.get("type"), "author": author_id},
                "reason": "detail_unavailable", "retry_after": _retry_after(observed_at, hours=1),
            })
            continue
        event_at = _iso_from_millis(detail.get("time")) or published_at
        items.append(
            {
                "source_key": key,
                "platform": "xiaohongshu",
                "item_type": "note",
                "external_id": row["id"],
                "url": canonical,
                "title": detail.get("title") or row.get("title") or None,
                "content_text": detail.get("desc") or "",
                "published_at": event_at,
                "event_at": event_at,
                "observed_at": observed_at,
                "content_status": "partial",
                "metadata": {
                    "author_id": author_id,
                    "note_type": detail.get("type") or row.get("type"),
                    "media_count": detail.get("media_count"),
                    "source_method": "browseract_scripted",
                },
            }
        )
        resolved.append(row["id"])
    visible = sorted(page_rows, key=lambda row: int(row["time"]), reverse=True)
    newest = visible[0] if visible else None
    oldest = _iso_from_millis(visible[-1]["time"]) if visible else None
    boundary_at = cursor["last_seen_at"] if verified else since_at
    reached = bool(visible and (len(visible) < 20 or
                   (verified and visible[-1]["id"] in existing and not _newer(oldest, boundary_at)) or
                   (oldest and utc_time(oldest) <= since_at)))
    status = "complete" if reached and not pending else "partial"
    return SourceResult(
        key,
        "xiaohongshu",
        status,
        items,
        warnings,
        newest["id"] if newest else cursor["last_seen_external_id"],
        _iso_from_millis(newest.get("time")) if newest else cursor["last_seen_at"],
        pages_checked=1,
        oldest_checked_at=oldest,
        stop_reason="known_boundary" if reached else "initial_page_limit",
        pending=pending,
        resolved_pending=resolved,
    )


def collect_bilibili(
    browser: BrowserAct,
    conn: Any,
    source: dict[str, Any],
    observed_at: str,
    raw_dir: Path,
    pending_rows: list[dict[str, Any]] | None = None,
) -> SourceResult:
    key = source["key"]
    expected_mid = str(source["external_id"])
    since_at = _since_at(conn, key)
    cursor = source_cursor(conn, key)
    verified = bool(cursor["coverage_verified"])
    cursor_bvid = (cursor["last_seen_external_id"] or "").split(":", 1)[0]
    existing_bvids = {value.split(":", 1)[0] for value in existing_external_ids(conn, key)}
    seen_bvids = seen_external_ids(conn, key)
    try:
        browser.navigate(source["url"])
        rows = _eval_after_ready(browser, SCRIPTS / "extract-bilibili-space.js", "a[href*='/video/BV']")
    except Exception as exc:
        return SourceResult(key, "bilibili", "failed", [], [], None, None, str(exc))

    pending_ids = {row.get("bvid") for row in pending_rows or []}
    candidates_by_id = {row["bvid"]: row for row in pending_rows or [] if row.get("bvid")}
    known_bvids = existing_bvids | seen_bvids | ({cursor_bvid} if cursor_bvid else set())
    visible_ids = [row.get("bvid") for row in rows if row.get("bvid")]
    reached_boundary = bool(verified and cursor_bvid and cursor_bvid in visible_ids)
    for row in rows:
        bvid = row.get("bvid")
        if not bvid:
            continue
        if bvid in known_bvids and bvid not in pending_ids:
            continue
        candidates_by_id[bvid] = row

    items: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    resolved: list[str] = []
    newest_owner_id = cursor_bvid or None
    newest_owner_at = cursor["last_seen_at"]
    oldest_checked_at: str | None = None
    for row in candidates_by_id.values():
        bvid = row["bvid"]
        try:
            browser.navigate(row["url"])
            raw = _eval_after_ready(browser, SCRIPTS / "bilibili-subtitle-source.js", "body")
            if raw.get("status") == "signed_player_request_missing":
                browser.wait_stable(5000)
                raw = browser.eval_file(SCRIPTS / "bilibili-subtitle-source.js")
        except Exception as exc:
            skipped.append({"bvid": bvid, "url": row["url"], "reason": f"extract_failed: {exc}"})
            pending.append({"external_id": bvid, "url": row["url"],
                            "payload": {"bvid": bvid, "title": row.get("title")},
                            "reason": "extract_failed", "retry_after": _retry_after(observed_at, hours=1)})
            continue
        published_at = _iso_from_seconds(raw.get("pubdate"))
        if published_at and (oldest_checked_at is None or utc_time(published_at) < utc_time(oldest_checked_at)):
            oldest_checked_at = published_at
        owner_mid = str((raw.get("owner") or {}).get("mid") or "")
        if owner_mid == expected_mid and published_at and (
            newest_owner_at is None or utc_time(published_at) > utc_time(newest_owner_at)
        ):
            newest_owner_id = bvid
            newest_owner_at = published_at
        reason = None
        if owner_mid != expected_mid:
            reason = f"owner_mismatch:{owner_mid or 'unknown'}"
        elif not published_at or utc_time(published_at) < since_at:
            reason = "before_since_or_unknown_time"
        elif raw.get("status") != "ok":
            reason = raw.get("status") or "subtitle_unavailable"
        if reason:
            skipped.append(
                {
                    "bvid": bvid,
                    "url": row["url"],
                    "title": raw.get("title") or row.get("title"),
                    "published_at": published_at,
                    "reason": reason,
                }
            )
            if not reason.startswith("owner_mismatch:") and reason != "before_since_or_unknown_time":
                pending.append({"external_id": bvid, "url": row["url"],
                                "payload": {"bvid": bvid, "title": raw.get("title") or row.get("title")},
                                "reason": reason,
                                "retry_after": _retry_after(observed_at, hours=168 if reason in
                                                            {"no_subtitle", "chinese_subtitle_missing"} else 1)})
            else:
                resolved.append(bvid)
            continue
        raw_path = raw_dir / f"{bvid}.json"
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        cid = raw["cid"]
        items.append(
            {
                "source_key": key,
                "platform": "bilibili",
                "item_type": "video",
                "external_id": f"{bvid}:{cid}",
                "url": raw["url"],
                "title": (raw.get("title") or row.get("title") or "").removesuffix("_哔哩哔哩_bilibili"),
                "published_at": published_at,
                "event_at": published_at,
                "observed_at": observed_at,
                "content_status": "complete",
                "raw_ref": str(raw_path.relative_to(ROOT)),
                "metadata": {
                    "owner_mid": int(expected_mid),
                    "cid": cid,
                    "duration_seconds": raw.get("duration"),
                    "subtitle_language": raw.get("language"),
                    "subtitle_segment_count": raw.get("segment_count"),
                    "source_method": "browseract_scripted",
                },
            }
        )
        resolved.append(bvid)
    if not cursor_bvid and oldest_checked_at and utc_time(oldest_checked_at) <= since_at:
        reached_boundary = True
    status = "complete" if reached_boundary and not pending else "partial"
    return SourceResult(
        key,
        "bilibili",
        status,
        items,
        skipped,
        newest_owner_id or cursor["last_seen_external_id"],
        newest_owner_at,
        pages_checked=1,
        oldest_checked_at=oldest_checked_at,
        stop_reason="known_boundary" if reached_boundary else "visible_list_limit",
        pending=pending,
        resolved_pending=resolved,
    )


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _scroll_limit(conn: Any, source_key: str, default: int) -> int:
    previous = conn.execute(
        """SELECT pages_checked, stop_reason FROM source_checks
           WHERE source_key=? AND status='partial' ORDER BY checked_at DESC LIMIT 1""",
        (source_key,),
    ).fetchone()
    if previous and "page_limit" in (previous["stop_reason"] or ""):
        return max(default, min(30, previous["pages_checked"]))
    return default


def run(args: argparse.Namespace) -> dict[str, Any]:
    started = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    run_date = args.date or datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    output_dir = args.output_dir or ROOT / "data" / "raw" / "runs" / run_date / run_id
    platforms = {value.strip() for value in args.platforms.split(",") if value.strip()}
    config = json.loads(args.config.read_text(encoding="utf-8"))
    sources = [source for source in config["sources"] if source["platform"] in platforms]
    conn = connect(args.db)
    load_sources(conn, args.config)
    session = f"sourceflow-{run_id}-{os.getpid()}"
    browser = BrowserAct(args.browser_id, session)
    results: list[SourceResult] = []
    try:
        for source in sources:
            pending_rows = due_pending_items(conn, source["key"], started_at)
            if source["platform"] == "x":
                result = collect_x(browser, conn, source, started_at,
                                   _scroll_limit(conn, source["key"], args.max_scrolls))
            elif source["platform"] == "xiaohongshu":
                result = collect_xiaohongshu(browser, conn, source, started_at, pending_rows)
            elif source["platform"] == "bilibili":
                result = collect_bilibili(browser, conn, source, started_at,
                                          ROOT / "data" / "raw" / "bilibili", pending_rows)
            else:
                continue
            results.append(result)
    finally:
        browser.close()

    candidates_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for result in results:
        for item in result.items:
            candidates_by_key[(item["platform"], item["item_type"], item["external_id"])] = item
    candidates = list(candidates_by_key.values())
    import_result: dict[str, Any] = {"inserted": 0, "updated": 0, "duplicate": 0, "older": 0,
                                     "inserted_keys": [], "updated_keys": []}
    if not args.dry_run:
        try:
            import_result = insert_candidates(conn, candidates, commit=False) if candidates else import_result
            for result in results:
                for pending in result.pending or []:
                    save_pending_item(conn, result.source_key, pending["external_id"], pending["url"],
                                      pending["payload"], pending["reason"], started_at, pending["retry_after"])
                for external_id in result.resolved_pending or []:
                    clear_pending_item(conn, result.source_key, external_id)
                unresolved = conn.execute(
                    "SELECT COUNT(*) FROM pending_items WHERE source_key=?", (result.source_key,)
                ).fetchone()[0]
                result.pending_total = unresolved
                if unresolved and result.status == "complete":
                    result.status = "partial"
                    result.stop_reason = f"{result.stop_reason},pending_items" if result.stop_reason else "pending_items"
                record_source_check(conn, run_id, result.source_key, result.status, started_at,
                                    result.pages_checked, result.oldest_checked_at,
                                    result.stop_reason, len(result.items))
                if result.status == "complete":
                    update_source_cursor(
                        conn,
                        result.source_key,
                        result.cursor_id,
                        result.cursor_at,
                        started_at,
                    )
                if result.platform == "bilibili":
                    for skipped in result.skipped:
                        reason = str(skipped.get("reason") or "skipped")
                        if skipped.get("bvid") and not reason.startswith("extract_failed"):
                            record_source_seen(
                                conn, result.source_key, skipped["bvid"], reason, started_at,
                                {"url": skipped.get("url"), "title": skipped.get("title")},
                            )
                    for external_id in result.resolved_pending or []:
                        record_source_seen(conn, result.source_key, external_id, "resolved", started_at)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    actual_keys = set(import_result["inserted_keys"]) | set(import_result["updated_keys"])
    actual_items = [item for item in candidates
                    if (item["platform"], item["item_type"], item["external_id"]) in actual_keys]
    for platform in platforms:
        platform_items = [item for item in candidates if item["platform"] == platform]
        _write_json(
            output_dir / f"{platform}.json",
            {"observed_at": started_at, "platform": platform, "items": platform_items},
        )
    _write_json(output_dir / "new_items.json", {"observed_at": started_at, "items": actual_items})

    finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    platform_stats: dict[str, dict[str, int]] = {}
    for platform in sorted(platforms):
        subset = [result for result in results if result.platform == platform]
        platform_stats[platform] = {
            "sources": len(subset),
            "success": sum(result.status == "complete" for result in subset),
            "partial": sum(result.status == "partial" for result in subset),
            "failed": sum(result.status == "failed" for result in subset),
            "candidates": sum(len(result.items) for result in subset),
            "new": sum(item["platform"] == platform for item in actual_items),
            "skipped": sum(len(result.skipped) for result in subset),
        }
    db_counts = {key: value for key, value in import_result.items() if not key.endswith("_keys")}
    report = {
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": round(time.monotonic() - started, 2),
        "output_dir": str(output_dir),
        "new_items_file": str(output_dir / "new_items.json"),
        "run_file": str(output_dir / "run.json"),
        "dry_run": args.dry_run,
        "platforms": platform_stats,
        "database": db_counts,
        "sources": [
            {
                "source_key": result.source_key,
                "status": result.status,
                "new": sum(item["source_key"] == result.source_key for item in actual_items),
                "candidates": len(result.items),
                "skipped": len(result.skipped),
                "pages_checked": result.pages_checked,
                "oldest_checked_at": result.oldest_checked_at,
                "stop_reason": result.stop_reason,
                "pending": result.pending_total if not args.dry_run else len(result.pending or []),
                "error": result.error,
            }
            for result in results
        ],
    }
    _write_json(output_dir / "run.json", report)
    conn.close()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Script-first SourceFlow browser collector")
    parser.add_argument("--platforms", default="x,xiaohongshu,bilibili")
    parser.add_argument("--date", help="Run date used for output directory, YYYY-MM-DD")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--browser-id", default=os.getenv("SOURCEFLOW_BROWSER_ID"))
    parser.add_argument("--max-scrolls", type=int, default=2)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    report = run(args)
    print(json.dumps({
        "duration_seconds": report["duration_seconds"],
        "run_file": report["run_file"],
        "new_items_file": report["new_items_file"],
        "platforms": report["platforms"],
        "database": report["database"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
