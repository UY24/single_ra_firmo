"""Phase 3: stream cleaned/ + input.csv into the two CSVs and a summary-only report."""
import csv
import io
import json
import unittest
from unittest import mock

from app.services.serpwow import relationship_outputs as outputs
from app.services.serpwow import s3_run_store as store
from tests.test_s3_run_store import FakeS3, _patched

PREFIX = "acme/relationship/run1"
CSV = (b"Input_URL,Company_Name_X,Company_Name_Y,country,Notes\n"
       b"https://acme.com/p,Acme,Sanzo,US,keep-me\n"
       b"https://acme.com/p,Acme,Yuzu,US,also-keep\n"
       b"https://acme.com/p,Acme,Dead,US,third\n")


# A minimal scrape.do body: what write_row stores, and all read_row needs to conclude the
# row was one billed 200.
BODY = {"text_blocks": [{"snippet": "Acme invested in it."}],
        "references": [{"title": "Y", "link": "https://y.example"}]}


def _seed(fake):
    with _patched(fake):
        store.put_bytes(store.input_key(PREFIX), CSV)
        store.write_row(PREFIX, 0, {"response": BODY})
        store.put_object(store.cleaned_key(PREFIX, 0), {
            "row_index": 0, "candidates": ["https://drinksanzo.com"],
            "x_domain": "acme.com",
            "parsed": {"relationship_status": "confirmed",
                       "official_website": "https://drinksanzo.com",
                       "relationship_summary": "Acme led the Series A.",
                       "relationship_evidence": ["Series A, 2021"],
                       "relationship_confidence_score": 92,
                       "website_confidence_score": 88}})
        store.write_row(PREFIX, 1, {"response": BODY})
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


def _read_csv(fake, name, prefix=PREFIX):
    with _patched(fake):
        raw = store.get_bytes(f"{prefix}/{name}")
    return list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))


class AttemptLogTests(unittest.TestCase):
    def test_the_csv_carries_an_attempt_log_with_the_candidate_set(self) -> None:
        """One AI Mode call replaced three phase queries, but "one attempt" is not
        "nothing worth recording": without this you cannot tell a verdict reached on 12
        references from one reached on an empty response."""
        fake = FakeS3()
        _seed(fake)
        with _patched(fake):
            outputs.write_outputs(PREFIX, store.Counters(PREFIX, rows_total=3))
        row = _read_csv(fake, "confirmed_relation.csv")[0]
        log = row["attempt_log"]
        self.assertIn("google/search/ai-mode", log)
        self.assertIn("ai_mode_references:", log)
        self.assertIn("candidates_after_filtering:", log)
        # The candidate set the gate was allowed to choose from.
        self.assertIn("candidate: https://drinksanzo.com", log)
        # Newline-joined so each fact is its own line inside one quoted cell.
        self.assertIn("\n", log)

    def test_an_errored_row_records_the_error_in_its_attempt_log(self) -> None:
        fake = FakeS3()
        _seed(fake)
        with _patched(fake):
            outputs.write_outputs(PREFIX, store.Counters(PREFIX, rows_total=3))
        dead = next(r for r in _read_csv(fake, "notconfirmed_relation.csv")
                    if r["Company_Name_Y"] == "Dead")
        self.assertIn("529", dead["attempt_log"])


