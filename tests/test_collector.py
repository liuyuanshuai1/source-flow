import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sourceflow.collector import BrowserAct, BrowserError, collect_bilibili, collect_x, run
from sourceflow.db import (connect, due_pending_items, load_sources, record_source_seen,
                           save_pending_item, source_cursor, update_source_cursor)


TEST_CONFIG = Path(__file__).parent / "fixtures/sources.json"


class FakeBrowser:
    def __init__(self, pages):
        self.pages = pages
        self.url = ""
        self.subtitle_calls = 0

    def navigate(self, url):
        self.url = url

    def eval_file(self, path):
        name = path.name
        if name == "extract-x-timeline.js":
            return self.pages[self.url]
        if name == "extract-bilibili-space.js":
            return self.pages["space"]
        if name == "bilibili-subtitle-source.js":
            self.subtitle_calls += 1
            return self.pages[self.url]
        raise AssertionError(name)

    def wait_selector(self, selector, timeout_ms=6000):
        pass

    def wait_stable(self, timeout_ms=5000):
        pass

    def scroll(self, amount=1500):
        pass

    def close(self):
        pass


class CollectorTests(unittest.TestCase):
    def test_browser_reconnects_once_after_health_check_failure(self):
        browser = object.__new__(BrowserAct)
        browser.browser_id = "browser"
        browser.session = "owned-session"
        browser.opened = True
        calls = []

        def fake_run(args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                raise BrowserError("Error 210101: Connection health check failed")
            return ""

        browser._run = fake_run
        browser.navigate("https://example.com")
        self.assertEqual(calls[1], ["session", "close", "owned-session"])
        self.assertEqual(calls[2][:4], ["--session", "owned-session", "browser", "open"])

    def test_source_cursor_persists_skipped_item(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = connect(Path(directory) / "sourceflow.sqlite3")
            load_sources(conn, TEST_CONFIG)
            update_source_cursor(conn, "bilibili:12345678", "BVNEW", "2026-09-19T00:00:00Z", "2026-09-19T01:00:00Z")
            conn.commit()
            cursor = source_cursor(conn, "bilibili:12345678")
            self.assertEqual(cursor["last_seen_external_id"], "BVNEW")
            self.assertEqual(cursor["last_seen_at"], "2026-09-19T00:00:00Z")

    def test_x_filters_old_and_duplicate_rows_without_agent_judgment(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = connect(Path(directory) / "sourceflow.sqlite3")
            load_sources(conn, TEST_CONFIG)
            row_new = {"handle": "example", "id": "new", "url": "https://x.com/example/status/new", "published_at": "2026-09-19T00:00:00Z", "text": "new", "timeline": "posts"}
            row_old = {"handle": "example", "id": "old", "url": "https://x.com/example/status/old", "published_at": "2026-08-31T00:00:00Z", "text": "old", "timeline": "posts"}
            pages = {
                "https://x.com/example": [row_new, row_old],
                "https://x.com/example/with_replies": [{**row_new, "timeline": "with_replies"}, row_old],
            }
            source = {"key": "x:example", "external_id": "example"}
            result = collect_x(FakeBrowser(pages), conn, source, "2026-09-19T01:00:00Z", 0)
            self.assertEqual(result.status, "complete")
            self.assertEqual([item["external_id"] for item in result.items], ["new"])

    def test_x_page_limit_is_partial_and_does_not_claim_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = connect(Path(directory) / "sourceflow.sqlite3")
            load_sources(conn, TEST_CONFIG)
            row = {"handle": "example", "id": "new", "url": "https://x.com/example/status/new",
                   "published_at": "2026-09-19T00:00:00Z", "text": "new", "timeline": "posts"}
            browser = FakeBrowser({"https://x.com/example": [row],
                                   "https://x.com/example/with_replies": [row]})
            result = collect_x(browser, conn, {"key": "x:example", "external_id": "example"},
                               "2026-09-19T01:00:00Z", 0)
            self.assertEqual(result.status, "partial")
            self.assertIn("page_limit", result.stop_reason)
            self.assertEqual([item["external_id"] for item in result.items], ["new"])

    def test_bilibili_no_subtitle_is_scheduled_for_later_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = connect(Path(directory) / "sourceflow.sqlite3")
            load_sources(conn, TEST_CONFIG)
            pages = {
                "space": [
                    {"bvid": "BVNEW", "url": "https://www.bilibili.com/video/BVNEW/", "title": "new"},
                ],
                "https://www.bilibili.com/video/BVNEW/": {
                    "bvid": "BVNEW", "cid": 1, "url": "https://www.bilibili.com/video/BVNEW/",
                    "title": "new", "pubdate": 1789776000, "duration": 60,
                    "owner": {"mid": 12345678, "name": "Example"}, "status": "no_subtitle",
                },
            }
            browser = FakeBrowser(pages)
            source = {"key": "bilibili:12345678", "external_id": "12345678", "url": "https://space.bilibili.com/12345678/video"}
            result = collect_bilibili(browser, conn, source, "2026-09-19T01:00:00Z", Path(directory))
            self.assertEqual(result.items, [])
            self.assertEqual(result.skipped[0]["reason"], "no_subtitle")
            self.assertEqual(result.status, "partial")
            self.assertEqual(result.pending[0]["external_id"], "BVNEW")

            pending = result.pending[0]
            save_pending_item(conn, result.source_key, pending["external_id"], pending["url"],
                              pending["payload"], pending["reason"], "2026-09-19T01:00:00Z", pending["retry_after"])
            record_source_seen(conn, result.source_key, "BVNEW", "no_subtitle", "2026-09-19T01:00:00Z")
            conn.commit()
            self.assertEqual(due_pending_items(conn, result.source_key, "2026-09-20T01:00:00Z"), [])
            second_browser = FakeBrowser(pages)
            second = collect_bilibili(second_browser, conn, source, "2026-09-20T01:00:00Z", Path(directory))
            self.assertEqual(second.items, [])
            self.assertEqual(second_browser.subtitle_calls, 0)
            self.assertEqual(len(due_pending_items(conn, result.source_key, "2026-09-27T01:00:00Z")), 1)

    def test_repeated_run_writes_distinct_reports_and_only_inserted_items(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "sources.json"
            config.write_text(json.dumps({"since_at": "2026-09-01T00:00:00Z", "sources": [
                {"key": "x:example", "platform": "x", "external_id": "example", "name": "Example",
                 "url": "https://x.com/example"}]}), encoding="utf-8")
            new = {"handle": "example", "id": "new", "url": "https://x.com/example/status/new",
                   "published_at": "2026-09-19T00:00:00Z", "text": "new", "timeline": "posts"}
            old = {**new, "id": "old", "published_at": "2026-08-31T00:00:00Z"}
            pages = {"https://x.com/example": [new],
                     "https://x.com/example/with_replies": [new]}
            args = argparse.Namespace(date="2026-09-19", output_dir=None, platforms="x", config=config,
                                      db=root / "sourceflow.sqlite3", browser_id="fake", max_scrolls=0,
                                      dry_run=False)
            with patch("sourceflow.collector.ROOT", root), patch(
                "sourceflow.collector.BrowserAct", side_effect=lambda *_: FakeBrowser(pages)
            ):
                first = run(args)
                self.assertEqual(first["sources"][0]["status"], "partial")
                self.assertEqual(source_cursor(connect(args.db), "x:example")["coverage_verified"], 0)
                pages = {"https://x.com/example": [new, old],
                         "https://x.com/example/with_replies": [new, old]}
                second = run(args)
            self.assertNotEqual(first["run_file"], second["run_file"])
            self.assertEqual(first["database"]["inserted"], 1)
            self.assertEqual(second["database"]["inserted"], 0)
            self.assertEqual(second["sources"][0]["status"], "complete")
            self.assertEqual(len(json.loads(Path(first["new_items_file"]).read_text())["items"]), 1)
            self.assertEqual(json.loads(Path(second["new_items_file"]).read_text())["items"], [])
            conn = connect(args.db)
            self.assertEqual(source_cursor(conn, "x:example")["coverage_verified"], 1)


if __name__ == "__main__":
    unittest.main()
