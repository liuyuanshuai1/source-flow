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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from sourceflow.db import (
    DEFAULT_CONFIG,
    DEFAULT_DB,
    ROOT,
    connect,
    existing_external_ids,
    insert_candidates,
    latest_time,
    load_sources,
    record_source_seen,
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
    cutoff = latest_time(conn, key)
    existing = existing_external_ids(conn, key)
    cursor = source_cursor(conn, key)["last_seen_external_id"]
    rows_by_id: dict[str, dict[str, Any]] = {}
    try:
        for suffix in ("", "/with_replies"):
            browser.navigate(f"https://x.com/{handle}{suffix}")
            for scroll_index in range(max_scrolls + 1):
                rows = _eval_after_ready(browser, SCRIPTS / "extract-x-timeline.js", "article[data-testid='tweet']")
                for row in rows:
                    if row.get("handle", "").lower() == handle.lower():
                        rows_by_id[row["id"]] = row
                timestamps = [row.get("published_at") for row in rows if row.get("published_at")]
                ids = {row.get("id") for row in rows}
                reached_boundary = cursor in ids or any(not _newer(stamp, cutoff) for stamp in timestamps)
                if reached_boundary or scroll_index == max_scrolls:
                    break
                browser.scroll()
                browser.wait_stable(3500)
    except Exception as exc:
        return SourceResult(key, "x", "failed", [], [], None, None, str(exc))

    ordered = sorted(rows_by_id.values(), key=lambda row: utc_time(row["published_at"]), reverse=True)
    items = []
    for row in ordered:
        if row["id"] in existing or not _newer(row.get("published_at"), cutoff):
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
        "success",
        items,
        [],
        newest["id"] if newest else cursor,
        newest.get("published_at") if newest else source_cursor(conn, key)["last_seen_at"],
    )


def collect_xiaohongshu(
    browser: BrowserAct,
    conn: Any,
    source: dict[str, Any],
    observed_at: str,
) -> SourceResult:
    key = source["key"]
    author_id = source["external_id"]
    cutoff = latest_time(conn, key)
    existing = existing_external_ids(conn, key)
    cursor = source_cursor(conn, key)
    try:
        browser.navigate(source["url"])
        rows = _eval_after_ready(
            browser,
            SCRIPTS / "extract-xiaohongshu-space.js",
            "a[href*='/explore/'], a[href*='/user/profile/']",
        )
    except Exception as exc:
        return SourceResult(key, "xiaohongshu", "failed", [], [], None, None, str(exc))

    rows = [row for row in rows if row.get("author") == author_id and row.get("id") and row.get("time")]
    ordered = sorted(rows, key=lambda row: int(row["time"]), reverse=True)
    items: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for row in ordered:
        published_at = _iso_from_millis(row.get("time"))
        if row["id"] in existing or not _newer(published_at, cutoff):
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
    newest = ordered[0] if ordered else None
    return SourceResult(
        key,
        "xiaohongshu",
        "success",
        items,
        warnings,
        newest["id"] if newest else cursor["last_seen_external_id"],
        _iso_from_millis(newest.get("time")) if newest else cursor["last_seen_at"],
    )