class LlmReportingTests(unittest.TestCase):
    """The run-detail Model chip and the Input/Output-token + LLM-cost tiles read these.
    They went blank because the summary stopped feeding them, not because the UI lost
    them — collect_results returns usage per request and the runner was discarding it."""

    def test_model_tokens_and_gemini_cost_are_reported(self) -> None:
        fake = FakeS3()
        _seed(fake)
        with _patched(fake):
            # Two rows carry usage; the third has none (e.g. an error row).
            for idx in (0, 1):
                c = store.get_object(store.cleaned_key(PREFIX, idx)) or {}
                c.update({"usage": {"promptTokenCount": 100,
                                    "candidatesTokenCount": 40},
                          "model": "gemini-2.5-flash-lite"})
                store.put_object(store.cleaned_key(PREFIX, idx), c)
            summary = outputs.write_outputs(PREFIX, store.Counters(PREFIX, rows_total=3))

        self.assertEqual(summary["model"], "gemini-2.5-flash-lite")
        self.assertEqual(summary["token_usage"]["prompt_tokens"], 200)
        self.assertEqual(summary["token_usage"]["completion_tokens"], 80)
        self.assertEqual(summary["token_usage"]["total_tokens"], 280)
        # scrape.do is credits-only, so USD is the Gemini spend alone.
        self.assertEqual(summary["cost"]["llm_usd"], summary["cost"]["total_usd"])
        self.assertGreater(summary["cost"]["llm_usd"], 0.0)

    def test_batch_mode_is_always_on_for_this_pipeline(self) -> None:
        fake = FakeS3()
        _seed(fake)
        with _patched(fake):
            summary = outputs.write_outputs(PREFIX, store.Counters(PREFIX, rows_total=3))
        self.assertTrue(summary["is_batch"])

    def test_timings_are_reported_per_phase_and_in_total(self) -> None:
        fake = FakeS3()
        _seed(fake)
        with _patched(fake):
            counters = store.Counters(PREFIX, rows_total=3,
                                      created_at="2026-08-04T00:00:00Z")
            counters.bump(scrape_seconds=42, llm_seconds=300)
            summary = outputs.write_outputs(PREFIX, counters)

        self.assertEqual(summary["phase_seconds"], {"scraping": 42, "cleaning": 300})
        # created_at is long past, so total wall clock is a real positive number.
        self.assertGreater(summary["processing_seconds_total"], 0)
        self.assertGreater(summary["processing_seconds_avg"], 0)


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
        # The "confidence" column is NOT structurally dead (it was reviewed as "always
        # 0" because build_relationship_prompt's schema has no confidence_score key):
        # apply_relationship_gate WRITES confidence_score back onto the same `parsed`
        # dict in place, as min(relationship, website) for a confirmed row that passed
        # the URL gate — min(92, 88) here — and _out_row reads that same object.
        self.assertEqual(confirmed[0]["confidence"], "88")
        self.assertEqual(confirmed[0]["relationship_confidence"], "92")
        self.assertEqual(confirmed[0]["website_confidence"], "88")

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

    def test_run_log_status_reflects_errors(self) -> None:
        # _seed's row 2 (Dead) is a genuine error -> the run is NOT clean.
        fake = FakeS3()
        _seed(fake)
        with _patched(fake):
            outputs.write_outputs(PREFIX, store.Counters(PREFIX, rows_total=3))
            log = store.get_bytes(f"{PREFIX}/run.log").decode()
        self.assertIn("status=completed_with_errors\n", log)

    def test_csv_and_log_uploads_go_through_put_fileobj_not_put_bytes(self) -> None:
        """Memory: nothing about assembling the CSVs/log may scale with row count.
        Proof: they go to S3 via put_fileobj (a file object, spooled to disk and
        handed to boto3's multipart-capable upload_fileobj) rather than put_bytes
        with a fully-materialised in-memory blob."""
        fake = FakeS3()
        _seed(fake)
        seen: dict[str, object] = {}
        orig_put_fileobj = store.put_fileobj

        def _spy(key, fileobj, **kw):
            seen[key] = fileobj
            assert hasattr(fileobj, "seek") and hasattr(fileobj, "read"), (
                "put_fileobj must receive a file object, not in-memory text/bytes")
            return orig_put_fileobj(key, fileobj, **kw)

        with _patched(fake), mock.patch.object(store, "put_fileobj", side_effect=_spy):
            outputs.write_outputs(PREFIX, store.Counters(PREFIX, rows_total=3))

        self.assertEqual(set(seen),
                         {f"{PREFIX}/confirmed_relation.csv",
                          f"{PREFIX}/notconfirmed_relation.csv",
                          f"{PREFIX}/retry.csv", f"{PREFIX}/run.log"})


