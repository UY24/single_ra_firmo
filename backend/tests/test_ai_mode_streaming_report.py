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

    def test_abort_discards_partial_csvs(self):
        # A crashed finish must never leave truncated CSVs served as complete.
        with tempfile.TemporaryDirectory() as d:
            run_dir = Path(d)
            report = StreamingRunReport(run_dir)
            report.add_batch({"request_index": 1, "status": "success"}, _results())
            report.abort()
            self.assertFalse((run_dir / "found.csv").exists())
            self.assertFalse((run_dir / "notFound.csv").exists())
            self.assertFalse((run_dir / "found.csv.tmp").exists())

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


INPUT_CSV = ("firm_id,company_name,country,notes\n"
             "1,Acme,Japan,alpha\n"
             ",,Japan,SKIPPED\n"          # no company name: parse_entities_csv drops it
             "3,Beta,Germany,gamma\n")


def _seed_input(run_dir: Path, text: str = INPUT_CSV):
    (run_dir / "input.csv").write_text(text, encoding="utf-8")
    from app.models.entities import parse_entities_csv

    return parse_entities_csv(text)


def _found_rows(run_dir: Path, name: str = "found.csv"):
    return list(csv.DictReader(io.StringIO(
        (run_dir / name).read_text(encoding="utf-8-sig"))))


class TestInputPassthrough(unittest.TestCase):
    """The output is the user's own file plus what we worked out. Reporting only the
    columns parse_entities_csv happens to map hands back a file they cannot line up
    against their input."""

    def _report(self, run_dir, text=INPUT_CSV):
        parsed = _seed_input(run_dir, text)
        return (StreamingRunReport(run_dir,
                                   parsed.columns_detected.get("company_name")),
                parsed)

    def test_input_columns_come_first_verbatim_then_the_computed_ones(self):
        with tempfile.TemporaryDirectory() as d:
            run_dir = Path(d)
            report, parsed = self._report(run_dir)
            report.add_batch({"request_index": 1}, [
                EntityResult(e.company_name, e.country, e.sno,
                             website_url=f"https://{e.sno}.example")
                for e in parsed.entities])
            report.close(summary={})
            rows = _found_rows(run_dir)
            self.assertEqual(list(rows[0]),
                             ["firm_id", "company_name", "country", "notes",
                              "website_url", "confidence", "flags", "attempt_log"])
            self.assertEqual([r["notes"] for r in rows], ["alpha", "gamma"])

    def test_a_row_with_no_company_name_is_skipped_so_columns_stay_aligned(self):
        """parse_entities_csv drops a nameless row WITHOUT spending an sno, so the
        cursor over input.csv must drop it too — otherwise every row after the first
        blank one is written with the previous row's cells."""
        with tempfile.TemporaryDirectory() as d:
            run_dir = Path(d)
            report, parsed = self._report(run_dir)
            report.add_batch({"request_index": 1}, [
                EntityResult(e.company_name, e.country, e.sno,
                             website_url=f"https://{e.sno}.example")
                for e in parsed.entities])
            report.close(summary={})
            rows = _found_rows(run_dir)
            self.assertEqual([r["firm_id"] for r in rows], ["1", "3"])
            self.assertEqual([r["company_name"] for r in rows], ["Acme", "Beta"])
            self.assertEqual([r["notes"] for r in rows], ["alpha", "gamma"])

    def test_an_input_column_named_like_a_computed_one_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            run_dir = Path(d)
            report, parsed = self._report(
                run_dir,
                "company_name,country,website_url,error\n"
                "Acme,Japan,https://user-supplied.example,mine\n")
            report.add_batch({"request_index": 1}, [
                EntityResult("Acme", "Japan", 1, website_url="https://ours.example")])
            report.close(summary={})
            row = _found_rows(run_dir)[0]
            self.assertEqual(row["website_url__orig"], "https://user-supplied.example")
            self.assertEqual(row["website_url"], "https://ours.example")
            # "error" is reserved for BOTH files, so the two headers stay parallel.
            self.assertEqual(row["error__orig"], "mine")

    def test_no_company_column_keeps_the_classic_columns(self):
        """Headerless/positional input has no header to pass through and a different
        row-skipping rule — fall back rather than emit misaligned cells."""
        with tempfile.TemporaryDirectory() as d:
            run_dir = Path(d)
            _seed_input(run_dir)
            report = StreamingRunReport(run_dir, None)
            report.add_batch({"request_index": 1}, _results()[:1])
            report.close(summary={})
            self.assertEqual(list(_found_rows(run_dir)[0]),
                             ["company_name", "company_local_name", "country",
                              "website_url", "confidence", "flags", "attempt_log"])


if __name__ == "__main__":
    unittest.main()
