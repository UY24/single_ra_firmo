"""gmaps on the S3-only runner: object presence is the state, and there is no phase 2."""
import asyncio
import csv
import io
import json
import unittest
from unittest import mock

from app.services.serpwow import gmaps_outputs as outputs
from app.services.serpwow import gmaps_runner as runner
from app.services.serpwow import s3_run_driver as driver
from app.services.serpwow import s3_run_store as store
from app.services.serpwow.schemas import CrawlResponse
from tests.test_s3_run_store import FakeS3, _patched

PREFIX = "acme/gmaps/run1"
CSV = (b"company_name,country,full_address\n"
       b"Acme Motors,us,500 Main Street\n"
       b"Beta Foods,us,12 Side Road\n"
       b",us,no name here\n")


def _seed(fake: FakeS3) -> None:
    with _patched(fake):
        store.put_bytes(store.input_key(PREFIX), CSV)
        store.Counters(PREFIX, rows_total=3, phase="queued").flush(True)


def _response(website="https://acme.com", *, error=None, no_results=False,
              requests=1, credits=10):
    """What execute_gmaps_lookup returns: (CrawlResponse, raw json string)."""
    context = {
        "pipeline": "gmaps",
        "success": bool(website),
        "error": error,
        # A no-listing row is a 502 with an "no results" body: attempted, never billed.
        "cost_breakdown": {
            "scrapedo_requests": requests,
            "scrapedo_successful_requests": 0 if (error or no_results) else 1,
            "scrapedo_failed_requests": requests if (error or no_results) else 0,
            "scrapedo_recovered_requests": 0,
            "scrapedo_error_requests": requests if error else 0,
            "scrapedo_no_results": 1 if no_results else 0,
            "scrapedo_billed_empty": 0,
            "scrapedo_credits": 0 if (error or no_results) else credits,
            "gemini_cost_usd": 0.0, "total_cost_usd": 0.0,
        },
        "gmaps_confidence": {"raw": {"official_website": website,
                                     "confidence_score": 90 if website else 0},
                             "mode": "heuristic"},
    }
    if error:
        context["error_category"] = "http_5xx"
    if no_results:
        context["row_error"] = "No Google Maps listing exists for this company."
    return (CrawlResponse(company_name="X", country="us",
                          official_website=website or None, summary="s",
                          gemini_cost_usd=0.0, total_cost_usd=0.0,
                          context=context),
            json.dumps({"local_results": [{"website": website}] if website else []}))


