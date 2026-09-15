"""firmographics on the S3-only runner: object presence is the state, three phases.

The migration this covers removed the ~2.7k-row ceiling: the old path rewrote the WHOLE
state.json per row under a per-upload lock, so bytes written grew with the square of the
row count. Every test here drives the REAL runner and the REAL store against a fake S3.
"""
import asyncio
import csv
import io
import json
import os
import unittest
from unittest import mock

from app.services.serpwow import firmographics_outputs as outputs
from app.services.serpwow import firmographics_runner as runner
from app.services.serpwow import s3_run_store as store
from app.services.serpwow.schemas import CrawlResponse
from tests.test_s3_run_store import FakeS3, _patched

PREFIX = "acme/firmographics/run1"
CSV = (b"website_url,company_name,country\n"
       b"https://acme.com,Acme Motors,de\n"
       b"https://beta.com,Beta Foods,de\n"
       b",Nameless Co,de\n")

FIELDS = {"address": "Bertha-Benz-Str. 2", "phone": "+49 7142 9930-0",
          "email": "info@acme.com", "industry": "Manufacturing",
          "products": ["Towbars"], "services": ["OE development"]}


def _seed(fake: FakeS3) -> None:
    with _patched(fake):
        store.put_bytes(store.input_key(PREFIX), CSV)
        store.Counters(PREFIX, rows_total=3, phase="queued").flush(True)


def _response(*, fields=None, overview=True, error=None, credits=10, deferred=False):
    """What execute_firmographic_extraction returns: (CrawlResponse, raw SERP json)."""
    scrapedo = {
        "provider": "scrapedo", "used": not error, "domain": "acme.com",
        "ai_overview": ({"state": "complete", "text_blocks": [{"snippet": "x"}]}
                        if overview else None),
        "deferred": deferred, "billed_no_overview": bool(not overview and not error),
        "search_requests": 1, "search_successful": 0 if error else 1,
        "ai_overview_requests": 1 if deferred else 0,
        "ai_overview_successful": 1 if deferred else 0,
        "request_count": 1, "successful_requests": 0 if error else 1,
        "failed_requests": 1 if error else 0, "credits": 0 if error else credits,
        "error": error, "error_category": "rate_limit" if error else None,
    }
    context = {
        "pipeline": "firmographics",
        # The executor records the phase split per row; the CSV's processing_seconds is
        # their sum. Present in the fixture so that column is actually covered.
        "timing": {"search_seconds": 1.25, "llm_seconds": 0.5 if fields else 0.0},
        "scrapedo": scrapedo,
        "mapping_ai": {"provider": "google-gemini",
                       "model": "gemini-2.5-flash-lite" if fields else None,
                       "used": bool(fields), "error": None,
                       "usage": ({"promptTokenCount": 100, "candidatesTokenCount": 50}
                                 if fields else {}),
                       "raw": fields},
        "cost_breakdown": {
            "scrapedo_requests": 1 + (1 if deferred else 0),
            "scrapedo_successful_requests": 0 if error else 1 + (1 if deferred else 0),
            "scrapedo_failed_requests": 1 if error else 0,
            "scrapedo_credits": 0 if error else credits,
            "scrapedo_search_requests": 1, "scrapedo_search_successful": 0 if error else 1,
            "scrapedo_ai_overview_requests": 1 if deferred else 0,
            "scrapedo_ai_overview_successful": 1 if deferred else 0,
            "scrapedo_ai_overview_deferred": 1 if deferred else 0,
            "scrapedo_billed_empty": 1 if (not overview and not error) else 0,
            "scrapedo_error_requests": 1 if error else 0,
            "scrapedo_billed_errors": 0,
            "gemini_cost_usd": 0.0002 if fields else 0.0,
            "total_cost_usd": 0.0002 if fields else 0.0,
        },
    }
    if error:
        context["formatted_results"] = [{
            "phase": "scrapedo_search", "success": False, "error": error,
            "error_source": "scrapedo", "error_category": "rate_limit"}]
    elif not overview:
        context["row_error"] = "Google returned no AI overview for this website."
    return (CrawlResponse(
        company_name="X", country="de", official_website="https://acme.com",
        summary="s", gemini_cost_usd=0.0002 if fields else 0.0,
        total_cost_usd=0.0002 if fields else 0.0, **(fields or {}),
        context=context), json.dumps({"ai_overview": scrapedo["ai_overview"]}))


def _csv_rows(fake: FakeS3, name: str) -> list[dict[str, str]]:
    with _patched(fake):
        data = store.get_bytes(f"{PREFIX}/{name}")
    return list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))


