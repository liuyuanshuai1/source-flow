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
            last_checked_at TEXT NOT NULL
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
        """
    )
    if "raw_ref" not in {row["name"] for row in conn.execute("PRAGMA table_info(items)")}:
        conn.execute("ALTER TABLE items ADD COLUMN raw_ref TEXT")
    return conn


def source_cursor(conn: sqlite3.Connection, source_key: str) -> dict[str, str | None]:
    row = conn.execute(
        "SELECT last_seen_external_id, last_seen_at, last_checked_at FROM source_cursors WHERE source_key=?",
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
    }


def update_source_cursor(
    conn: sqlite3.Connection,
    source_key: str,
    external_id: str | None,
    seen_at: str | None,
    checked_at: str,
) -> None:
    conn.execute(
        """INSERT INTO source_cursors(source_key, last_seen_external_id, last_seen_at, last_checked_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(source_key) DO UPDATE SET
             last_seen_external_id=excluded.last_seen_external_id,
             last_seen_at=excluded.last_seen_at,
             last_checked_at=excluded.last_checked_at""",
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


def insert_candidates(conn: sqlite3.Connection, candidates: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"inserted": 0, "duplicate": 0, "older": 0}
    cutoffs = {item["source_key"]: latest_time(conn, item["source_key"]) for item in candidates}
    # Compare every candidate with the cutoff at the start of this batch.
    ordered = sorted(candidates, key=lambda item: utc_time(item["event_at"]))
    for item in ordered:
        source = conn.execute("SELECT platform FROM sources WHERE source_key = ?", (item["source_key"],)).fetchone()
        if source is None:
            raise ValueError(f"unknown source: {item['source_key']}")
        if source["platform"] != item["platform"]:
            raise ValueError(f"platform mismatch: {item['source_key']}")
        existing = conn.execute(
            "SELECT id FROM items WHERE platform=? AND item_type=? AND external_id=?",
            (item["platform"], item["item_type"], item["external_id"]),
        ).fetchone()
        observed_at = utc_time(item["observed_at"])
        if existing:
            conn.execute("UPDATE items SET last_seen_at=? WHERE id=?", (observed_at, existing["id"]))
            counts["duplicate"] += 1
            continue
        event_at = utc_time(item["event_at"])
        baseline = bool(item.get("baseline_before_since"))
        existing_for_source = conn.execute(
            "SELECT 1 FROM items WHERE source_key=? LIMIT 1", (item["source_key"],)
        ).fetchone()
        if event_at <= cutoffs[item["source_key"]] and not (baseline and not existing_for_source):
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
    conn.commit()
    return counts