class ScrapePhaseTests(unittest.TestCase):
    def test_a_scraped_row_writes_the_provider_bytes_and_the_result(self):
        fake = FakeS3()
        _seed(fake)
        with _patched(fake), mock.patch.object(
                runner, "execute_gmaps_lookup",
                new=mock.AsyncMock(return_value=_response())):
            counters = store.Counters(PREFIX, rows_total=3)
            asyncio.run(runner.run_scrape_phase(PREFIX, counters))

        with _patched(fake):
            raw = store.get_bytes(store.raw_key(PREFIX, 0))
            row = store.get_object(store.row_key(PREFIX, 0))
        # raw/ is the provider's own payload, byte for byte — it is what a human reads to
        # judge a row, so nothing of ours goes in it.
        self.assertEqual(json.loads(raw)["local_results"][0]["website"],
                         "https://acme.com")
        self.assertEqual(row["official_website"], "https://acme.com")
        self.assertEqual(row["fields"]["company_name"], "Acme Motors")

    def test_rows_that_already_have_a_result_are_never_rescraped(self):
        """Resume is free: this is what stops a re-drive re-buying credits."""
        fake = FakeS3()
        _seed(fake)
        with _patched(fake):
            store.put_object(store.row_key(PREFIX, 0), {"row_index": 0, "fields": {},
                                                        "context": {}})
        lookup = mock.AsyncMock(return_value=_response())
        with _patched(fake), mock.patch.object(runner, "execute_gmaps_lookup", lookup):
            asyncio.run(runner.run_scrape_phase(PREFIX, store.Counters(PREFIX, rows_total=3)))
        # rows 1 and 2 only — and row 2 has no company name, so it never reaches the client
        self.assertEqual(lookup.await_count, 1)

    def test_a_row_with_no_company_name_still_produces_a_result_row(self):
        """One row in, one row out. A nameless row cannot be searched, but dropping it
        would silently shorten the output CSVs against the input."""
        fake = FakeS3()
        _seed(fake)
        lookup = mock.AsyncMock(return_value=_response())
        with _patched(fake), mock.patch.object(runner, "execute_gmaps_lookup", lookup):
            asyncio.run(runner.run_scrape_phase(PREFIX, store.Counters(PREFIX, rows_total=3)))
        with _patched(fake):
            row = store.get_object(store.row_key(PREFIX, 2))
        self.assertEqual(row["row_error"], "Row has no company name.")
        self.assertEqual(lookup.await_count, 2)     # never called for the blank row

    def test_credits_and_attempts_land_on_the_counters(self):
        fake = FakeS3()
        _seed(fake)
        with _patched(fake), mock.patch.object(
                runner, "execute_gmaps_lookup",
                new=mock.AsyncMock(return_value=_response(requests=3, credits=10))):
            counters = store.Counters(PREFIX, rows_total=3)
            asyncio.run(runner.run_scrape_phase(PREFIX, counters))
            status = store.read_status(PREFIX)
        # 2 scrapable rows x 3 attempts; only the successful call of each is billed.
        self.assertEqual(status["requests"], 6)
        self.assertEqual(status["credits"], 20)

    def test_a_failed_put_counts_the_row_failed_rather_than_losing_it_silently(self):
        """S3 is the ONLY copy — a swallowed PUT would drop the row's work with no trace
        and no re-drive."""
        fake = FakeS3(fail_keys={store.row_key(PREFIX, 0), store.row_key(PREFIX, 1)})
        _seed(fake)
        with _patched(fake), mock.patch.object(
                runner, "execute_gmaps_lookup",
                new=mock.AsyncMock(return_value=_response())):
            counters = store.Counters(PREFIX, rows_total=3)
            asyncio.run(runner.run_scrape_phase(PREFIX, counters))
            status = store.read_status(PREFIX)
        self.assertEqual(status["rows_failed"], 2)


class DoneMarkerTests(unittest.TestCase):
    def test_done_means_the_result_object_not_the_raw_one(self):
        """raw/ is written FIRST and rows/ second, so a crash between them must leave the
        row pending — otherwise it would be reported with no result at all."""
        fake = FakeS3()
        _seed(fake)
        with _patched(fake):
            store.put_bytes(store.raw_key(PREFIX, 0), b"{}", "application/json")
            done = store.list_done_rows(PREFIX, runner.DONE_PREFIX)
        self.assertEqual(done, set())

    def test_an_error_marker_also_counts_as_done(self):
        fake = FakeS3()
        _seed(fake)
        with _patched(fake):
            store.put_object(store.error_key(PREFIX, 1), {"error": "HTTP 502"})
            done = store.list_done_rows(PREFIX, runner.DONE_PREFIX)
        self.assertEqual(done, {1})


class DriveTests(unittest.TestCase):
    def test_drive_run_is_scrape_then_outputs_with_no_verdict_phase(self):
        fake = FakeS3()
        _seed(fake)
        order = []
        with _patched(fake), \
                mock.patch.object(runner, "run_scrape_phase",
                                  lambda p, c: order.append("scrape") or asyncio.sleep(0)), \
                mock.patch.object(runner, "write_outputs",
                                  lambda p, c: order.append("outputs") or {}), \
                mock.patch.object(driver, "notify_terminal"):
            store.write_run_pointer("run1", PREFIX, "Acme",
                                    segment=runner.PIPELINE_SEGMENT)
            asyncio.run(runner.drive_run("run1"))
        self.assertEqual(order, ["scrape", "outputs"])

    def test_the_pointer_lives_in_its_own_namespace(self):
        """_gmaps_runs/, not _relationship_runs/ — otherwise one pipeline's stale-run scan
        would pick up the other's runs and drive them with the wrong runner."""
        fake = FakeS3()
        with _patched(fake):
            store.write_run_pointer("run1", PREFIX, "Acme",
                                    segment=runner.PIPELINE_SEGMENT)
        self.assertIn("_gmaps_runs/run1.json", fake.objects)
        with _patched(fake):
            self.assertIsNone(store.read_run_pointer("run1"))   # relationship default
            self.assertIsNotNone(
                store.read_run_pointer("run1", runner.PIPELINE_SEGMENT))