def _drive(fake: FakeS3, executor, *, batch=False, batch_results=None):
    """Run all three phases with the provider (and optionally Gemini) faked."""
    env = {"LLM_BATCH": "true" if batch else "false",
           "SCRAPEDO_CONCURRENCY": "2"}

    def fake_gemini(prefix, items, counters=None):
        return {key: {"parsed": (batch_results if batch_results is not None else FIELDS),
                      "usage": {"promptTokenCount": 100, "candidatesTokenCount": 50},
                      "model": "gemini-2.5-flash-lite"}
                for key, _body in items}

    async def go():
        with _patched(fake), mock.patch.dict(os.environ, env), \
                mock.patch.object(runner, "execute_firmographic_extraction", executor), \
                mock.patch.object(runner, "_run_gemini_batch", fake_gemini):
            counters = store.Counters(PREFIX, rows_total=3)
            return await runner._phases(PREFIX, counters, {})

    return asyncio.run(go())


class InlineModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeS3()
        _seed(self.fake)

    async def _executor(self, **kwargs):
        return _response(fields=FIELDS)

    def test_three_objects_per_row_and_no_state_json(self) -> None:
        summary = _drive(self.fake, self._executor)
        with _patched(self.fake):
            keys = sorted(store.iter_keys(PREFIX))
        # raw/ (the SERP) + rows/ (our result). No cleaned/ in inline mode — that PUT is
        # the one inline mode is allowed to skip, at 500k rows it is 500k round-trips.
        self.assertTrue(any("/raw/" in k for k in keys))
        self.assertTrue(any("/rows/" in k for k in keys))
        self.assertFalse(any("/cleaned/" in k for k in keys))
        # Nothing named state.json anywhere: that file is what capped the old pipeline.
        self.assertFalse(any(k.endswith("state.json") for k in keys))
        self.assertEqual(summary["llm_mode"], "inline")
        self.assertFalse(summary["is_batch"])

    def test_enriched_rows_land_in_enriched_csv(self) -> None:
        _drive(self.fake, self._executor)
        rows = _csv_rows(self.fake, "enriched.csv")
        self.assertEqual(len(rows), 2)          # the nameless row has no website
        self.assertEqual(rows[0]["address"], FIELDS["address"])
        self.assertEqual(rows[0]["products"], "Towbars")
        # The input header goes out verbatim, then our columns.
        self.assertEqual(rows[0]["website_url"], "https://acme.com")
        self.assertEqual(rows[0]["company_name"], "Acme Motors")
        # Per-row facts the old 37-column export carried and the first cut of this one lost.
        self.assertEqual(rows[0]["outcome"], "found")
        self.assertEqual(rows[0]["row_index"], "0")
        self.assertTrue(rows[0]["summary"])
        self.assertTrue(float(rows[0]["total_cost_usd"]) > 0)
        self.assertIn("/raw/", rows[0]["raw_response_s3_key"])
        self.assertEqual(rows[0]["processing_seconds"], "1.75")   # 1.25 scrape + 0.5 llm
        # Dropped on request: both were per-run facts masquerading as per-row ones, and
        # llm_model even reported a model for rows whose LLM never ran.
        self.assertNotIn("llm_model", rows[0])
        self.assertNotIn("scrapedo_credits", rows[0])

    def test_row_without_a_website_is_not_found_and_costs_nothing(self) -> None:
        summary = _drive(self.fake, self._executor)
        rows = _csv_rows(self.fake, "notEnriched.csv")
        self.assertEqual(len(rows), 1)
        self.assertIn("no website_url", rows[0]["enrichment_note"])
        # Spent nothing: no provider call, so no per-row cost and no SERP to point at.
        self.assertEqual(rows[0]["total_cost_usd"], "")
        self.assertEqual(rows[0]["raw_response_s3_key"], "")
        # Not an error: nothing failed, there was nothing to look up.
        self.assertEqual(summary["outcome_breakdown"]["errored"], 0)
        self.assertEqual(summary["outcome_breakdown"]["not_found"], 1)
        # And not rerunnable — the INPUT is what is missing.
        self.assertEqual(_csv_rows(self.fake, "retry.csv"), [])

    def test_credits_are_summed_per_endpoint(self) -> None:
        summary = _drive(self.fake, self._executor)
        cost = summary["cost"]
        self.assertEqual(cost["scrapedo_credits"], 20)          # 2 rows x 10
        self.assertEqual(cost["scrapedo_search_successful"], 2)
        self.assertEqual(cost["scrapedo_ai_overview_successful"], 0)
        # Credits are not dollars: the only USD is the LLM.
        self.assertEqual(cost["serpwow_usd"], 0.0)
        self.assertAlmostEqual(cost["total_usd"], cost["llm_usd"])
        self.assertEqual(summary["token_usage"]["total_tokens"], 300)


    def test_inline_mode_llm_phase_does_no_per_row_reads(self) -> None:
        """The phase must cost ONE list, not a GET per row.

        It opened every rows/ object to ask "was this deferred?", which is 500k round-trips
        at scale inside a phase that has nothing to do. A pending_llm/ marker makes the
        pending set a list instead.
        """
        _drive(self.fake, self._executor)      # inline: nothing marked pending
        reads = []
        real_get = store.get_object

        def counting_get(key):
            reads.append(key)
            return real_get(key)

        async def go():
            with _patched(self.fake), \
                    mock.patch.dict(os.environ, {"LLM_BATCH": "false"}), \
                    mock.patch.object(store, "get_object", counting_get):
                await runner.run_llm_phase(PREFIX, store.Counters(PREFIX, rows_total=3))
        asyncio.run(go())
        self.assertEqual([k for k in reads if "/rows/" in k], [],
                         "inline LLM phase opened per-row objects")


