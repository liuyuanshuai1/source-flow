import tempfile
import unittest
from pathlib import Path

from sourceflow.collector import BrowserAct, BrowserError, collect_bilibili, collect_x
from sourceflow.db import connect, load_sources, source_cursor, update_source_cursor


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
            self.assertEqual(result.status, "success")
            self.assertEqual([item["external_id"] for item in result.items], ["new"])

    def test_bilibili_no_subtitle_is_skipped_and_becomes_cursor(self):
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
            self.assertEqual(result.cursor_id, "BVNEW")

            update_source_cursor(conn, result.source_key, result.cursor_id, result.cursor_at, "2026-09-19T01:00:00Z")
            conn.commit()
            second_browser = FakeBrowser(pages)
            second = collect_bilibili(second_browser, conn, source, "2026-09-20T01:00:00Z", Path(directory))
            self.assertEqual(second.items, [])
            self.assertEqual(second_browser.subtitle_calls, 0)


if __name__ == "__main__":
    unittest.main()
