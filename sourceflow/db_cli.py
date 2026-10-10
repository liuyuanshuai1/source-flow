from __future__ import annotations

import argparse
import json
from pathlib import Path

from sourceflow.db import DEFAULT_DB, connect, insert_candidates, latest_time, load_sources


def main() -> None:
    parser = argparse.ArgumentParser(description="SourceFlow local SQLite store")
    parser.add_argument("command", choices=["init", "import", "latest", "summary"])
    parser.add_argument("argument", nargs="?", help="JSON file for import, source key for latest")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = parser.parse_args()
    conn = connect(args.db)
    try:
        if args.command == "init":
            print(f"sources={load_sources(conn)} db={args.db}")
        elif args.command == "import":
            if not args.argument:
                parser.error("import requires a JSON file")
            raw = json.loads(Path(args.argument).read_text(encoding="utf-8"))
            print(json.dumps(insert_candidates(conn, raw["items"]), ensure_ascii=False))
        elif args.command == "latest":
            if not args.argument:
                parser.error("latest requires a source key")
            print(latest_time(conn, args.argument))
        else:
            for row in conn.execute(
                """SELECT s.source_key, s.name, s.since_at, COUNT(i.id) AS items,
                   COALESCE(MAX(i.event_at), s.since_at) AS newest
                   FROM sources s LEFT JOIN items i ON i.source_key=s.source_key
                   GROUP BY s.source_key ORDER BY s.platform, s.name"""
            ):
                print(f"{row['source_key']}\t{row['name']}\t{row['items']}\t{max(row['since_at'], row['newest'])}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
