"""Small SQLite store for source-specific incremental collection."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data/sourceflow.sqlite3"
DEFAULT_CONFIG = ROOT / "config/sources.json"


def utc_time(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"time has no timezone: {value}")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def connect(path: Path = DEFAULT_DB) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sources (
            source_key TEXT PRIMARY KEY,
            platform TEXT NOT NULL,
            external_id TEXT NOT NULL,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            since_at TEXT NOT NULL,
            UNIQUE(platform, external_id)
        );
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY,
            source_key TEXT NOT NULL REFERENCES sources(source_key),
            platform TEXT NOT NULL,
            item_type TEXT NOT NULL,
            external_id TEXT NOT NULL,
            url TEXT NOT NULL,
            title TEXT,
            content_text TEXT,
            published_at TEXT,
            event_at TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            content_status TEXT NOT NULL,
            raw_ref TEXT,
            metadata_json TEXT NOT NULL,
            UNIQUE(platform, item_type, external_id)
        );
        CREATE INDEX IF NOT EXISTS idx_items_source_event ON items(source_key, event_at);
        CREATE TABLE IF NOT EXISTS source_cursors (
            source_key TEXT PRIMARY KEY REFERENCES sources(source_key),
            last_seen_external_id TEXT,
            last_seen_at TEXT,
            last_checked_at TEXT NOT NULL,
            coverage_verified INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS source_seen (
            source_key TEXT NOT NULL REFERENCES sources(source_key),
            external_id TEXT NOT NULL,
            status TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            PRIMARY KEY(source_key, external_id)
        );
        CREATE TABLE IF NOT EXISTS pending_items (
            source_key TEXT NOT NULL REFERENCES sources(source_key),
            external_id TEXT NOT NULL,
            url TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            reason TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_attempt_at TEXT NOT NULL,
            retry_after TEXT NOT NULL,
            PRIMARY KEY(source_key, external_id)
        );
        CREATE TABLE IF NOT EXISTS source_checks (
            run_id TEXT NOT NULL,
            source_key TEXT NOT NULL REFERENCES sources(source_key),
            status TEXT NOT NULL,
            checked_at TEXT NOT NULL,
            pages_checked INTEGER NOT NULL,
            oldest_checked_at TEXT,
            stop_reason TEXT,
            candidate_count INTEGER NOT NULL,
            PRIMARY KEY(run_id, source_key)
        );
        """
    )
    if "raw_ref" not in {row["name"] for row in conn.execute("PRAGMA table_info(items)")}:
        conn.execute("ALTER TABLE items ADD COLUMN raw_ref TEXT")
    if "coverage_verified" not in {row["name"] for row in conn.execute("PRAGMA table_info(source_cursors)")}:
        conn.execute("ALTER TABLE source_cursors ADD COLUMN coverage_verified INTEGER NOT NULL DEFAULT 0")
    return conn


def source_cursor(conn: sqlite3.Connection, source_key: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT last_seen_external_id, last_seen_at, last_checked_at, coverage_verified FROM source_cursors WHERE source_key=?",
        (source_key,),
    ).fetchone()
    if row:
        return dict(row)
    latest = conn.execute(
        """SELECT external_id, event_at
           FROM items WHERE source_key=? ORDER BY event_at DESC, id DESC LIMIT 1""",
        (source_key,),
    ).fetchone()
    return {
        "last_seen_external_id": latest["external_id"] if latest else None,
        "last_seen_at": latest["event_at"] if latest else None,
        "last_checked_at": None,
        "coverage_verified": 0,
    }


def update_source_cursor(
    conn: sqlite3.Connection,
    source_key: str,
    external_id: str | None,
    seen_at: str | None,
    checked_at: str,
) -> None:
    conn.execute(
        """INSERT INTO source_cursors(source_key, last_seen_external_id, last_seen_at, last_checked_at, coverage_verified)
           VALUES (?, ?, ?, ?, 1)
           ON CONFLICT(source_key) DO UPDATE SET
             last_seen_external_id=excluded.last_seen_external_id,
             last_seen_at=excluded.last_seen_at,
             last_checked_at=excluded.last_checked_at,
             coverage_verified=1""",
        (source_key, external_id, utc_time(seen_at) if seen_at else None, utc_time(checked_at)),
    )


