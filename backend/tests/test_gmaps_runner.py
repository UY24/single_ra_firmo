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

    def test_a_no_listing_row_lands_on_the_live_counter(self):
        """The mid-run status serves rows_no_listing, and Counters.bump used to DROP an
        unlisted key — so a run in flight reported 0 no-listing rows and the billing card
        could not explain why the bill was under rows x 10 credits."""
        fake = FakeS3()
        _seed(fake)
        with _patched(fake), mock.patch.object(
                runner, "execute_gmaps_lookup",
                new=mock.AsyncMock(return_value=_response(
                    website=None, no_results=True, requests=3))):
            counters = store.Counters(PREFIX, rows_total=3)
            asyncio.run(runner.run_scrape_phase(PREFIX, counters))
            status = store.read_status(PREFIX)
        self.assertEqual(status["rows_no_listing"], 2)
        # Attempted three times each and billed for none: failures are free.
        self.assertEqual(status["requests"], 6)
        self.assertEqual(status["credits"], 0)

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

    def test_found_and_not_found_carry_every_input_column(self):
        """The output is the INPUT FILE plus what we worked out. Reporting only the five
        columns the parser happens to map (company/country/address/firm_id/industry) hands
        back a file the user cannot line up against their own."""
        fake = FakeS3()
        self._seed_rows(fake)
        self._run_outputs(fake)
        found = list(csv.DictReader(io.StringIO(
            fake.objects[f"{PREFIX}/found.csv"].decode("utf-8-sig"))))
        not_found = list(csv.DictReader(io.StringIO(
            fake.objects[f"{PREFIX}/notFound.csv"].decode("utf-8-sig"))))
        self.assertEqual(
            list(found[0]),
            ["company_name", "country", "full_address",       # input.csv, verbatim + in order
             "website_url", "confidence", "flags", "attempt_log"])
        self.assertEqual(list(not_found[0]), list(found[0]) + ["error"])
        # full_address is carried straight from the input, not re-derived from the result.
        self.assertEqual(found[0]["full_address"], "500 Main Street")
        self.assertEqual(found[0]["company_name"], "Acme Motors")
        self.assertEqual(found[0]["website_url"], "https://acme.com")

    def test_an_input_column_named_like_a_computed_one_is_not_overwritten(self):
        """"every original column passes through" has to hold even when the input picks a
        name we also write — otherwise the user's cell silently becomes our value."""
        fake = FakeS3()
        with _patched(fake):
            store.put_bytes(store.input_key(PREFIX),
                            b"company_name,country,website_url\n"
                            b"Acme Motors,us,https://user-supplied.example\n")
            store.Counters(PREFIX, rows_total=1, phase="queued").flush(True)
            resp, _raw = _response()
            store.put_object(store.row_key(PREFIX, 0), {
                "row_index": 0, "fields": {"company_name": "Acme Motors", "country": "us"},
                "context": resp.context, "official_website": "https://acme.com"})
        self._run_outputs(fake)
        row = list(csv.DictReader(io.StringIO(
            fake.objects[f"{PREFIX}/found.csv"].decode("utf-8-sig"))))[0]
        self.assertEqual(row["website_url__orig"], "https://user-supplied.example")
        self.assertEqual(row["website_url"], "https://acme.com")

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

    def test_retry_csv_lists_only_the_rows_worth_running_again(self):
        """The rerun/refund list. A row Google answered is finished — rerunning it buys
        the same answer twice. Only a row that got no answer belongs here: no listing
        after every retry, a dead row, or a BILLED 200 that carried nothing."""
        fake = FakeS3()
        self._seed_rows(fake)
        self._run_outputs(fake)
        text = fake.objects[f"{PREFIX}/retry.csv"].decode("utf-8-sig")
        rows = list(csv.DictReader(io.StringIO(text)))
        # row 0 found a website; rows 1 (no listing) and 2 (dead) did not.
        self.assertEqual([r["company_name"] for r in rows], ["Beta Foods", ""])
        # The ORIGINAL input columns, verbatim and in order, so the file uploads straight
        # back to /uploads/gmaps — plus one column saying why each row is here.
        self.assertEqual(list(rows[0]),
                         ["company_name", "country", "full_address", "retry_reason"])
        self.assertEqual(rows[0]["full_address"], "12 Side Road")
        self.assertIn("no_listing", rows[0]["retry_reason"])
        self.assertIn("credits=0", rows[0]["retry_reason"])
        self.assertIn("error", rows[1]["retry_reason"])
        self.assertIn("HTTP 502", rows[1]["retry_reason"])

    def test_a_billed_empty_row_is_flagged_refundable_in_retry_csv(self):
        """HTTP 200 with zero results: credits charged for no data. That is the scrape.do
        refund claim, and the only reason a gmaps run pays for nothing."""
        fake = FakeS3()
        self._seed_rows(fake)
        with _patched(fake):
            resp, _raw = _response(website=None)
            resp.context["cost_breakdown"]["scrapedo_billed_empty"] = 1
            store.put_object(store.row_key(PREFIX, 1), {
                "row_index": 1, "fields": {"company_name": "Beta Foods", "country": "us"},
                "context": resp.context, "official_website": ""})
        self._run_outputs(fake)
        text = fake.objects[f"{PREFIX}/retry.csv"].decode("utf-8-sig")
        row = next(r for r in csv.DictReader(io.StringIO(text))
                   if r["company_name"] == "Beta Foods")
        self.assertIn("billed_empty", row["retry_reason"])
        self.assertIn("refundable", row["retry_reason"])
        self.assertIn("credits=10", row["retry_reason"])

    def test_a_200_with_an_error_body_is_counted_as_billed(self):
        """scrape.do charges for the 200 even when its body reports a failure, so that row
        is money spent for nothing — it belongs with billed_empty, not with the free 502s."""
        fake = FakeS3()
        self._seed_rows(fake)
        with _patched(fake):
            resp, _raw = _response(website=None, error="scrape.do maps search failed: bad")
            resp.context["cost_breakdown"].update(
                scrapedo_requests=1, scrapedo_successful_requests=1,
                scrapedo_failed_requests=0, scrapedo_error_requests=0,
                scrapedo_credits=10)
            store.put_object(store.row_key(PREFIX, 1), {
                "row_index": 1, "fields": {"company_name": "Beta Foods", "country": "us"},
                "context": resp.context, "official_website": ""})
        summary = self._run_outputs(fake)
        self.assertEqual(summary["cost"]["scrapedo_billed_errors"], 1)
        self.assertEqual(summary["cost"]["scrapedo_successful_requests"], 2)

    def test_a_billed_error_row_is_flagged_refundable_in_retry_csv(self):
        """Credits were charged (HTTP 200) and the body was an error: same refund claim as
        a billed-empty row, so retry.csv has to say so — it is the file the claim is made
        from. A dead row that never got a 200 cost nothing and must NOT say refundable."""
        fake = FakeS3()
        self._seed_rows(fake)
        with _patched(fake):
            resp, _raw = _response(website=None, error="scrape.do maps search failed: bad")
            resp.context["cost_breakdown"].update(
                scrapedo_requests=1, scrapedo_successful_requests=1,
                scrapedo_failed_requests=0, scrapedo_credits=10)
            store.put_object(store.row_key(PREFIX, 1), {
                "row_index": 1, "fields": {"company_name": "Beta Foods", "country": "us"},
                "context": resp.context, "official_website": ""})
        self._run_outputs(fake)
        text = fake.objects[f"{PREFIX}/retry.csv"].decode("utf-8-sig")
        rows = {r["company_name"]: r["retry_reason"]
                for r in csv.DictReader(io.StringIO(text))}
        self.assertIn("refundable", rows["Beta Foods"])
        self.assertIn("credits=10", rows["Beta Foods"])
        self.assertNotIn("refundable", rows[""])     # the dead row: 3 attempts, 0 credits

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


class MidRunSummaryTests(unittest.TestCase):
    def test_the_live_summary_splits_billed_from_unbilled_calls(self):
        """/status serves this until report.json exists, and the billing card reads the
        split from it. Credits are exactly 10 per billed 200, so it is arithmetic."""
        from app.services.serpwow.engine import _gmaps_fallback_summary

        summary = _gmaps_fallback_summary(
            {"requests": 139, "credits": 870, "rows_no_listing": 13,
             "rows_billed_empty": 0},
            total=100, scraped=87, failed=0)
        cost = summary["cost"]
        self.assertEqual(cost["scrapedo_successful_requests"], 87)
        self.assertEqual(cost["scrapedo_failed_requests"], 52)
        self.assertEqual(summary["empty_response_breakdown"]["no_listing"], 13)


if __name__ == "__main__":
    unittest.main()
