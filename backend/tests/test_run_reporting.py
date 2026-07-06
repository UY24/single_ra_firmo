# backend/tests/test_run_reporting.py
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.models.results import AttemptLogEntry, EntityResult, Flag
from app.services.ai_mode import run_store
from app.services.ai_mode.run_reporting import write_outputs
from app.services.ai_mode.run_store import slugify_company


class TestSlugify(unittest.TestCase):
    def test_slugify(self):
        self.assertEqual(slugify_company("Acme Corp (JP)!"), "acme-corp-jp")

    def test_slugify_uppercase_and_spaces(self):
        self.assertEqual(slugify_company("  HELLO   World  "), "hello-world")

    def test_slugify_unicode_only_name_falls_back(self):
        self.assertEqual(slugify_company("株式会社"), "unnamed")

    def test_slugify_empty_falls_back(self):
        self.assertEqual(slugify_company("   "), "unnamed")


class TestRunStore(unittest.TestCase):
    def test_run_dir_for_find_and_list(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "ai_mode_results"
            with patch.object(run_store, "AI_MODE_RESULTS_DIR", root):
                d1 = run_store.run_dir_for("Acme Corp", "run-1")
                self.assertEqual(d1, root / "acme-corp" / "run-1")
                self.assertTrue(d1.is_dir())
                self.assertTrue((d1 / "raw_responses").is_dir())

                d2 = run_store.run_dir_for("Beta GmbH", "run-2")

                self.assertEqual(run_store.find_run_dir("run-1"), d1)
                self.assertEqual(run_store.find_run_dir("run-2"), d2)
                self.assertIsNone(run_store.find_run_dir("missing"))

                self.assertEqual(set(run_store.list_run_dirs()), {d1, d2})

    def test_find_and_list_when_root_missing(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "does-not-exist"
            with patch.object(run_store, "AI_MODE_RESULTS_DIR", root):
                self.assertIsNone(run_store.find_run_dir("run-1"))
                self.assertEqual(run_store.list_run_dirs(), [])


class TestWriteOutputs(unittest.TestCase):
    def test_found_notfound_and_final_report(self):
        results = [
            EntityResult("Acme", "Japan", 1, website_url="https://acme.jp", confidence=90,
                         flags=[Flag("name_match", "exact")],
                         attempt_log=[AttemptLogEntry("q1", "found", "https://acme.jp")]),
            EntityResult("Beta", "Germany", 2, confidence=10,
                         flags=[Flag("no_results", "nothing found")], error="llm: no data"),
        ]
        with tempfile.TemporaryDirectory() as d:
            run_dir = Path(d)
            write_outputs(run_dir, results, summary={"status": "completed"},
                          requests=[{"batch": 1, "status": "ok"}])
            found = list(csv.DictReader(io.StringIO((run_dir / "found.csv").read_text())))
            notfound = list(csv.DictReader(io.StringIO((run_dir / "notFound.csv").read_text())))
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0]["website_url"], "https://acme.jp")
            self.assertEqual(found[0]["flags"], "name_match: exact")
            self.assertIn("attempt_log", found[0])
            self.assertEqual(notfound[0]["error"], "llm: no data")
            report = json.loads((run_dir / "final_report.json").read_text())
            self.assertEqual(report["summary"]["status"], "completed")
            self.assertEqual(len(report["requests"]), 1)
            self.assertEqual(len(report["entities"]), 2)
            self.assertFalse((run_dir / "report.json").exists())


if __name__ == "__main__":
    unittest.main()