def existing_external_ids(conn: sqlite3.Connection, source_key: str) -> set[str]:
    return {
        row["external_id"]
        for row in conn.execute("SELECT external_id FROM items WHERE source_key=?", (source_key,))
    }


def seen_external_ids(conn: sqlite3.Connection, source_key: str) -> set[str]:
    return {
        row["external_id"]
        for row in conn.execute("SELECT external_id FROM source_seen WHERE source_key=?", (source_key,))
    }


def due_pending_items(conn: sqlite3.Connection, source_key: str, now: str) -> list[dict[str, Any]]:
    return [
        {
            "external_id": row["external_id"],
            "url": row["url"],
            **json.loads(row["payload_json"]),
        }
        for row in conn.execute(
            "SELECT external_id, url, payload_json FROM pending_items WHERE source_key=? AND retry_after<=? ORDER BY first_seen_at",
            (source_key, utc_time(now)),
        )
    ]


def save_pending_item(
    conn: sqlite3.Connection,
    source_key: str,
    external_id: str,
    url: str,
    payload: dict[str, Any],
    reason: str,
    observed_at: str,
    retry_after: str,
) -> None:
    now = utc_time(observed_at)
    conn.execute(
        """INSERT INTO pending_items(source_key, external_id, url, payload_json, reason,
           first_seen_at, last_attempt_at, retry_after) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(source_key, external_id) DO UPDATE SET
             url=excluded.url, payload_json=excluded.payload_json, reason=excluded.reason,
             last_attempt_at=excluded.last_attempt_at, retry_after=excluded.retry_after""",
        (source_key, external_id, url, json.dumps(payload, ensure_ascii=False, sort_keys=True),
         reason, now, now, utc_time(retry_after)),
    )


def clear_pending_item(conn: sqlite3.Connection, source_key: str, external_id: str) -> None:
    conn.execute("DELETE FROM pending_items WHERE source_key=? AND external_id=?", (source_key, external_id))


