# backend/tests/test_ai_mode_streaming_report.py
"""StreamingRunReport (PR1): incremental CSV/report writer for 1M-row runs.

Verifies parity with the classic write_outputs contract, per-batch streaming,
outcome counters, and the AI_MODE_REPORT_ENTITIES_MAX entities cap.
"""
import csv
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.models.results import AttemptLogEntry, EntityResult, Flag
from app.services.ai_mode.run_reporting import StreamingRunReport, write_outputs
from app.services.serpwow.outcomes import SRC_SCRAPEDO


def _results():
    return [
        EntityResult("Acme", "Japan", 1, website_url="https://acme.jp", confidence=90,
                     flags=[Flag("name_match", "exact")],
                     attempt_log=[AttemptLogEntry("q1", "found", "https://acme.jp")]),
        EntityResult("Beta", "Germany", 2, confidence=10,
                     flags=[Flag("no_results", "nothing found")], error="llm: no data"),
        EntityResult("Gamma", "US", 3),
    ]


class TestStreamingRunReport(unittest.TestCase):
    def test_add_batch_and_close_matches_write_outputs_contract(self):
        with tempfile.TemporaryDirectory() as d:
            run_dir = Path(d)
            report = StreamingRunReport(run_dir)
            results = _results()
            report.add_batch({"request_index": 1, "status": "success"}, results[:2])
            report.add_batch({"request_index": 2, "status": "success"}, results[2:])
            paths = report.close(summary={"status": "completed"})

            self.assertEqual(
                set(paths), {"found.csv", "notFound.csv", "final_report.json"}
            )
            found = list(csv.DictReader(io.StringIO((run_dir / "found.csv").read_text())))
            notfound = list(csv.DictReader(io.StringIO((run_dir / "notFound.csv").read_text())))
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0]["website_url"], "https://acme.jp")
            self.assertEqual(found[0]["flags"], "name_match: exact")
            self.assertEqual(len(notfound), 2)
            self.assertEqual(notfound[0]["error"], "llm: no data")
            self.assertEqual(notfound[1]["error"], "")

            data = json.loads((run_dir / "final_report.json").read_text())
            self.assertEqual(data["summary"]["status"], "completed")
            self.assertEqual([r["request_index"] for r in data["requests"]], [1, 2])
            self.assertEqual(len(data["entities"]), 3)

    def test_counts_track_outcomes_incrementally(self):
        with tempfile.TemporaryDirectory() as d:
            report = StreamingRunReport(Path(d))
            results = _results()
            results.append(
                EntityResult("Delta", "US", 4, error="scrape.do error: HTTP 429",
                             error_source=SRC_SCRAPEDO)
            )
            report.add_batch(None, results)
            self.assertEqual(
                report.counts,
                # Beta has an untagged error -> errored (gemini); Gamma -> not_found.
                {"found": 1, "not_found": 1, "errored": 2},
            )
            self.assertEqual(report.websites_found, 1)
            self.assertEqual(report.websites_not_found, 3)
            self.assertIn(SRC_SCRAPEDO, report.by_source)
            report.close(summary={"status": "completed"})

    def test_entities_cap_omits_entities_but_keeps_csvs(self):
        with tempfile.TemporaryDirectory() as d, \
                patch.dict(os.environ, {"AI_MODE_REPORT_ENTITIES_MAX": "2"}):
            run_dir = Path(d)
            report = StreamingRunReport(run_dir)
            report.add_batch({"request_index": 1, "status": "success"}, _results())
            report.close(summary={"status": "completed"})

            data = json.loads((run_dir / "final_report.json").read_text())
            self.assertNotIn("entities", data)
            self.assertTrue(data["entities_omitted"])
            found = list(csv.DictReader(io.StringIO((run_dir / "found.csv").read_text())))
            notfound = list(csv.DictReader(io.StringIO((run_dir / "notFound.csv").read_text())))
            self.assertEqual(len(found) + len(notfound), 3)

    def test_write_outputs_reimplemented_on_streaming_writer(self):
        # Same signature + same on-disk contract as the classic implementation.
        with tempfile.TemporaryDirectory() as d:
            run_dir = Path(d)
            paths = write_outputs(run_dir, _results(), summary={"status": "x"},
                                  requests=[{"batch": 1}])
            self.assertEqual(
                set(paths), {"found.csv", "notFound.csv", "final_report.json"}
            )
            data = json.loads((run_dir / "final_report.json").read_text())
            self.assertEqual(len(data["entities"]), 3)
            self.assertEqual(data["requests"], [{"batch": 1}])


if __name__ == "__main__":
    unittest.main()