class DeferredOverviewTests(unittest.TestCase):
    def test_deferred_row_reports_its_five_extra_credits(self) -> None:
        fake = FakeS3()
        _seed(fake)

        async def executor(**kwargs):
            return _response(fields=FIELDS, deferred=True, credits=15)

        summary = _drive(fake, executor)
        cost = summary["cost"]
        self.assertEqual(cost["scrapedo_credits"], 30)              # 2 x (10 + 5)
        self.assertEqual(cost["scrapedo_ai_overview_successful"], 2)
        self.assertEqual(cost["scrapedo_ai_overview_deferred"], 2)
        self.assertEqual(summary["empty_response_breakdown"]["deferred"], 2)


class BatchModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeS3()
        _seed(self.fake)

    async def _executor(self, **kwargs):
        # Batch mode: the row task captures the overview and defers the fields.
        return _response(fields=None)

    def test_batch_phase_fills_the_fields(self) -> None:
        summary = _drive(self.fake, self._executor, batch=True)
        with _patched(self.fake):
            keys = sorted(store.iter_keys(PREFIX))
        self.assertTrue(any("/cleaned/" in k for k in keys))
        rows = _csv_rows(self.fake, "enriched.csv")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["address"], FIELDS["address"])
        self.assertEqual(summary["llm_mode"], "batch")
        self.assertTrue(summary["is_batch"])

    def test_a_batch_that_answers_nothing_leaves_rows_not_enriched(self) -> None:
        summary = _drive(self.fake, self._executor, batch=True,
                         batch_results={k: None for k in FIELDS})
        self.assertEqual(summary["outcome_breakdown"]["found"], 0)
        self.assertEqual(len(_csv_rows(self.fake, "notEnriched.csv")), 3)

    def test_resume_skips_rows_that_already_have_fields(self) -> None:
        """A second drive must buy nothing: every phase skips work with an object."""
        _drive(self.fake, self._executor, batch=True)
        calls = []

        async def counting(**kwargs):
            calls.append(1)
            return _response(fields=None)

        submitted = []

        def counting_batch(prefix, items, counters=None):
            submitted.extend(items)
            return {}

        async def go():
            with _patched(self.fake), \
                    mock.patch.dict(os.environ, {"LLM_BATCH": "true"}), \
                    mock.patch.object(runner, "execute_firmographic_extraction", counting), \
                    mock.patch.object(runner, "_run_gemini_batch", counting_batch):
                await runner._phases(PREFIX, store.Counters(PREFIX, rows_total=3), {})
        asyncio.run(go())
        self.assertEqual(calls, [], "re-drive re-scraped rows that already had results")
        self.assertEqual(submitted, [], "re-drive resubmitted rows that already had fields")