def record_source_check(
    conn: sqlite3.Connection,
    run_id: str,
    source_key: str,
    status: str,
    checked_at: str,
    pages_checked: int,
    oldest_checked_at: str | None,
    stop_reason: str | None,
    candidate_count: int,
) -> None:
    conn.execute(
        """INSERT INTO source_checks(run_id, source_key, status, checked_at, pages_checked,
           oldest_checked_at, stop_reason, candidate_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (run_id, source_key, status, utc_time(checked_at), pages_checked,
         utc_time(oldest_checked_at) if oldest_checked_at else None, stop_reason, candidate_count),
    )


def record_source_seen(
    conn: sqlite3.Connection,
    source_key: str,
    external_id: str,
    status: str,
    observed_at: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    timestamp = utc_time(observed_at)
    conn.execute(
        """INSERT INTO source_seen(source_key, external_id, status, first_seen_at, last_seen_at, metadata_json)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(source_key, external_id) DO UPDATE SET
             status=excluded.status,
             last_seen_at=excluded.last_seen_at,
             metadata_json=excluded.metadata_json""",
        (source_key, external_id, status, timestamp, timestamp, json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)),
    )


def load_sources(conn: sqlite3.Connection, config_path: Path = DEFAULT_CONFIG) -> int:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    since_at = utc_time(config["since_at"])
    for source in config["sources"]:
        conn.execute(
            """INSERT INTO sources(source_key, platform, external_id, name, url, since_at)
               VALUES (:key, :platform, :external_id, :name, :url, :since_at)
               ON CONFLICT(source_key) DO UPDATE SET name=excluded.name, url=excluded.url""",
            {**source, "since_at": since_at},
        )
    # Older runs stored unavailable subtitles only in source_seen. Move those
    # stable video IDs into the retry queue without copying browser credentials.
    for row in conn.execute(
        """SELECT s.source_key, s.external_id, s.status, s.first_seen_at, s.last_seen_at,
                  s.metadata_json
           FROM source_seen s JOIN sources src ON src.source_key=s.source_key
           WHERE src.platform='bilibili' AND s.status IN ('no_subtitle', 'chinese_subtitle_missing')
             AND NOT EXISTS (SELECT 1 FROM pending_items p
                             WHERE p.source_key=s.source_key AND p.external_id=s.external_id)"""
    ).fetchall():
        metadata = json.loads(row["metadata_json"])
        url = metadata.get("url") or f"https://www.bilibili.com/video/{row['external_id']}/"
        save_pending_item(
            conn, row["source_key"], row["external_id"], url,
            {"bvid": row["external_id"], "title": metadata.get("title")},
            row["status"], row["first_seen_at"], row["last_seen_at"],
        )
    conn.commit()
    return len(config["sources"])


def latest_time(conn: sqlite3.Connection, source_key: str) -> str:
    row = conn.execute(
        """SELECT s.since_at, MAX(i.event_at) AS newest
           FROM sources s LEFT JOIN items i ON i.source_key = s.source_key
           WHERE s.source_key = ? GROUP BY s.source_key""",
        (source_key,),
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown source: {source_key}")
    return max(row["since_at"], row["newest"] or row["since_at"])


def insert_candidates(
    conn: sqlite3.Connection, candidates: list[dict[str, Any]], *, commit: bool = True
) -> dict[str, Any]:
    counts: dict[str, Any] = {"inserted": 0, "updated": 0, "duplicate": 0, "older": 0,
                              "inserted_keys": [], "updated_keys": []}
    ordered = sorted(candidates, key=lambda item: utc_time(item["event_at"]))
    for item in ordered:
        source = conn.execute("SELECT platform, since_at FROM sources WHERE source_key = ?", (item["source_key"],)).fetchone()
        if source is None:
            raise ValueError(f"unknown source: {item['source_key']}")
        if source["platform"] != item["platform"]:
            raise ValueError(f"platform mismatch: {item['source_key']}")
        existing = conn.execute(
            "SELECT id, content_text, content_status FROM items WHERE platform=? AND item_type=? AND external_id=?",
            (item["platform"], item["item_type"], item["external_id"]),
        ).fetchone()
        observed_at = utc_time(item["observed_at"])
        event_at = utc_time(item["event_at"])
        baseline = bool(item.get("baseline_before_since"))
        if not existing and event_at < source["since_at"] and not baseline:
            counts["older"] += 1
            continue
        published_at = utc_time(item["published_at"]) if item.get("published_at") else None
        raw_ref = item.get("raw_ref")
        content_text = item.get("content_text")
        if raw_ref:
            raw_path = (ROOT / raw_ref).resolve()
            if not raw_path.is_relative_to(ROOT):
                raise ValueError(f"raw_ref outside project: {raw_ref}")
            raw = json.loads(raw_path.read_text(encoding="utf-8"))
            if item["platform"] == "bilibili" and raw.get("bvid") != item["external_id"].split(":")[0]:
                raise ValueError(f"Bilibili raw_ref does not match item: {raw_ref}")
            if content_text is None:
                content_text = raw.get("transcript") or ""
        if existing:
            if content_text and content_text != existing["content_text"] and existing["content_status"] != "complete":
                conn.execute(
                    """UPDATE items SET content_text=?, content_status=?, title=?, raw_ref=?,
                       published_at=?, last_seen_at=?, metadata_json=? WHERE id=?""",
                    (content_text, item.get("content_status", "complete"), item.get("title"), raw_ref,
                     published_at, observed_at,
                     json.dumps(item.get("metadata", {}), ensure_ascii=False, sort_keys=True), existing["id"]),
                )
                counts["updated"] += 1
                counts["updated_keys"].append((item["platform"], item["item_type"], item["external_id"]))
            else:
                conn.execute("UPDATE items SET last_seen_at=? WHERE id=?", (observed_at, existing["id"]))
                counts["duplicate"] += 1
            continue
        conn.execute(
            """INSERT INTO items(source_key, platform, item_type, external_id, url, title,
               content_text, published_at, event_at, first_seen_at, last_seen_at,
               content_status, raw_ref, metadata_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                item["source_key"], item["platform"], item["item_type"], item["external_id"],
                item["url"], item.get("title"), content_text, published_at,
                event_at, observed_at, observed_at, item.get("content_status", "complete"),
                raw_ref,
                json.dumps({**item.get("metadata", {}), "baseline_before_since": baseline}, ensure_ascii=False, sort_keys=True),
            ),
        )
        counts["inserted"] += 1
        counts["inserted_keys"].append((item["platform"], item["item_type"], item["external_id"]))
    if commit:
        conn.commit()
    return counts
