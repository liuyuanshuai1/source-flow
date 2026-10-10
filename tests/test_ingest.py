import json
import unittest

from sourceflow.ingest import collect, digest


class IngestTests(unittest.TestCase):
    def test_default_samples_cover_four_sources_without_pretending_completeness(self):
        from sourceflow.cli import DEFAULT_INPUTS

        records = collect(DEFAULT_INPUTS + [DEFAULT_INPUTS[0]])
        self.assertEqual({item["platform"] for item in records}, {"bilibili", "github", "x", "xiaohongshu"})
        self.assertEqual(len(records), 4)
        self.assertEqual(next(item for item in records if item["platform"] == "github")["status"], "sample")
        self.assertEqual(next(item for item in records if item["platform"] == "bilibili")["status"], "partial")
        report = digest(records)
        self.assertIn("中文转述", report)
        self.assertIn("演示数据", report)
        self.assertEqual(report.count("[查看原文]"), 4)

    def test_conflicting_same_source_id_is_rejected(self):
        from tempfile import TemporaryDirectory
        from sourceflow.cli import DEFAULT_INPUTS

        original = json.loads(DEFAULT_INPUTS[0].read_text(encoding="utf-8"))
        original["content"] = "另一段内容"
        with TemporaryDirectory() as directory:
            from pathlib import Path

            conflicting = Path(directory) / "conflict.json"
            conflicting.write_text(json.dumps(original), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "conflicting duplicate"):
                collect([DEFAULT_INPUTS[0], conflicting])