class DeadGeminiShardTests(unittest.TestCase):
    """The scrape succeeded and was billed; the Gemini shard never delivered.

    Everything here is about telling that apart from "Google had no AI Overview", which
    looks identical in the objects but is FINAL — rerunning it re-buys the same silence,
    while rerunning this costs Gemini tokens only.
    """

    def setUp(self) -> None:
        self.fake = FakeS3()
        _seed(self.fake)

        async def executor(**kwargs):
            return _response(fields=None)          # overview captured, deferred to batch

        def dead_shard(prefix, items, counters=None):
            raise RuntimeError("Gemini batch batches/abc ended JOB_STATE_EXPIRED")

        async def go():
            with _patched(self.fake), \
                    mock.patch.dict(os.environ, {"LLM_BATCH": "true",
                                                 "SCRAPEDO_CONCURRENCY": "2"}), \
                    mock.patch.object(runner, "execute_firmographic_extraction", executor), \
                    mock.patch.object(runner, "_run_gemini_batch", dead_shard):
                return await runner._phases(PREFIX, store.Counters(PREFIX, rows_total=3), {})

        self.summary = asyncio.run(go())

    def test_the_run_reports_the_failure_instead_of_a_clean_finish(self) -> None:
        self.assertEqual(self.summary["status"], "completed_with_errors")
        self.assertGreater(self.summary["error_breakdown"]["task_errors"], 0,
                           "a dead shard left no trace in the summary")

    def test_unjudged_rows_get_their_own_bucket_not_no_ai_overview(self) -> None:
        """These rows HAD an overview — that is why they were deferred. Counting them as
        no_ai_overview both overstated what Google failed to give us and hid the only
        outcome in that card a rerun can fix."""
        eb = self.summary["empty_response_breakdown"]
        self.assertEqual(eb["llm_incomplete"], 2)
        self.assertEqual(eb["no_ai_overview"], 0)

    def test_the_note_says_it_is_retryable_and_free_on_the_provider(self) -> None:
        rows = {r["website_url"]: r for r in _csv_rows(self.fake, "notEnriched.csv")}
        note = rows["https://acme.com"]["enrichment_note"]
        self.assertIn("LLM never completed", note)
        self.assertIn("no scrape.do re-spend", note)
        self.assertNotEqual(note, "No firmographics extracted.")

    def test_they_land_in_retry_csv(self) -> None:
        retry = _csv_rows(self.fake, "retry.csv")
        self.assertEqual(len(retry), 2, "unjudged rows missing from the rerun list")
        self.assertIn("llm_incomplete", retry[0]["retry_reason"])

    def test_a_row_gemini_ANSWERED_emptily_is_final_not_retryable(self) -> None:
        """The other side of the line: a cleaned/ object exists with no fields, meaning
        Gemini read the overview and found nothing. Rerunning re-buys that silence, so it
        must NOT be marked retryable."""
        fake = FakeS3()
        _seed(fake)

        async def executor(**kwargs):
            return _response(fields=None)

        def empty_answer(prefix, items, counters=None):
            return {key: {"parsed": {}, "usage": {}, "model": "m"} for key, _b in items}

        async def go():
            with _patched(fake), \
                    mock.patch.dict(os.environ, {"LLM_BATCH": "true"}), \
                    mock.patch.object(runner, "execute_firmographic_extraction", executor), \
                    mock.patch.object(runner, "_run_gemini_batch", empty_answer):
                return await runner._phases(PREFIX, store.Counters(PREFIX, rows_total=3), {})

        summary = asyncio.run(go())
        self.assertEqual(summary["empty_response_breakdown"]["llm_incomplete"], 0)
        self.assertEqual(_csv_rows(fake, "retry.csv"), [],
                         "a row Gemini answered was offered for rerun")


class ProviderErrorTests(unittest.TestCase):
    def test_a_dead_row_is_an_error_and_lands_in_retry_csv(self) -> None:
        fake = FakeS3()
        _seed(fake)

        async def executor(**kwargs):
            return _response(fields=None, overview=False, error="HTTP 429 rate limited")

        summary = _drive(fake, executor)
        self.assertEqual(summary["outcome_breakdown"]["errored"], 2)
        self.assertEqual(summary["status"], "completed_with_errors")
        self.assertEqual(summary["error_breakdown"]["by_source"], {"scrapedo": 2})
        retry = _csv_rows(fake, "retry.csv")
        self.assertEqual(len(retry), 2)
        self.assertIn("429", retry[0]["retry_reason"])
        # retry.csv is re-uploadable: the ORIGINAL columns, plus one reason column.
        self.assertEqual(set(retry[0]) - {"retry_reason"},
                         {"website_url", "company_name", "country"})

    def test_billed_search_with_no_overview_is_a_refund_claim(self) -> None:
        fake = FakeS3()
        _seed(fake)

        async def executor(**kwargs):
            return _response(fields=None, overview=False)

        summary = _drive(fake, executor)
        self.assertEqual(summary["outcome_breakdown"]["errored"], 0)
        self.assertEqual(summary["empty_response_breakdown"]["no_ai_overview"], 2)
        self.assertEqual(summary["cost"]["scrapedo_billed_empty"], 2)
        retry = _csv_rows(fake, "retry.csv")
        self.assertEqual(len(retry), 2)
        self.assertIn("refundable", retry[0]["retry_reason"])


class StopTests(unittest.TestCase):
    def test_a_stop_still_writes_the_outputs(self) -> None:
        """A stop must not discard a run: the scraped rows and the CSVs survive."""
        fake = FakeS3()
        _seed(fake)
        with _patched(fake):
            store.request_stop(PREFIX)

        async def executor(**kwargs):
            return _response(fields=FIELDS)

        summary = _drive(fake, executor)
        self.assertEqual(summary["status"], "stopped")
        with _patched(fake):
            self.assertIsNotNone(store.get_bytes(f"{PREFIX}/enriched.csv"))
            self.assertEqual(store.read_status(PREFIX)["phase"], "stopped")


if __name__ == "__main__":
    unittest.main()