COLLISION_PREFIX = "acme/relationship/run-collision"
COLLISION_CSV = (b"Input_URL,Company_Name_X,Company_Name_Y,country,flags,row_index\n"
                  b"https://acme.com/p,Acme,Sanzo,US,important-note,999\n")


class PassthroughCollisionTests(unittest.TestCase):
    def test_passthrough_column_colliding_with_a_computed_or_reserved_name_is_suffixed(
            self) -> None:
        """An input column literally named "flags" (one of EXTRA_COLUMNS) or
        "row_index" (the internal bookkeeping key injected by iter_input_rows) must
        not vanish under the computed/injected value — that breaks the "every
        original column passes through" contract with no warning."""
        fake = FakeS3()
        with _patched(fake):
            store.put_bytes(store.input_key(COLLISION_PREFIX), COLLISION_CSV)
            store.write_row(COLLISION_PREFIX, 0, {"response": BODY})
            store.put_object(store.cleaned_key(COLLISION_PREFIX, 0), {
                "row_index": 0, "candidates": ["https://drinksanzo.com"],
                "x_domain": "acme.com",
                "parsed": {"relationship_status": "confirmed",
                           "official_website": "https://drinksanzo.com",
                           "relationship_summary": "s",
                               "relationship_confidence_score": 90,
                           "website_confidence_score": 80}})
            outputs.write_outputs(COLLISION_PREFIX,
                                  store.Counters(COLLISION_PREFIX, rows_total=1))

        row = _read_csv(fake, "confirmed_relation.csv", prefix=COLLISION_PREFIX)[0]
        self.assertEqual(row["flags__orig"], "important-note")
        self.assertEqual(row["row_index__orig"], "999")
        # The computed "flags" column is untouched by the passthrough value.
        self.assertNotEqual(row["flags"], "important-note")


COST_PREFIX = "acme/relationship/run-cost"
COST_CSV = (b"Input_URL,Company_Name_X,Company_Name_Y,country\n"
            b"https://acme.com/p,Acme,Confirmed,US\n"
            b"https://acme.com/p,Acme,BilledError,US\n"
            b"https://acme.com/p,Acme,NeverProcessed,US\n"
            b"https://acme.com/p,Acme,HardFail,US\n")


def _seed_cost_rows(fake) -> None:
    with _patched(fake):
        store.put_bytes(store.input_key(COST_PREFIX), COST_CSV)
        store.write_row(COST_PREFIX, 0, {"response": BODY})
        store.put_object(store.cleaned_key(COST_PREFIX, 0), {
            "row_index": 0, "candidates": ["https://x.example"], "x_domain": "acme.com",
            "parsed": {"relationship_status": "confirmed",
                       "official_website": "https://x.example",
                       "relationship_summary": "s",
                       "relationship_confidence_score": 90, "website_confidence_score": 80}})
        # row_index 1: a BILLED error-body — HTTP 200 (successful_requests=1, credits
        # spent) but the response body itself reported a provider error. This must NOT
        # count as a failed request (it succeeded at the HTTP layer).
        store.put_object(store.error_key(COST_PREFIX, 1), {
            "error": "scrape.do ai-mode search failed: insufficient credit",
            "error_category": "internal", "request_count": 1,
            "successful_requests": 1, "credits": 10})
        # row_index 2: no raw/error object at all — never processed (an internal gap,
        # not a provider failure).
        # row_index 3: a genuine hard failure — every attempt failed, nothing billed.
        store.put_object(store.error_key(COST_PREFIX, 3), {
            "error": "HTTP 529", "error_category": "http_5xx",
            "request_count": 4, "successful_requests": 0, "credits": 0})