def collect_bilibili(
    browser: BrowserAct,
    conn: Any,
    source: dict[str, Any],
    observed_at: str,
    raw_dir: Path,
) -> SourceResult:
    key = source["key"]
    expected_mid = str(source["external_id"])
    cutoff = latest_time(conn, key)
    cursor = source_cursor(conn, key)
    cursor_bvid = (cursor["last_seen_external_id"] or "").split(":", 1)[0]
    existing_bvids = {value.split(":", 1)[0] for value in existing_external_ids(conn, key)}
    seen_bvids = seen_external_ids(conn, key)
    try:
        browser.navigate(source["url"])
        rows = _eval_after_ready(browser, SCRIPTS / "extract-bilibili-space.js", "a[href*='/video/BV']")
    except Exception as exc:
        return SourceResult(key, "bilibili", "failed", [], [], None, None, str(exc))

    candidates = []
    for row in rows:
        bvid = row.get("bvid")
        if not bvid:
            continue
        if bvid == cursor_bvid or bvid in existing_bvids:
            break
        if bvid in seen_bvids:
            continue
        candidates.append(row)

    items: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    newest_owner_id = cursor_bvid or None
    newest_owner_at = cursor["last_seen_at"]
    for row in candidates:
        bvid = row["bvid"]
        try:
            browser.navigate(row["url"])
            raw = _eval_after_ready(browser, SCRIPTS / "bilibili-subtitle-source.js", "body")
            if raw.get("status") == "signed_player_request_missing":
                browser.wait_stable(5000)
                raw = browser.eval_file(SCRIPTS / "bilibili-subtitle-source.js")
        except Exception as exc:
            skipped.append({"bvid": bvid, "url": row["url"], "reason": f"extract_failed: {exc}"})
            continue
        published_at = _iso_from_seconds(raw.get("pubdate"))
        owner_mid = str((raw.get("owner") or {}).get("mid") or "")
        if owner_mid == expected_mid and published_at and (
            newest_owner_at is None or utc_time(published_at) > utc_time(newest_owner_at)
        ):
            newest_owner_id = bvid
            newest_owner_at = published_at
        reason = None
        if owner_mid != expected_mid:
            reason = f"owner_mismatch:{owner_mid or 'unknown'}"
        elif not _newer(published_at, cutoff):
            reason = "not_newer_than_cutoff"
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
    return SourceResult(
        key,
        "bilibili",
        "success",
        items,
        skipped,
        newest_owner_id or cursor["last_seen_external_id"],
        newest_owner_at,
    )


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def run(args: argparse.Namespace) -> dict[str, Any]:
    started = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    run_date = args.date or datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    output_dir = args.output_dir or ROOT / "data" / "raw" / "runs" / run_date
    platforms = {value.strip() for value in args.platforms.split(",") if value.strip()}
    config = json.loads(args.config.read_text(encoding="utf-8"))
    sources = [source for source in config["sources"] if source["platform"] in platforms]
    conn = connect(args.db)
    load_sources(conn, args.config)
    session = f"sourceflow-{run_date.replace('-', '')}-{os.getpid()}"
    browser = BrowserAct(args.browser_id, session)
    results: list[SourceResult] = []
    try:
        for source in sources:
            if source["platform"] == "x":
                result = collect_x(browser, conn, source, started_at, args.max_scrolls)
            elif source["platform"] == "xiaohongshu":
                result = collect_xiaohongshu(browser, conn, source, started_at)
            elif source["platform"] == "bilibili":
                result = collect_bilibili(browser, conn, source, started_at, ROOT / "data" / "raw" / "bilibili")
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
    import_result = {"inserted": 0, "duplicate": 0, "older": 0}
    if not args.dry_run:
        try:
            import_result = insert_candidates(conn, candidates) if candidates else import_result
            for result in results:
                if result.status == "success":
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
                                    conn,
                                    result.source_key,
                                    skipped["bvid"],
                                    reason,
                                    started_at,
                                    {"url": skipped.get("url"), "title": skipped.get("title")},
                                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    for platform in platforms:
        platform_items = [item for item in candidates if item["platform"] == platform]
        _write_json(
            output_dir / f"{platform}.json",
            {"observed_at": started_at, "platform": platform, "items": platform_items},
        )
    _write_json(output_dir / "new_items.json", {"observed_at": started_at, "items": candidates})

    finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    platform_stats: dict[str, dict[str, int]] = {}
    for platform in sorted(platforms):
        subset = [result for result in results if result.platform == platform]
        platform_stats[platform] = {
            "sources": len(subset),
            "success": sum(result.status == "success" for result in subset),
            "failed": sum(result.status != "success" for result in subset),
            "candidates": sum(len(result.items) for result in subset),
            "skipped": sum(len(result.skipped) for result in subset),
        }
    report = {
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": round(time.monotonic() - started, 2),
        "output_dir": str(output_dir),
        "new_items_file": str(output_dir / "new_items.json"),
        "platforms": platform_stats,
        "database": import_result,
        "sources": [
            {
                "source_key": result.source_key,
                "status": result.status,
                "new": len(result.items),
                "skipped": len(result.skipped),
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
        "new_items_file": report["new_items_file"],
        "platforms": report["platforms"],
        "database": report["database"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
