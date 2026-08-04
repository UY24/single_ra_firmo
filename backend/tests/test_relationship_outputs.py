"""Phase 3: stream cleaned/ + input.csv into the two CSVs and a summary-only report."""
import csv
import io
import json
import unittest

from app.services.serpwow import relationship_outputs as outputs
from app.services.serpwow import relationship_store as store
from tests.test_relationship_store import FakeS3, _patched

PREFIX = "acme/relationship/run1"
CSV = (b"Input_URL,Company_Name_X,Company_Name_Y,country,Notes\n"
       b"https://acme.com/p,Acme,Sanzo,US,keep-me\n"
       b"https://acme.com/p,Acme,Yuzu,US,also-keep\n"
       b"https://acme.com/p,Acme,Dead,US,third\n")


def _seed(fake):
    with _patched(fake):
        store.put_bytes(store.input_key(PREFIX), CSV)
        store.put_object(store.raw_key(PREFIX, 0), {"credits": 10, "request_count": 1,
                                                    "successful_requests": 1})
        store.put_object(store.cleaned_key(PREFIX, 0), {
            "row_index": 0, "candidates": ["https://drinksanzo.com"],
            "x_domain": "acme.com",
            "parsed": {"relationship_status": "confirmed",
                       "official_website": "https://drinksanzo.com",
                       "relationship_summary": "Acme led the Series A.",
                       "relationship_evidence": ["Series A, 2021"],
                       "resolved_company_y_name": "Sanzo",
                       "relationship_confidence_score": 92,
                       "website_confidence_score": 88}})
        store.put_object(store.raw_key(PREFIX, 1), {"credits": 10, "request_count": 1,
                                                    "successful_requests": 1})
        store.put_object(store.cleaned_key(PREFIX, 1), {
            "row_index": 1, "candidates": ["https://yuzu.com"], "x_domain": "acme.com",
            "parsed": {"relationship_status": "not_confirmed",
                       "official_website": "https://yuzu.com",
                       "relationship_summary": "No financial link found.",
                       "relationship_confidence_score": 5,
                       "website_confidence_score": 70}})
        store.put_object(store.error_key(PREFIX, 2),
                         {"error": "HTTP 529", "request_count": 4,
                          "successful_requests": 0, "credits": 0})


def _read_csv(fake, name):
    with _patched(fake):
        raw = store.get_bytes(f"{PREFIX}/{name}")
    return list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))


class OutputTests(unittest.TestCase):
    def test_split_is_by_relationship_status_not_url_presence(self) -> None:
        fake = FakeS3()
        _seed(fake)
        with _patched(fake):
            outputs.write_outputs(PREFIX, store.Counters(PREFIX, rows_total=3))

        confirmed = _read_csv(fake, "confirmed_relation.csv")
        notconfirmed = _read_csv(fake, "notconfirmed_relation.csv")

        self.assertEqual([r["Company_Name_Y"] for r in confirmed], ["Sanzo"])
        # Yuzu HAS a candidate URL but no confirmed relationship -> notconfirmed, and
        # its website_url must be gated to empty.
        names = [r["Company_Name_Y"] for r in notconfirmed]
        self.assertEqual(sorted(names), ["Dead", "Yuzu"])
        yuzu = next(r for r in notconfirmed if r["Company_Name_Y"] == "Yuzu")
        self.assertEqual(yuzu["website_url"], "")

    def test_passthrough_columns_survive(self) -> None:
        fake = FakeS3()
        _seed(fake)
        with _patched(fake):
            outputs.write_outputs(PREFIX, store.Counters(PREFIX, rows_total=3))
        confirmed = _read_csv(fake, "confirmed_relation.csv")
        self.assertEqual(confirmed[0]["Notes"], "keep-me")
        self.assertEqual(confirmed[0]["website_url"], "https://drinksanzo.com")

    def test_a_scrape_error_row_is_reported_with_its_source(self) -> None:
        fake = FakeS3()
        _seed(fake)
        with _patched(fake):
            outputs.write_outputs(PREFIX, store.Counters(PREFIX, rows_total=3))
        dead = next(r for r in _read_csv(fake, "notconfirmed_relation.csv")
                    if r["Company_Name_Y"] == "Dead")
        self.assertEqual(dead["error_source"], "scrapedo")
        self.assertIn("529", dead["error_reason"])

    def test_report_json_is_summary_only_with_no_per_row_array(self) -> None:
        fake = FakeS3()
        _seed(fake)
        with _patched(fake):
            outputs.write_outputs(PREFIX, store.Counters(PREFIX, rows_total=3))
            report = json.loads(store.get_bytes(f"{PREFIX}/report.json"))

        self.assertIn("summary", report)
        # At 500k rows a rows array is gigabytes. The CSVs carry per-row detail.
        self.assertNotIn("rows", report)
        s = report["summary"]
        self.assertEqual(s["total_rows"], 3)
        self.assertEqual(s["websites_found"], 1)
        self.assertEqual(s["relationship_breakdown"]["confirmed"], 1)
        self.assertEqual(s["outcome_breakdown"]["errored"], 1)

    def test_cost_is_credits_only_with_no_serpwow_keys(self) -> None:
        fake = FakeS3()
        _seed(fake)
        with _patched(fake):
            summary = outputs.write_outputs(PREFIX, store.Counters(PREFIX, rows_total=3))
        cost = summary["cost"]
        self.assertEqual(cost["scrapedo_credits"], 20)     # 2 successes x 10
        self.assertEqual(cost["scrapedo_requests"], 6)     # 1 + 1 + 4 attempts
        self.assertEqual(cost["scrapedo_error_requests"], 4)
        self.assertNotIn("serpwow_usd", cost)
        self.assertNotIn("serpwow_searches", cost)

    def test_run_log_is_written(self) -> None:
        fake = FakeS3()
        _seed(fake)
        with _patched(fake):
            outputs.write_outputs(PREFIX, store.Counters(PREFIX, rows_total=3))
            log = store.get_bytes(f"{PREFIX}/run.log").decode()
        self.assertIn("scrapedo_credits=20", log)
        self.assertIn("Sanzo", log)


if __name__ == "__main__":
    unittest.main()
