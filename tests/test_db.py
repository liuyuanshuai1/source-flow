import tempfile
import unittest
from pathlib import Path

from sourceflow.db import (connect, due_pending_items, insert_candidates, latest_time,
                           load_sources, record_source_seen, seen_external_ids)


TEST_CONFIG = Path(__file__).parent / "fixtures/sources.json"


class DatabaseTests(unittest.TestCase):
    def test_sources_and_candidate_deduplication(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = connect(Path(directory) / "sourceflow.sqlite3")
            self.assertEqual(load_sources(conn, TEST_CONFIG), 2)
            candidate = {
                "source_key": "x:example",
                "platform": "x",
                "item_type": "post",
                "external_id": "example-post",
                "url": "https://x.com/example/status/example-post",
                "published_at": "2026-09-19T00:00:00Z",
                "event_at": "2026-09-19T00:00:00Z",
                "observed_at": "2026-09-19T01:00:00Z",
                "content_text": "Example post",
            }
            self.assertEqual(insert_candidates(conn, [candidate])["inserted"], 1)
            self.assertEqual(insert_candidates(conn, [candidate])["duplicate"], 1)
            self.assertEqual(latest_time(conn, "x:example"), "2026-09-19T00:00:00Z")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM items").fetchone()[0], 1)

    def test_seen_items_are_recorded_per_source(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = connect(Path(directory) / "sourceflow.sqlite3")
            load_sources(conn, TEST_CONFIG)
            record_source_seen(conn, "bilibili:12345678", "BVEXAMPLE", "no_subtitle", "2026-09-19T01:00:00Z")
            conn.commit()
            self.assertEqual(seen_external_ids(conn, "bilibili:12345678"), {"BVEXAMPLE"})

    def test_late_visible_item_is_inserted_below_latest_time(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = connect(Path(directory) / "sourceflow.sqlite3")
            load_sources(conn, TEST_CONFIG)
            base = {"source_key": "x:example", "platform": "x", "item_type": "post",
                    "url": "https://x.com/example/status/id", "observed_at": "2026-09-20T00:00:00Z",
                    "content_text": "text"}
            newest = {**base, "external_id": "newest", "event_at": "2026-09-19T00:00:00Z"}
            late = {**base, "external_id": "late", "event_at": "2026-09-10T00:00:00Z"}
            self.assertEqual(insert_candidates(conn, [newest])["inserted"], 1)
            result = insert_candidates(conn, [late])
            self.assertEqual(result["inserted"], 1)
            self.assertEqual(result["inserted_keys"], [("x", "post", "late")])

    def test_legacy_no_subtitle_row_is_migrated_once_to_retry_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = connect(Path(directory) / "sourceflow.sqlite3")
            load_sources(conn, TEST_CONFIG)
            record_source_seen(conn, "bilibili:12345678", "BVLEGACY", "no_subtitle",
                               "2026-09-19T01:00:00Z", {"title": "Legacy"})
            conn.commit()
            load_sources(conn, TEST_CONFIG)
            load_sources(conn, TEST_CONFIG)
            due = due_pending_items(conn, "bilibili:12345678", "2026-09-20T01:00:00Z")
            self.assertEqual(len(due), 1)
            self.assertEqual(due[0]["bvid"], "BVLEGACY")
            self.assertEqual(due[0]["url"], "https://www.bilibili.com/video/BVLEGACY/")


if __name__ == "__main__":
    unittest.main()