class CostAccountingTests(unittest.TestCase):
    def test_billed_error_body_does_not_inflate_error_requests_past_failed(self) -> None:
        fake = FakeS3()
        _seed_cost_rows(fake)
        with _patched(fake):
            summary = outputs.write_outputs(
                COST_PREFIX, store.Counters(COST_PREFIX, rows_total=4))
        cost = summary["cost"]
        self.assertEqual(cost["scrapedo_requests"], 6)              # 1+1+0+4
        self.assertEqual(cost["scrapedo_successful_requests"], 2)   # confirmed + billed error
        self.assertEqual(cost["scrapedo_failed_requests"], 4)       # only HardFail's attempts
        # error_requests must never exceed failed_requests (the gmaps framing): the
        # billed-error-body row's one attempt succeeded at the HTTP layer, so it
        # contributes 0, not 1.
        self.assertEqual(cost["scrapedo_error_requests"], 4)
        self.assertLessEqual(cost["scrapedo_error_requests"], cost["scrapedo_failed_requests"])

    def test_error_breakdown_attributes_source_and_category_from_the_envelope(self) -> None:
        fake = FakeS3()
        _seed_cost_rows(fake)
        with _patched(fake):
            summary = outputs.write_outputs(
                COST_PREFIX, store.Counters(COST_PREFIX, rows_total=4))
        eb = summary["error_breakdown"]
        # 2 genuine scrape.do failures (billed error + hard fail); 1 internal gap
        # (never processed) — an internal gap must NOT be reported as a scrapedo
        # provider failure.
        self.assertEqual(eb["by_source"], {"scrapedo": 2, "internal": 1})
        self.assertEqual(eb["by_category"], {"internal": 2, "http_5xx": 1})

    def test_a_stopped_run_gets_a_distinct_status_not_completed_with_errors(self) -> None:
        """A stop can land mid-verdict/reporting (run_scrape_phase's own stop check only
        covers the gap between phases 1 and 2). write_outputs must still write every row
        that made it through, but label the run "stopped" — not "completed" or
        "completed_with_errors" — and the phase must stay one the redrive scan treats
        as terminal (never a re-spend)."""
        fake = FakeS3()
        _seed_cost_rows(fake)
        with _patched(fake):
            store.request_stop(COST_PREFIX)
            counters = store.Counters(COST_PREFIX, rows_total=4)
            summary = outputs.write_outputs(COST_PREFIX, counters)
            status = store.read_status(COST_PREFIX)

        self.assertEqual(summary["status"], "stopped")
        self.assertEqual(status["phase"], "stopped")
        from app.services.serpwow import s3_run_driver as driver
        self.assertIn("stopped", driver.TERMINAL_PHASES)

    def test_task_errors_label_the_run_completed_with_errors(self) -> None:
        """A row/shard task that RAISED leaves no per-row error marker: its rows read as
        plain "unclear"/llm_missing, every by_source count stays 0, and the run therefore
        used to report a clean "completed" after silently discarding an already-paid-for
        Gemini shard. counters.task_errors is the only trace, so the label must use it."""
        fake = FakeS3()
        with _patched(fake):
            store.put_bytes(store.input_key(COST_PREFIX),
                            b"Input_URL,Company_Name_X,Company_Name_Y,country\n"
                            b"https://acme.com/p,Acme,Sanzo,US\n")
            # Scraped fine, but no cleaned/ object — the shard that owned it died.
            store.write_row(COST_PREFIX, 0,
                             {"response": {"text_blocks": [{"snippet": "Acme invested."}],
                                           "references": []}})
            counters = store.Counters(COST_PREFIX, rows_total=1)
            counters.bump(task_errors=1)
            summary = outputs.write_outputs(COST_PREFIX, counters)

        self.assertEqual(summary["status"], "completed_with_errors")
        self.assertEqual(summary["outcome_breakdown"]["errored"], 0)   # no error marker
        self.assertEqual(summary["error_breakdown"]["task_errors"], 1)

    def test_run_log_status_is_completed_when_nothing_errored(self) -> None:
        fake = FakeS3()
        with _patched(fake):
            store.put_bytes(store.input_key(COST_PREFIX),
                            b"Input_URL,Company_Name_X,Company_Name_Y,country\n"
                            b"https://acme.com/p,Acme,Sanzo,US\n")
            store.write_row(COST_PREFIX, 0, {"response": BODY})
            store.put_object(store.cleaned_key(COST_PREFIX, 0), {
                "row_index": 0, "candidates": ["https://drinksanzo.com"],
                "x_domain": "acme.com",
                "parsed": {"relationship_status": "confirmed",
                           "official_website": "https://drinksanzo.com",
                           "relationship_summary": "s",
                               "relationship_confidence_score": 90,
                           "website_confidence_score": 80}})
            outputs.write_outputs(COST_PREFIX, store.Counters(COST_PREFIX, rows_total=1))
            log = store.get_bytes(f"{COST_PREFIX}/run.log").decode()
        self.assertIn("status=completed\n", log)