class OutputsTests(unittest.TestCase):
    def _run_outputs(self, fake) -> dict:
        with _patched(fake):
            return outputs.write_outputs(PREFIX, store.Counters(PREFIX, rows_total=3))

    def _seed_rows(self, fake):
        _seed(fake)
        with _patched(fake):
            resp, _raw = _response()
            store.put_object(store.row_key(PREFIX, 0), {
                "row_index": 0, "fields": {"company_name": "Acme Motors", "country": "us"},
                "context": resp.context, "official_website": "https://acme.com"})
            resp2, _ = _response(website=None, no_results=True)
            store.put_object(store.row_key(PREFIX, 1), {
                "row_index": 1, "fields": {"company_name": "Beta Foods", "country": "us"},
                "context": resp2.context, "official_website": ""})
            store.put_object(store.error_key(PREFIX, 2), {
                "row_index": 2, "error": "scrape.do maps search failed (HTTP 502).",
                "error_category": "http_5xx",
                "cost_breakdown": {"scrapedo_requests": 3, "scrapedo_error_requests": 3}})

    def test_rows_split_between_found_and_not_found(self):
        fake = FakeS3()
        self._seed_rows(fake)
        summary = self._run_outputs(fake)
        found = fake.objects[f"{PREFIX}/found.csv"].decode("utf-8-sig")
        not_found = fake.objects[f"{PREFIX}/notFound.csv"].decode("utf-8-sig")
        self.assertIn("https://acme.com", found)
        self.assertIn("Beta Foods", not_found)
        self.assertEqual(summary["websites_found"], 1)
        self.assertEqual(summary["total_rows"], 3)

    def test_a_no_listing_row_is_not_found_not_an_error(self):
        """scrape.do overloads 502 for "Google has no listing"; counting those as errors
        inflates the failed badge and offers a retry that can only fail again."""
        fake = FakeS3()
        self._seed_rows(fake)
        summary = self._run_outputs(fake)
        self.assertEqual(summary["outcome_breakdown"]["not_found"], 1)
        self.assertEqual(summary["empty_response_breakdown"]["no_listing"], 1)

    def test_a_dead_row_reports_the_providers_own_message(self):
        fake = FakeS3()
        self._seed_rows(fake)
        summary = self._run_outputs(fake)
        self.assertEqual(summary["outcome_breakdown"]["errored"], 1)
        self.assertEqual(summary["error_breakdown"]["by_source"], {"scrapedo": 1})
        self.assertIn("HTTP 502",
                      fake.objects[f"{PREFIX}/notFound.csv"].decode("utf-8-sig"))

    def test_report_json_is_summary_only(self):
        """At 500k rows a per-row array would be gigabytes — the CSVs hold the detail."""
        fake = FakeS3()
        self._seed_rows(fake)
        self._run_outputs(fake)
        report = json.loads(fake.objects[f"{PREFIX}/report.json"])
        self.assertEqual(list(report.keys()), ["summary"])
        self.assertNotIn("rows", report)

    def test_cost_reports_credits_and_no_serpwow_keys(self):
        fake = FakeS3()
        self._seed_rows(fake)
        cost = self._run_outputs(fake)["cost"]
        self.assertEqual(cost["scrapedo_credits"], 10)
        self.assertEqual(cost["scrapedo_error_requests"], 3)
        self.assertEqual(cost["llm_usd"], 0.0)

    def test_a_row_that_was_never_processed_is_internal_not_a_provider_failure(self):
        fake = FakeS3()
        _seed(fake)
        summary = self._run_outputs(fake)
        self.assertEqual(summary["outcome_breakdown"]["errored"], 3)
        self.assertEqual(summary["error_breakdown"]["by_source"], {"internal": 3})

    def test_every_input_row_appears_exactly_once(self):
        fake = FakeS3()
        self._seed_rows(fake)
        self._run_outputs(fake)
        rows = 0
        for name in ("found.csv", "notFound.csv"):
            text = fake.objects[f"{PREFIX}/{name}"].decode("utf-8-sig")
            rows += len(list(csv.DictReader(io.StringIO(text))))
        self.assertEqual(rows, 3)


if __name__ == "__main__":
    unittest.main()