RETRY_PREFIX = "acme/relationship/retry1"
RETRY_CSV = (b"Input_URL,Company_Name_X,Company_Name_Y,country\n"
             b"https://acme.com/p,Acme,Confirmed,US\n"
             b"https://acme.com/p,Acme,Empty,US\n"
             b"https://acme.com/p,Acme,Dead,US\n"
             b"https://acme.com/p,Acme,NeverRan,US\n")


class RetryCsvTests(unittest.TestCase):
    def _seed(self, fake) -> None:
        with _patched(fake):
            store.put_bytes(store.input_key(RETRY_PREFIX), RETRY_CSV)
            store.write_row(RETRY_PREFIX, 0, {"response": BODY})
            store.put_object(store.cleaned_key(RETRY_PREFIX, 0), {
                "row_index": 0, "candidates": ["https://c.example"],
                "x_domain": "acme.com",
                "parsed": {"relationship_status": "confirmed",
                           "official_website": "https://c.example",
                           "relationship_summary": "s",
                           "relationship_confidence_score": 90,
                           "website_confidence_score": 80}})
            # A billed 200 that carried neither prose nor citations: 10 credits for
            # literally nothing — the refund claim.
            store.write_row(RETRY_PREFIX, 1, {"response": {}})
            store.put_object(store.error_key(RETRY_PREFIX, 2), {
                "error": "HTTP 529", "error_category": "http_5xx",
                "request_count": 4, "successful_requests": 0, "credits": 0})
            # row 3: no object at all — never processed.

    def test_retry_csv_lists_the_rows_that_got_no_answer(self) -> None:
        """A verdict — confirmed or not — is an answer; rerunning it re-buys it. Only a
        row with nothing to show for its credits belongs in the rerun list."""
        fake = FakeS3()
        self._seed(fake)
        with _patched(fake):
            outputs.write_outputs(RETRY_PREFIX,
                                  store.Counters(RETRY_PREFIX, rows_total=4))
        rows = _read_csv(fake, "retry.csv", RETRY_PREFIX)
        self.assertEqual([r["Company_Name_Y"] for r in rows],
                         ["Empty", "Dead", "NeverRan"])
        # The ORIGINAL input columns, so the file re-uploads to /uploads/relationship.
        self.assertEqual(list(rows[0]),
                         ["Input_URL", "Company_Name_X", "Company_Name_Y", "country",
                          "retry_reason"])
        self.assertIn("billed_empty", rows[0]["retry_reason"])
        self.assertIn("refundable", rows[0]["retry_reason"])
        self.assertIn("credits=10", rows[0]["retry_reason"])
        self.assertIn("HTTP 529", rows[1]["retry_reason"])
        self.assertIn("never processed", rows[2]["retry_reason"].lower())


if __name__ == "__main__":
    unittest.main()
