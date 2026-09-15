"""Phase 1: resume-by-object-presence, concurrency, and credit accounting."""
import asyncio
import json
import os
import time
import unittest
from unittest import mock

from app.services import ai_mode as ai_mode_pkg
# Imported for its side effect: mock.patch.object(ai_mode_pkg, "gemini_batch") below
# needs the submodule attribute to exist, and only an import sets it.
from app.services.ai_mode import gemini_batch as _gemini_batch  # noqa: F401
from app.services.common import llm_batch
from app.services.serpwow import relationship_runner as runner
from app.services.serpwow import s3_run_driver as driver
from app.services.serpwow import s3_run_store as store
from tests.test_s3_run_store import FakeS3, _patched

CSV = (b"Input_URL,Company_Name_X,Company_Name_Y,country\n"
       b"https://acme.com/p,Acme,Sanzo,US\n"
       b"https://acme.com/p,Acme,Yuzu,US\n"
       b"https://acme.com/p,Acme,Pomelo,US\n")

# The shape search_ai_mode returns: the provider's body under "response" (and the exact
# wire text alongside it), our call bookkeeping at the top level. write_row splits them
# so raw/ holds only the body.
OK_BODY = {"text_blocks": [{"snippet": "Acme invested in it."}],
           "references": [{"title": "Y", "link": "https://y.com"}]}
OK_ENVELOPE = {
    "query": "q", "request_count": 1, "successful_requests": 1, "failed_requests": 0,
    "credits": 10, "response": OK_BODY, "response_text": json.dumps(OK_BODY),
    "billed_empty": False, "error": None, "error_category": None,
}
ERR_ENVELOPE = {
    "query": "q", "request_count": 4, "successful_requests": 0, "failed_requests": 4,
    "credits": 0, "response": None, "response_text": None, "billed_empty": False,
    "error": "HTTP 529", "error_category": "rate_limit",
}

PREFIX = "acme/relationship/run1"


def _seed(fake):
    with _patched(fake):
        store.put_bytes(store.input_key(PREFIX), CSV)


def _drive(fake, envelopes):
    """Run phase 1 with search_ai_mode stubbed to return envelopes in order."""
    calls = []

    async def fake_search(query, gl="us", client=None):
        calls.append(query)
        return envelopes[len(calls) - 1] if len(calls) <= len(envelopes) else OK_ENVELOPE

    with _patched(fake), mock.patch.object(runner, "search_ai_mode", fake_search), \
            mock.patch.dict(os.environ, {"SCRAPEDO_CONCURRENCY": "2"}, clear=False):
        counters = store.Counters(PREFIX, rows_total=3)
        asyncio.run(runner.run_scrape_phase(PREFIX, counters))
    return calls, counters


class ScrapePhaseTests(unittest.TestCase):
    def test_every_row_is_scraped_and_persisted(self) -> None:
        fake = FakeS3()
        _seed(fake)
        calls, counters = _drive(fake, [OK_ENVELOPE] * 3)

        self.assertEqual(len(calls), 3)
        with _patched(fake):
            for idx in (0, 1, 2):
                self.assertIsNotNone(store.get_object(store.raw_key(PREFIX, idx)))
        self.assertEqual(counters.values["rows_scraped"], 3)
        self.assertEqual(counters.values["credits"], 30)

    def test_the_prompt_carries_this_rows_company_y(self) -> None:
        fake = FakeS3()
        _seed(fake)
        calls, _ = _drive(fake, [OK_ENVELOPE] * 3)
        joined = " ".join(calls)
        for name in ("Sanzo", "Yuzu", "Pomelo"):
            self.assertIn(name, joined)

    def test_a_redrive_skips_rows_that_already_have_an_object(self) -> None:
        """Resume: this is why a crash costs no credits."""
        fake = FakeS3()
        _seed(fake)
        with _patched(fake):
            store.write_row(PREFIX, 0, OK_ENVELOPE)
            store.put_object(store.error_key(PREFIX, 1), ERR_ENVELOPE)

        calls, _ = _drive(fake, [OK_ENVELOPE])
        self.assertEqual(len(calls), 1)          # only row 2 was scraped
        self.assertIn("Pomelo", calls[0])

    def test_a_fully_scraped_run_issues_zero_calls(self) -> None:
        fake = FakeS3()
        _seed(fake)
        with _patched(fake):
            for idx in (0, 1, 2):
                store.write_row(PREFIX, idx, OK_ENVELOPE)
        calls, _ = _drive(fake, [OK_ENVELOPE])
        self.assertEqual(calls, [])

    def test_a_dead_row_writes_an_error_marker_not_a_raw_object(self) -> None:
        fake = FakeS3()
        _seed(fake)
        _drive(fake, [ERR_ENVELOPE, OK_ENVELOPE, OK_ENVELOPE])

        with _patched(fake):
            self.assertIsNotNone(store.get_object(store.error_key(PREFIX, 0)))
            self.assertIsNone(store.get_object(store.raw_key(PREFIX, 0)))

    def test_failed_attempts_cost_no_credits(self) -> None:
        fake = FakeS3()
        _seed(fake)
        _, counters = _drive(fake, [ERR_ENVELOPE, OK_ENVELOPE, OK_ENVELOPE])
        self.assertEqual(counters.values["rows_failed"], 1)
        self.assertEqual(counters.values["credits"], 20)   # only the 2 successes
        self.assertEqual(counters.values["requests"], 4 + 1 + 1)

    def test_billed_empty_is_counted_and_is_not_a_failure(self) -> None:
        empty = {**OK_ENVELOPE, "text_blocks": [], "references": [],
                 "billed_empty": True}
        fake = FakeS3()
        _seed(fake)
        _, counters = _drive(fake, [empty, OK_ENVELOPE, OK_ENVELOPE])
        self.assertEqual(counters.values["rows_billed_empty"], 1)
        self.assertEqual(counters.values["rows_failed"], 0)
        self.assertEqual(counters.values["rows_scraped"], 3)

    def test_stop_marker_halts_the_phase_early(self) -> None:
        fake = FakeS3()
        _seed(fake)
        with _patched(fake):
            store.request_stop(PREFIX)
        calls, _ = _drive(fake, [OK_ENVELOPE] * 3)
        self.assertEqual(calls, [])

    def test_an_s3_put_failure_marks_the_row_failed_rather_than_losing_it(self) -> None:
        """With no local copy, a swallowed PUT would silently drop the row's work."""
        fake = FakeS3(fail_keys={store.raw_key(PREFIX, 0)})
        _seed(fake)
        _, counters = _drive(fake, [OK_ENVELOPE] * 3)
        self.assertEqual(counters.values["rows_failed"], 1)
        self.assertEqual(counters.values["rows_scraped"], 2)

    def test_a_raising_row_task_is_counted_rather_than_swallowed(self) -> None:
        """asyncio.wait hands back Tasks whose exception is only realised if someone
        ASKS for it. The realistic trigger: the prompt file operators are invited to edit
        goes through .format(), so one stray "{" makes build_relationship_search_query
        raise KeyError on EVERY row. Unretrieved, that killed all tasks, wrote no object,
        moved no counter, "completed" the phase normally, and left a GC warning as the
        only trace."""
        fake = FakeS3()
        _seed(fake)

        def boom(**kwargs):
            raise KeyError("relationship_confidence_score")

        with _patched(fake), \
                mock.patch.object(runner, "build_relationship_search_query", boom), \
                mock.patch.dict(os.environ, {"SCRAPEDO_CONCURRENCY": "2"}, clear=False):
            counters = store.Counters(PREFIX, rows_total=3)
            asyncio.run(runner.run_scrape_phase(PREFIX, counters))   # must not raise

        self.assertEqual(counters.values["task_errors"], 3)
        self.assertEqual(counters.values["rows_scraped"], 0)
        with _patched(fake):
            self.assertIsNone(store.get_object(store.raw_key(PREFIX, 0)))
            # And it is durable, not just in memory: the operator sees it in status.json.
            self.assertEqual(store.read_status(PREFIX)["task_errors"], 3)


class StreamingTests(unittest.TestCase):
    def test_input_rows_are_never_materialised_as_a_list(self) -> None:
        """At 500k rows a list of row dicts is hundreds of MB. The runner must consume
        the CSV iterator lazily, bounded by the concurrency window."""
        fake = FakeS3()
        _seed(fake)
        live = {"max": 0, "now": 0}
        real_iter = store.iter_input_rows

        def counting_iter(prefix):
            for row in real_iter(prefix):
                live["now"] += 1
                live["max"] = max(live["max"], live["now"])
                yield row

        async def fake_search(query, gl="us", client=None):
            await asyncio.sleep(0)
            live["now"] -= 1
            return OK_ENVELOPE

        with _patched(fake), \
                mock.patch.object(store, "iter_input_rows", counting_iter), \
                mock.patch.object(runner, "search_ai_mode", fake_search), \
                mock.patch.dict(os.environ, {"SCRAPEDO_CONCURRENCY": "2"},
                                clear=False):
            counters = store.Counters(PREFIX, rows_total=3)
            asyncio.run(runner.run_scrape_phase(PREFIX, counters))

        # Never more rows pulled from the CSV than the concurrency window (2) allows.
        # (rows_total is also 3 here, so asserting against 3 would pass either way —
        # the real claim is bounded by SCRAPEDO_CONCURRENCY, not by row count.)
        # This bound is timing-sensitive: it counts CSV pulls, not in-flight tasks, so a
        # slower stub could legitimately observe limit + 1 if a row is pulled just before
        # a completing task frees its slot. See the invariant test below for the bound
        # that holds regardless of stub timing.
        self.assertLessEqual(live["max"], 2)

    def test_live_tasks_never_exceed_the_concurrency_limit(self) -> None:
        """The hard invariant, asserted directly: count concurrent search_ai_mode calls
        (a tight proxy for len(tasks)) instead of CSV pulls, so the bound holds
        regardless of stub timing. A slower stub (sleep(0.01), as the reviewer used to
        reproduce the pull-ahead edge case) makes sure a real breach would show up."""
        fake = FakeS3()
        _seed(fake)
        live = {"max": 0, "now": 0}

        async def fake_search(query, gl="us", client=None):
            live["now"] += 1
            live["max"] = max(live["max"], live["now"])
            await asyncio.sleep(0.01)
            live["now"] -= 1
            return OK_ENVELOPE

        with _patched(fake), mock.patch.object(runner, "search_ai_mode", fake_search), \
                mock.patch.dict(os.environ, {"SCRAPEDO_CONCURRENCY": "2"},
                                clear=False):
            counters = store.Counters(PREFIX, rows_total=3)
            asyncio.run(runner.run_scrape_phase(PREFIX, counters))

        self.assertLessEqual(live["max"], 2)

    def test_the_blocking_csv_read_does_not_freeze_the_event_loop(self) -> None:
        """Finding 1 regression: `asyncio.to_thread(_iter_pending, prefix, done)` only
        constructs the generator object (near-instant, no I/O) — the blocking S3 read
        happens wherever next() is actually called. If that next() runs on the event
        loop, a slow row read stalls every in-flight scrape.do call. Prove the loop
        stays responsive during a slow read via a background heartbeat."""
        fake = FakeS3()
        _seed(fake)
        real_iter = store.iter_input_rows

        def slow_iter(prefix):
            for row in real_iter(prefix):
                time.sleep(0.05)  # simulate a blocking network read
                yield row

        heartbeat = {"ticks": 0}

        async def ticker():
            while True:
                heartbeat["ticks"] += 1
                await asyncio.sleep(0.01)

        async def fake_search(query, gl="us", client=None):
            return OK_ENVELOPE

        async def drive():
            tick_task = asyncio.create_task(ticker())
            counters = store.Counters(PREFIX, rows_total=3)
            await runner.run_scrape_phase(PREFIX, counters)
            tick_task.cancel()

        with _patched(fake), mock.patch.object(store, "iter_input_rows", slow_iter), \
                mock.patch.object(runner, "search_ai_mode", fake_search), \
                mock.patch.dict(os.environ, {"SCRAPEDO_CONCURRENCY": "2"},
                                clear=False):
            asyncio.run(drive())

        # 3 rows * 0.05s blocking read = 0.15s total; a frozen event loop would tick
        # near zero times during that window. A responsive one ticks roughly every
        # 0.01s, so 5+ ticks is a solid margin.
        self.assertGreaterEqual(heartbeat["ticks"], 5)


class StopCheckFrequencyTests(unittest.TestCase):
    def test_stop_check_frequency_is_time_bounded_not_row_bounded(self) -> None:
        """Finding 2: with independently varying scrape latencies, asyncio.wait
        (FIRST_COMPLETED) wakes the dispatch loop roughly once per completion, so a
        once-per-outer-iteration check was effectively once-per-row in steady state
        (reviewer measured 218 checks over 300 rows at limit=100, i.e. 73%). The check
        must be time-gated so its frequency is bounded by construction, not by how
        completions happen to interleave. Sleeps are deliberately varied (not the
        uniform stub other tests use) — a uniform stub synchronises completions and
        is exactly what made the old, non-time-gated bound look tighter than it is."""
        rows = 60
        csv_bytes = b"Input_URL,Company_Name_X,Company_Name_Y,country\n" + b"".join(
            f"https://acme.com/p,Acme,Y{i},US\n".encode() for i in range(rows))
        fake = FakeS3()
        with _patched(fake):
            store.put_bytes(store.input_key(PREFIX), csv_bytes)

        delays = [0.001, 0.006, 0.002, 0.008, 0.003, 0.005]
        call_count = {"n": 0}

        async def fake_search(query, gl="us", client=None):
            i = call_count["n"]
            call_count["n"] += 1
            await asyncio.sleep(delays[i % len(delays)])
            return OK_ENVELOPE

        real_stop_requested = store.stop_requested
        stop_calls = {"n": 0}

        def counting_stop(prefix):
            stop_calls["n"] += 1
            return real_stop_requested(prefix)

        with _patched(fake), mock.patch.object(runner, "search_ai_mode", fake_search), \
                mock.patch.object(store, "stop_requested", counting_stop), \
                mock.patch.object(driver, "_STOP_CHECK_INTERVAL_SEC", 0.02), \
                mock.patch.dict(os.environ, {"SCRAPEDO_CONCURRENCY": "20"},
                                clear=False):
            counters = store.Counters(PREFIX, rows_total=rows)
            asyncio.run(runner.run_scrape_phase(PREFIX, counters))

        # A time-gated check over this run's short wall-clock duration should fire a
        # handful of times, nowhere near once per row.
        self.assertLess(stop_calls["n"], rows // 2)

    def test_stop_marker_set_before_the_first_check_interval_still_halts_immediately(
            self) -> None:
        """The throttle must never skip the very first check: a stop marker set before
        the phase starts has to halt it with zero scrape calls, exactly like the
        untimed version did."""
        fake = FakeS3()
        _seed(fake)
        with _patched(fake):
            store.request_stop(PREFIX)
        # A long interval would still be fine here — the FIRST check is unconditional —
        # but use the production default to prove that, not a test-friendly override.
        calls, _ = _drive(fake, [OK_ENVELOPE] * 3)
        self.assertEqual(calls, [])


class GeminiBatchTimeoutTests(unittest.TestCase):
    """A shard that never terminalises used to park the polling thread FOREVER: the run
    sat in phase="cleaning" while the heartbeat kept it looking healthy, so
    redrive_stale_runs never rescued it either. Now the wait is bounded."""

    def _never_terminal(self):
        gb = mock.Mock()
        gb.create_batch.return_value = {"name": "batches/stuck"}
        gb.batch_name_from_create.return_value = "batches/stuck"
        gb.get_batch.return_value = {"done": False}
        gb.state_name.return_value = "JOB_STATE_RUNNING"
        gb.is_terminal.return_value = False
        return gb

    def test_a_batch_that_never_finishes_raises_instead_of_hanging(self) -> None:
        gb = self._never_terminal()
        slept = []
        fake = FakeS3()
        _seed(fake)
        with _patched(fake), mock.patch.object(ai_mode_pkg, "gemini_batch", gb), \
                mock.patch.dict(os.environ, {"GEMINI_BATCH_TIMEOUT_SEC": "60",
                                             "GEMINI_BATCH_POLL_SEC": "30"}, clear=False), \
                mock.patch("time.sleep", slept.append), \
                mock.patch("time.monotonic", side_effect=[0.0, 10.0, 70.0, 70.0]):
            with self.assertRaises(TimeoutError):
                runner._run_gemini_batch(PREFIX, [("0", {})])
        # It polled and waited rather than bailing on the first pass.
        self.assertTrue(slept)

    def test_it_does_not_raise_while_still_inside_the_deadline(self) -> None:
        gb = self._never_terminal()
        # Terminal on the second poll, so a healthy slow batch is unaffected.
        gb.is_terminal.side_effect = [False, True]
        gb.collect_results.return_value = [{"key": "0", "text": "{}"}]
        gb.parse_json_from_text.return_value = {}
        fake = FakeS3()
        _seed(fake)
        with _patched(fake), mock.patch.object(ai_mode_pkg, "gemini_batch", gb), \
                mock.patch.dict(os.environ, {"GEMINI_BATCH_TIMEOUT_SEC": "99999"},
                                clear=False), \
                mock.patch("time.sleep", lambda _s: None):
            out = runner._run_gemini_batch(PREFIX, [("0", {})])
        self.assertEqual(out["0"]["parsed"], {})
        # usage + model are kept now, not discarded — the UI's token/model tiles read them.
        self.assertIn("usage", out["0"])
        # Assert against the RESOLVER, not os.getenv with a default: a blank-but-present
        # GEMINI_BATCH_MODEL (which tests/__init__ sets, so the suite ignores a developer
        # .env) makes os.getenv return "" while llm_batch correctly treats it as unset.
        self.assertEqual(out["0"]["model"], llm_batch.batch_model())


class VerdictPhaseTests(unittest.TestCase):
    def _seed_raw(self, fake, n=3):
        _seed(fake)
        with _patched(fake):
            for idx in range(n):
                store.write_row(PREFIX, idx, {
                    **OK_ENVELOPE, "row_index": idx, "x_domain": "acme.com",
                    "fields": {"row_index": idx, "x_name": "Acme", "y_name": f"Y{idx}",
                               "input_url": "https://acme.com/p", "city": "",
                               "country": "US"},
                })

    def test_a_verdict_object_is_written_per_row_with_its_candidate_set(self) -> None:
        fake = FakeS3()
        self._seed_raw(fake)

        def fake_batch(prefix_arg, items, counters=None):
            return {key: {"parsed": {"relationship_status": "confirmed",
                                     "official_website": "https://y.com",
                                     "relationship_confidence_score": 90,
                                     "website_confidence_score": 90},
                          "usage": {"promptTokenCount": 11,
                                    "candidatesTokenCount": 7},
                          "model": "gemini-2.5-flash-lite"} for key, _body in items}

        with _patched(fake), mock.patch.object(runner, "_run_gemini_batch", fake_batch):
            counters = store.Counters(PREFIX, rows_total=3)
            asyncio.run(runner.run_verdict_phase(PREFIX, counters))

        with _patched(fake):
            cleaned = store.get_object(store.cleaned_key(PREFIX, 0))
        self.assertEqual(cleaned["parsed"]["relationship_status"], "confirmed")
        # Stored so phase 3 reads cleaned/ only, not cleaned/ + raw/.
        self.assertIn("https://y.com", cleaned["candidates"])
        self.assertEqual(cleaned["x_domain"], "acme.com")

    def test_rows_that_already_have_a_verdict_are_not_resubmitted(self) -> None:
        fake = FakeS3()
        self._seed_raw(fake)
        with _patched(fake):
            store.put_object(store.cleaned_key(PREFIX, 0),
                             {"row_index": 0, "parsed": {}, "candidates": []})
        submitted = {}

        def fake_batch(prefix_arg, items, counters=None):
            submitted["keys"] = [k for k, _b in items]
            return {k: {} for k, _b in items}

        with _patched(fake), mock.patch.object(runner, "_run_gemini_batch", fake_batch):
            asyncio.run(runner.run_verdict_phase(
                PREFIX, store.Counters(PREFIX, rows_total=3)))

        self.assertNotIn("0", submitted["keys"])
        self.assertEqual(sorted(submitted["keys"]), ["1", "2"])

    def test_error_rows_are_skipped_entirely(self) -> None:
        fake = FakeS3()
        _seed(fake)
        with _patched(fake):
            store.put_object(store.error_key(PREFIX, 0), ERR_ENVELOPE)
        submitted = {}

        def fake_batch(prefix_arg, items, counters=None):
            submitted["keys"] = [k for k, _b in items]
            return {}

        with _patched(fake), mock.patch.object(runner, "_run_gemini_batch", fake_batch):
            asyncio.run(runner.run_verdict_phase(
                PREFIX, store.Counters(PREFIX, rows_total=1)))

        self.assertEqual(submitted["keys"], [])

    def test_a_zero_evidence_row_costs_zero_gemini_requests(self) -> None:
        """A billed_empty row (HTTP 200, no text_blocks, no references) has a perfectly
        parseable envelope, so the skip-if-unparseable gate let it into a shard and paid
        for it — then build_row_result's has_evidence check threw the verdict away and
        stamped REL_ERROR_NO_EVIDENCE anyway. ~15k wasted Batch requests at 500k rows
        with 3% empty responses."""
        fake = FakeS3()
        _seed(fake)
        with _patched(fake):
            store.write_row(PREFIX, 0, {
                **OK_ENVELOPE, "row_index": 0, "x_domain": "acme.com",
                # Empty ARRAYS, which now live inside the provider body.
                "response": {"text_blocks": [], "references": []},
                "response_text": '{"text_blocks":[],"references":[]}',
                "billed_empty": True,
                "fields": {"row_index": 0, "x_name": "Acme", "y_name": "Y0",
                           "input_url": "https://acme.com/p", "city": "",
                           "country": "US"}})
        submitted: list[str] = []

        def fake_batch(prefix_arg, items, counters=None):
            submitted.extend(key for key, _body in items)
            return {}

        with _patched(fake), mock.patch.object(runner, "_run_gemini_batch", fake_batch):
            counters = store.Counters(PREFIX, rows_total=1)
            asyncio.run(runner.run_verdict_phase(PREFIX, counters))

        self.assertEqual(submitted, [])
        # The cleaned object is still written, so phase 3 reaches the same verdict and a
        # re-drive skips the row instead of re-considering it forever.
        with _patched(fake):
            cleaned = store.get_object(store.cleaned_key(PREFIX, 0))
        self.assertIsNone(cleaned["parsed"])
        self.assertEqual(counters.values["rows_cleaned"], 1)

    def test_a_raising_shard_is_counted_not_silently_discarded(self) -> None:
        """submit() wraps create_batch/collect_results AND a raising put_object. One
        Gemini quota error or S3 blip used to discard a whole already-PAID-FOR 5000-row
        shard with no trace: gather(return_exceptions=True) collected the exception and
        nobody looked at it, and the rows resurfaced as plain unclear/llm_missing."""
        fake = FakeS3()
        self._seed_raw(fake)

        def boom(prefix_arg, items, counters=None):
            raise RuntimeError("gemini: quota exceeded for concurrent batches")

        with _patched(fake), mock.patch.object(runner, "_run_gemini_batch", boom):
            counters = store.Counters(PREFIX, rows_total=3)
            asyncio.run(runner.run_verdict_phase(PREFIX, counters))   # must not raise

        self.assertEqual(counters.values["task_errors"], 1)
        self.assertEqual(counters.values["rows_cleaned"], 0)
        with _patched(fake):
            self.assertIsNone(store.get_object(store.cleaned_key(PREFIX, 0)))

    def test_a_stop_stops_submitting_further_gemini_shards(self) -> None:
        """Without a stop check in this loop, a stop at row 1 of a 500k run still bought
        all 100 remaining shards. Checked once per shard boundary (not per row) and
        BEFORE the shard goes out, so a stop costs zero further batches."""
        fake = FakeS3()
        self._seed_raw(fake)
        with _patched(fake):
            store.request_stop(PREFIX)
        submitted: list[list[str]] = []

        def fake_batch(prefix_arg, items, counters=None):
            submitted.append([key for key, _body in items])
            return {}

        # Shard size 2 over 3 pending rows: the boundary is reached at row 1, which is
        # where the stop check lives. The partial tail must not be bought either.
        env = {"GEMINI_BATCH_SHARD_SIZE": "2"}
        with _patched(fake), mock.patch.object(runner, "_run_gemini_batch", fake_batch), \
                mock.patch.dict(os.environ, env, clear=False):
            asyncio.run(runner.run_verdict_phase(
                PREFIX, store.Counters(PREFIX, rows_total=3)))
            self.assertEqual(submitted, [])

            # Control: the identical fixture with the stop lifted DOES submit, so the
            # gate is the stop marker and not a broken shard-boundary path.
            store.clear_stop(PREFIX)
        with _patched(fake), mock.patch.object(runner, "_run_gemini_batch", fake_batch), \
                mock.patch.dict(os.environ, env, clear=False):
            asyncio.run(runner.run_verdict_phase(
                PREFIX, store.Counters(PREFIX, rows_total=3)))
        self.assertEqual(submitted, [["0", "1"], ["2"]])


class DriveTests(unittest.TestCase):
    def test_drive_run_executes_all_three_phases_in_order(self) -> None:
        fake = FakeS3()
        _seed(fake)
        order = []

        async def phase(name):
            order.append(name)

        with _patched(fake), \
                mock.patch.object(runner, "run_scrape_phase",
                                  lambda p, c: phase("scrape")), \
                mock.patch.object(runner, "run_verdict_phase",
                                  lambda p, c: phase("verdict")), \
                mock.patch.object(runner, "write_outputs",
                                  lambda p, c: order.append("outputs") or {}), \
                mock.patch.object(driver, "notify_terminal"):
            store.write_run_pointer("run1", PREFIX, "Acme")
            asyncio.run(runner.drive_run("run1"))

        self.assertEqual(order, ["scrape", "verdict", "outputs"])

    def test_drive_run_on_an_unknown_id_is_a_noop_not_a_crash(self) -> None:
        fake = FakeS3()
        with _patched(fake):
            asyncio.run(runner.drive_run("nope"))   # must not raise

    def test_drive_run_notifies_after_a_completed_run(self) -> None:
        fake = FakeS3()
        _seed(fake)
        notified = []
        with _patched(fake), \
                mock.patch.object(runner, "run_scrape_phase",
                                  lambda p, c: asyncio.sleep(0)), \
                mock.patch.object(runner, "run_verdict_phase",
                                  lambda p, c: asyncio.sleep(0)), \
                mock.patch.object(runner, "write_outputs",
                                  lambda p, c: {"status": "completed"}), \
                mock.patch.object(driver, "notify_terminal",
                                  lambda rid, ptr, summary, **kw: notified.append(
                                      (rid, summary))):
            store.write_run_pointer("run1", PREFIX, "Acme")
            asyncio.run(runner.drive_run("run1"))
        self.assertEqual(notified, [("run1", {"status": "completed"})])

    def test_a_stop_between_phase_1_and_2_still_writes_outputs_and_terminalizes(
            self) -> None:
        """A stop must SKIP the verdict phase, not discard the run. The earlier version of
        this test pinned the opposite (return before write_outputs/_notify_terminal),
        which meant "Stop" on day 3 of a 500k run threw away every scraped row: no
        confirmed_relation.csv at all (write_outputs handles a missing cleaned/ object
        fine — parsed=None -> "unclear"), Supabase left on create_run's "queued" forever,
        and /status mapping phase="stopped" to "completed" so the detail page showed a
        completed run with an empty Files card."""
        fake = FakeS3()
        _seed(fake)
        with _patched(fake):
            store.write_run_pointer("run1", PREFIX, "Acme")
            store.request_stop(PREFIX)
        called = []
        with _patched(fake), \
                mock.patch.object(runner, "run_scrape_phase",
                                  lambda p, c: asyncio.sleep(0)), \
                mock.patch.object(runner, "run_verdict_phase",
                                  lambda p, c: called.append("verdict")
                                  or asyncio.sleep(0)), \
                mock.patch.object(runner, "write_outputs",
                                  lambda p, c: called.append("outputs")
                                  or {"status": "stopped"}), \
                mock.patch.object(driver, "notify_terminal",
                                  lambda *a, **kw: called.append("notify")):
            asyncio.run(runner.drive_run("run1"))
        # Outputs and terminalization happen; only the money-spending phase is skipped.
        self.assertEqual(called, ["outputs", "notify"])

    def test_a_phase_exception_marks_the_run_failed_not_crashing_the_caller(self) -> None:
        fake = FakeS3()
        _seed(fake)
        with _patched(fake):
            store.write_run_pointer("run1", PREFIX, "Acme")

        async def boom(p, c):
            raise RuntimeError("scrape.do is on fire")

        with _patched(fake), mock.patch.object(runner, "run_scrape_phase", boom):
            asyncio.run(runner.drive_run("run1"))   # must not raise

        with _patched(fake):
            self.assertEqual(store.read_status(PREFIX)["phase"], "failed")

    def _svc_statuses(self, svc) -> list:
        return [call.kwargs.get("status") for call in svc.update_run.call_args_list]

    def test_supabase_is_told_the_run_is_running_and_then_terminalized(self) -> None:
        """_notify_terminal used to be the ONLY update_run call, so a healthy 6-day 500k
        run read create_run's "queued" in the Runs list for its entire life."""
        fake = FakeS3()
        _seed(fake)
        svc = mock.Mock()
        with _patched(fake), \
                mock.patch("app.services.companies.get_company_service",
                           return_value=svc), \
                mock.patch.object(runner, "run_scrape_phase",
                                  lambda p, c: asyncio.sleep(0)), \
                mock.patch.object(runner, "run_verdict_phase",
                                  lambda p, c: asyncio.sleep(0)), \
                mock.patch.object(runner, "write_outputs",
                                  lambda p, c: {"status": "completed"}), \
                mock.patch("app.core.notify.notify_run_complete"):
            store.write_run_pointer("run1", PREFIX, "Acme", run_db_id="db-1")
            asyncio.run(runner.drive_run("run1"))
        self.assertEqual(self._svc_statuses(svc), ["running", "completed"])

    def test_a_failed_run_terminalizes_supabase_instead_of_staying_queued(self) -> None:
        fake = FakeS3()
        _seed(fake)
        svc = mock.Mock()

        async def boom(p, c):
            raise RuntimeError("scrape.do is on fire")

        with _patched(fake), \
                mock.patch("app.services.companies.get_company_service",
                           return_value=svc), \
                mock.patch.object(runner, "run_scrape_phase", boom):
            store.write_run_pointer("run1", PREFIX, "Acme", run_db_id="db-1")
            asyncio.run(runner.drive_run("run1"))
        self.assertEqual(self._svc_statuses(svc), ["running", "failed"])

    def test_supabase_failures_never_break_a_drive(self) -> None:
        """Every Supabase call in this pipeline is bookkeeping — it must not fail a run
        that is otherwise fine."""
        fake = FakeS3()
        _seed(fake)
        svc = mock.Mock()
        svc.update_run.side_effect = RuntimeError("supabase down")
        with _patched(fake), \
                mock.patch("app.services.companies.get_company_service",
                           return_value=svc), \
                mock.patch.object(runner, "run_scrape_phase",
                                  lambda p, c: asyncio.sleep(0)), \
                mock.patch.object(runner, "run_verdict_phase",
                                  lambda p, c: asyncio.sleep(0)), \
                mock.patch.object(runner, "write_outputs",
                                  lambda p, c: {"status": "completed"}), \
                mock.patch("app.core.notify.notify_run_complete"):
            store.write_run_pointer("run1", PREFIX, "Acme", run_db_id="db-1")
            asyncio.run(runner.drive_run("run1"))   # must not raise
        # Both attempts were made and both blew up harmlessly — the run still completed
        # through write_outputs/_notify_terminal rather than diverting to the failed path.
        self.assertEqual(self._svc_statuses(svc), ["running", "completed"])


class NotifyTerminalTests(unittest.TestCase):
    """_notify_terminal must attempt both channels and never raise, even if either
    dependency (Supabase, Slack) is unavailable or throws. Supabase goes through
    get_company_service().update_run(...) directly — NOT engine._update_supabase_run,
    whose rows-shaped REPORTING_PIPELINES branch would zero out every stat this pipeline
    doesn't keep in state["rows"] (found/not_found/cost/success/failed/file_links)."""

    def test_never_raises_when_both_notifiers_blow_up(self) -> None:
        summary = {"status": "completed", "cost": {}, "outcome_breakdown": {},
                   "websites_found": 1, "websites_not_found": 0, "total_rows": 1}
        pointer = {"run_db_id": "db-1", "company_name": "Acme"}
        fake_svc = mock.Mock()
        fake_svc.update_run.side_effect = RuntimeError("supabase down")
        with mock.patch("app.services.companies.get_company_service",
                        return_value=fake_svc), \
                mock.patch("app.core.notify.notify_run_complete",
                          side_effect=RuntimeError("slack down")):
            driver.notify_terminal("run1", pointer, summary,
                                   pipeline="relationship",
                                   search_label="Scrape.do searches")   # must not raise

    def test_both_channels_are_attempted_with_a_run_db_id(self) -> None:
        summary = {"status": "completed_with_errors",
                   "cost": {"scrapedo_requests": 5, "scrapedo_credits": 50},
                   "outcome_breakdown": {"errored": 1}, "websites_found": 2,
                   "websites_not_found": 1, "total_rows": 3}
        pointer = {"run_db_id": "db-1", "company_name": "Acme"}
        fake_svc = mock.Mock()
        with mock.patch("app.services.companies.get_company_service",
                        return_value=fake_svc), \
                mock.patch("app.core.notify.notify_run_complete") as slack:
            driver.notify_terminal("run1", pointer, summary,
                                   pipeline="relationship",
                                   search_label="Scrape.do searches")

        fake_svc.update_run.assert_called_once()
        args, kwargs = fake_svc.update_run.call_args
        self.assertEqual(args[0], "db-1")
        self.assertEqual(kwargs["status"], "completed_with_errors")
        # The real numbers write_outputs computed, NOT zeros rebuilt from an empty rows
        # list — this is the exact thing the shared engine._update_supabase_run helper
        # would have gotten wrong for this pipeline.
        self.assertEqual(kwargs["websites_found"], 2)
        self.assertEqual(kwargs["websites_not_found"], 1)
        self.assertEqual(kwargs["failed_count"], 1)
        self.assertEqual(kwargs["total_rows"], 3)
        self.assertEqual(kwargs["cost"]["scrapedo_credits"], 50)
        slack.assert_called_once()
        self.assertEqual(slack.call_args.kwargs["status"], "completed_with_errors")

    def test_no_supabase_call_without_a_run_db_id(self) -> None:
        """No run_db_id in the pointer (Supabase was never configured for this run) —
        Supabase must be skipped, not called with a falsy id."""
        summary = {"status": "completed", "cost": {}, "outcome_breakdown": {},
                   "websites_found": 0, "websites_not_found": 0, "total_rows": 0}
        pointer = {"company_name": "Acme"}
        with mock.patch("app.services.companies.get_company_service") as get_svc, \
                mock.patch("app.core.notify.notify_run_complete") as slack:
            driver.notify_terminal("run1", pointer, summary,
                                   pipeline="relationship",
                                   search_label="Scrape.do searches")
        get_svc.assert_not_called()
        slack.assert_called_once()

    def test_no_crash_when_supabase_is_unconfigured(self) -> None:
        """get_company_service() returning None (Supabase env unset) must be a no-op,
        not an AttributeError on None.update_run(...)."""
        summary = {"status": "completed", "cost": {}, "outcome_breakdown": {},
                   "websites_found": 1, "websites_not_found": 0, "total_rows": 1}
        pointer = {"run_db_id": "db-1", "company_name": "Acme"}
        with mock.patch("app.services.companies.get_company_service",
                        return_value=None), \
                mock.patch("app.core.notify.notify_run_complete") as slack:
            driver.notify_terminal("run1", pointer, summary,
                                   pipeline="relationship",
                                   search_label="Scrape.do searches")   # must not raise
        slack.assert_called_once()


class RedriveTests(unittest.TestCase):
    def _pointer(self, fake, status):
        with _patched(fake):
            store.write_run_pointer("run1", PREFIX, "Acme")
            store.put_object(store.status_key(PREFIX), status)

    def test_a_stale_nonterminal_run_is_redriven(self) -> None:
        fake = FakeS3()
        self._pointer(fake, {"phase": "scraping",
                             "updated_at": "2000-01-01T00:00:00Z"})
        driven = []
        with _patched(fake), mock.patch.object(
                runner, "drive_run",
                lambda rid: driven.append(rid) or asyncio.sleep(0)):
            n = asyncio.run(runner.redrive_stale_runs())
        self.assertEqual(driven, ["run1"])
        self.assertEqual(n, 1)

    def test_a_fresh_run_is_left_alone(self) -> None:
        import time
        fake = FakeS3()
        fresh = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._pointer(fake, {"phase": "scraping", "updated_at": fresh})
        driven = []
        with _patched(fake), mock.patch.object(
                runner, "drive_run",
                lambda rid: driven.append(rid) or asyncio.sleep(0)):
            asyncio.run(runner.redrive_stale_runs())
        self.assertEqual(driven, [])

    def test_a_completed_run_is_never_redriven(self) -> None:
        fake = FakeS3()
        self._pointer(fake, {"phase": "completed",
                             "updated_at": "2000-01-01T00:00:00Z"})
        driven = []
        with _patched(fake), mock.patch.object(
                runner, "drive_run",
                lambda rid: driven.append(rid) or asyncio.sleep(0)):
            asyncio.run(runner.redrive_stale_runs())
        self.assertEqual(driven, [])

    def test_a_stopped_run_is_never_redriven(self) -> None:
        """stopped is terminal too — an operator's explicit stop must not be
        auto-resumed by the scan."""
        fake = FakeS3()
        self._pointer(fake, {"phase": "stopped",
                             "updated_at": "2000-01-01T00:00:00Z"})
        driven = []
        with _patched(fake), mock.patch.object(
                runner, "drive_run",
                lambda rid: driven.append(rid) or asyncio.sleep(0)):
            asyncio.run(runner.redrive_stale_runs())
        self.assertEqual(driven, [])

    def _redrive(self, fake):
        driven = []
        with _patched(fake), mock.patch.object(
                runner, "drive_run",
                lambda rid: driven.append(rid) or asyncio.sleep(0)):
            asyncio.run(runner.redrive_stale_runs())
        return driven

    def test_a_failed_run_is_retried(self) -> None:
        """"failed" means drive_run hit an exception, which on a multi-day run is usually
        transient. It used to be a dead end: recovery meant hand-typing the run id into
        the Operations page, even though a re-drive costs nothing."""
        fake = FakeS3()
        self._pointer(fake, {"phase": "failed", "drive_attempts": 1,
                             "updated_at": "2000-01-01T00:00:00Z"})
        self.assertEqual(self._redrive(fake), ["run1"])

    def test_a_repeatedly_failing_run_stops_being_retried(self) -> None:
        fake = FakeS3()
        self._pointer(fake, {"phase": "failed", "drive_attempts": 3,
                             "updated_at": "2000-01-01T00:00:00Z"})
        self.assertEqual(self._redrive(fake), [])

    def test_a_failed_run_still_waits_for_the_staleness_window(self) -> None:
        """Otherwise a run that dies instantly burns all its attempts in one scan pass."""
        import time as _t
        fake = FakeS3()
        fresh = _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())
        self._pointer(fake, {"phase": "failed", "drive_attempts": 0,
                             "updated_at": fresh})
        self.assertEqual(self._redrive(fake), [])

    def test_a_dying_drive_counts_its_attempt(self) -> None:
        """The counter has to be persisted, or the bound above can never be reached."""
        fake = FakeS3()
        _seed(fake)
        with _patched(fake):
            store.write_run_pointer("run1", PREFIX, "Acme")
            store.put_object(store.status_key(PREFIX), {"rows_total": 2, "phase": "queued"})

        async def boom(*_a, **_k):
            raise RuntimeError("S3 stream dropped")

        with _patched(fake), mock.patch.object(runner, "run_scrape_phase", boom):
            asyncio.run(runner.drive_run("run1"))
        with _patched(fake):
            status = store.read_status(PREFIX)
        self.assertEqual(status["phase"], "failed")
        self.assertEqual(status["drive_attempts"], 1)


class _FakeExchange:
    pass


class _FakeQueue:
    def __init__(self):
        self._handler = None
        self.bound = None

    async def bind(self, exchange, routing_key):
        self.bound = (exchange, routing_key)

    async def consume(self, handler):
        self._handler = handler


class _FakeChannel:
    def __init__(self):
        self.queue = _FakeQueue()
        self.exchange = _FakeExchange()
        self.declared_exchange = None
        self.declared_queue = None

    async def declare_exchange(self, name, kind, durable=True):
        self.declared_exchange = (name, kind, durable)
        return self.exchange

    async def declare_queue(self, name, durable=True):
        self.declared_queue = (name, durable)
        return self.queue


class ConsumeRelationshipRunsTests(unittest.TestCase):
    """The queue consumer acks on receipt (before driving the run) — see
    redrive_stale_runs for why. Prove the ack-then-drive ordering with a fake channel.
    It must also bind to the shared exchange (M3) rather than only declare the queue —
    otherwise Task 9's publisher has nothing to deliver to."""

    def test_message_is_acked_before_the_run_is_driven(self) -> None:
        import json as _json

        events = []

        class FakeMessage:
            def __init__(self, body: bytes):
                self.body = body

            async def ack(self):
                events.append("ack")

        async def fake_drive(run_id):
            events.append(("drive", run_id))

        channel = _FakeChannel()
        with mock.patch.object(runner, "drive_run", fake_drive):
            asyncio.run(runner.consume_relationship_runs(channel))
            message = FakeMessage(_json.dumps({"run_id": "run1"}).encode("utf-8"))
            asyncio.run(channel.queue._handler(message))

        self.assertEqual(events, ["ack", ("drive", "run1")])
        self.assertEqual(channel.declared_queue, (runner.RELATIONSHIP_QUEUE, True))
        self.assertEqual(
            channel.queue.bound, (channel.exchange, runner.RELATIONSHIP_ROUTING_KEY))

    def test_an_undecodable_message_is_dropped_not_raised(self) -> None:
        class FakeMessage:
            def __init__(self, body: bytes):
                self.body = body

            async def ack(self):
                pass

        channel = _FakeChannel()
        asyncio.run(runner.consume_relationship_runs(channel))
        message = FakeMessage(b"not json")
        asyncio.run(channel.queue._handler(message))   # must not raise

    def test_a_non_dict_json_message_is_dropped_not_raised(self) -> None:
        """M1: valid JSON that isn't an object (e.g. a bare list) must not crash the
        consumer with AttributeError from calling .get() on a list."""
        class FakeMessage:
            def __init__(self, body: bytes):
                self.body = body

            async def ack(self):
                pass

        channel = _FakeChannel()
        asyncio.run(runner.consume_relationship_runs(channel))
        message = FakeMessage(b"[1, 2, 3]")
        asyncio.run(channel.queue._handler(message))   # must not raise


class GeminiBatchHeartbeatTests(unittest.TestCase):
    """I2(a): _run_gemini_batch must flush counters on every poll iteration, so a run
    legitimately waiting on a single (possibly hours-long) Gemini batch never goes stale
    under RELATIONSHIP_STALE_SEC and gets a second, re-spending drive_run started on it."""

    def test_a_polling_batch_flushes_counters_before_it_resolves(self) -> None:
        fake = FakeS3()
        _seed(fake)
        polls = [
            {"name": "batches/1", "done": False, "state": {"name": "JOB_STATE_RUNNING"}},
            {"name": "batches/1", "done": True, "state": {"name": "JOB_STATE_SUCCEEDED"}},
        ]
        call_n = {"n": 0}

        def fake_get_batch(name):
            i = min(call_n["n"], len(polls) - 1)
            call_n["n"] += 1
            return polls[i]

        with _patched(fake), \
                mock.patch("app.services.ai_mode.gemini_batch.create_batch",
                          return_value={"name": "batches/1"}), \
                mock.patch("app.services.ai_mode.gemini_batch.get_batch",
                          side_effect=fake_get_batch), \
                mock.patch("app.services.ai_mode.gemini_batch.collect_results",
                          return_value=[]), \
                mock.patch("time.sleep"):
            counters = store.Counters(PREFIX, rows_total=1)
            before = fake.put_calls
            runner._run_gemini_batch(PREFIX, [("0", {})], counters)

        # One non-terminal poll happened before the terminal one -> the heartbeat must
        # have force-flushed status.json at least once in between.
        self.assertGreater(fake.put_calls, before)
        self.assertGreaterEqual(call_n["n"], 2)

    def test_no_counters_given_is_still_safe(self) -> None:
        """The default (counters=None) — existing callers/tests that don't pass one —
        must not break."""
        fake = FakeS3()
        _seed(fake)
        with _patched(fake), \
                mock.patch("app.services.ai_mode.gemini_batch.create_batch",
                          return_value={"name": "batches/1"}), \
                mock.patch("app.services.ai_mode.gemini_batch.get_batch",
                          return_value={"name": "batches/1", "done": True,
                                       "state": {"name": "JOB_STATE_SUCCEEDED"}}), \
                mock.patch("app.services.ai_mode.gemini_batch.collect_results",
                          return_value=[]):
            result = runner._run_gemini_batch(PREFIX, [("0", {})])   # no counters arg
        self.assertEqual(result, {})


class SingleFlightTests(unittest.TestCase):
    """I2(b): redrive_stale_runs and the queue consumer are two independent paths that
    can both decide to drive the same run_id. Without a guard, the second driver's
    pending = scraped - cleaned still contains every row the first driver already
    submitted (cleaned/ objects don't appear until the batch completes), so it
    re-submits a duplicate Gemini batch. drive_run must refuse to double-drive."""

    def test_a_second_drive_run_for_an_in_flight_run_is_a_no_op(self) -> None:
        fake = FakeS3()
        _seed(fake)
        with _patched(fake):
            store.write_run_pointer("run1", PREFIX, "Acme")

        started = asyncio.Event()
        release = asyncio.Event()
        calls: list[str] = []

        async def slow_scrape(p, c):
            calls.append("scrape-start")
            started.set()
            await release.wait()
            calls.append("scrape-end")

        async def run_both():
            with mock.patch.object(runner, "run_scrape_phase", slow_scrape), \
                    mock.patch.object(runner, "run_verdict_phase",
                                      lambda p, c: asyncio.sleep(0)), \
                    mock.patch.object(runner, "write_outputs",
                                      lambda p, c: {"status": "completed"}), \
                    mock.patch.object(driver, "notify_terminal"):
                first = asyncio.create_task(runner.drive_run("run1"))
                await started.wait()
                # The first drive is still inside run_scrape_phase (blocked on
                # `release`). A second drive_run for the SAME run_id must return
                # immediately, without ever re-entering run_scrape_phase.
                second = asyncio.create_task(runner.drive_run("run1"))
                await second
                calls.append("second-done")
                release.set()
                await first

        with _patched(fake):
            asyncio.run(run_both())

        self.assertEqual(calls, ["scrape-start", "second-done", "scrape-end"])

    def test_the_guard_is_cleared_after_a_run_finishes_so_a_later_drive_works(self) -> None:
        fake = FakeS3()
        _seed(fake)
        with _patched(fake):
            store.write_run_pointer("run1", PREFIX, "Acme")

        order = []

        async def phase(name, p, c):
            order.append(name)

        with _patched(fake), \
                mock.patch.object(runner, "run_scrape_phase",
                                  lambda p, c: phase("scrape", p, c)), \
                mock.patch.object(runner, "run_verdict_phase",
                                  lambda p, c: phase("verdict", p, c)), \
                mock.patch.object(runner, "write_outputs",
                                  lambda p, c: order.append("outputs") or {}), \
                mock.patch.object(driver, "notify_terminal"):
            asyncio.run(runner.drive_run("run1"))
            asyncio.run(runner.drive_run("run1"))   # must run again, not be skipped

        self.assertEqual(order, ["scrape", "verdict", "outputs"] * 2)
        self.assertNotIn("run1", driver._driving)


class BatchReattachTests(unittest.TestCase):
    """A Gemini Batch job runs on Google's side and is billed whether or not we are still
    listening. Losing its NAME (timeout, worker restart, crash) therefore means paying for
    the shard a second time, because the next drive sees rows with no verdict and submits
    an identical batch. These cover the record that prevents that."""

    def _seed_raw(self, fake, n=2):
        _seed(fake)
        with _patched(fake):
            for idx in range(n):
                store.write_row(PREFIX, idx, {
                    **OK_ENVELOPE, "row_index": idx, "x_domain": "acme.com",
                    "fields": {"row_index": idx, "x_name": "Acme", "y_name": f"Y{idx}",
                               "input_url": "https://acme.com/p"},
                })

    @staticmethod
    def _gb(*, terminal, name="batches/live"):
        gb = mock.Mock()
        gb.create_batch.return_value = {"name": name}
        gb.batch_name_from_create.return_value = name
        gb.get_batch.return_value = {"name": name, "done": terminal}
        gb.state_name.return_value = "JOB_STATE_SUCCEEDED"
        gb.is_terminal.return_value = terminal
        gb.collect_results.return_value = [
            {"key": "0", "text": "{}"}, {"key": "1", "text": "{}"}]
        gb.parse_json_from_text.return_value = {"relationship_status": "unclear"}
        return gb

    def test_the_job_name_is_persisted_before_the_first_poll(self) -> None:
        fake = FakeS3()
        self._seed_raw(fake)
        gb = self._gb(terminal=False)
        with _patched(fake), mock.patch.object(ai_mode_pkg, "gemini_batch", gb), \
                mock.patch.dict(os.environ,
                                {"GEMINI_BATCH_TIMEOUT_SEC": "60",
                                 "GEMINI_BATCH_POLL_SEC": "1"}, clear=False), \
                mock.patch("time.sleep", lambda _s: None), \
                mock.patch("time.monotonic", side_effect=[0.0, 70.0, 70.0]):
            with self.assertRaises(TimeoutError):
                runner._run_gemini_batch(PREFIX, [("0", {}), ("1", {})])
        # Abandoning the wait must NOT abandon the job: the record survives the timeout.
        with _patched(fake):
            records = store.list_batch_records(PREFIX)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["name"], "batches/live")
        self.assertEqual(records[0]["indices"], [0, 1])

    def test_a_redrive_reattaches_instead_of_submitting_a_second_batch(self) -> None:
        fake = FakeS3()
        self._seed_raw(fake)
        # A previous drive got as far as creating the job and writing the record.
        with _patched(fake):
            store.put_object(store.batch_record_key(PREFIX, [0, 1]),
                             {"name": "batches/paid-for", "model": "m",
                              "indices": [0, 1]})
        gb = self._gb(terminal=True)
        with _patched(fake), mock.patch.object(ai_mode_pkg, "gemini_batch", gb):
            counters = store.Counters(PREFIX, rows_total=2)
            asyncio.run(runner.run_verdict_phase(PREFIX, counters))

        gb.get_batch.assert_called_with("batches/paid-for")
        gb.create_batch.assert_not_called()       # the money assertion
        with _patched(fake):
            self.assertIsNotNone(store.get_object(store.cleaned_key(PREFIX, 0)))
            self.assertIsNotNone(store.get_object(store.cleaned_key(PREFIX, 1)))
            # Collected and durable -> the record is cleared, so the NEXT drive is a no-op.
            self.assertEqual(store.list_batch_records(PREFIX), [])

    def test_a_record_whose_rows_are_already_verdicted_is_just_cleared(self) -> None:
        """The normal path: a drive completed, so the record is stale. Clearing it must
        not cost a poll — the job is long finished and the rows already have verdicts."""
        fake = FakeS3()
        self._seed_raw(fake)
        with _patched(fake):
            store.put_object(store.batch_record_key(PREFIX, [0, 1]),
                             {"name": "batches/old", "model": "m", "indices": [0, 1]})
            for idx in (0, 1):
                store.put_object(store.cleaned_key(PREFIX, idx),
                                 {"row_index": idx, "parsed": {}})
        gb = self._gb(terminal=True)
        with _patched(fake), mock.patch.object(ai_mode_pkg, "gemini_batch", gb):
            counters = store.Counters(PREFIX, rows_total=2)
            asyncio.run(runner._reattach_batches(PREFIX, counters))
        gb.get_batch.assert_not_called()
        with _patched(fake):
            self.assertEqual(store.list_batch_records(PREFIX), [])

    def test_a_failed_record_write_does_not_abandon_a_paid_for_batch(self) -> None:
        """put_object raises by design here, but raising would discard a batch Google is
        already billing — the very double-spend the record exists to prevent."""
        fake = FakeS3()
        self._seed_raw(fake)
        gb = self._gb(terminal=True)
        fake.fail_keys.add(store.batch_record_key(PREFIX, [0, 1]))
        with _patched(fake), mock.patch.object(ai_mode_pkg, "gemini_batch", gb):
            out = runner._run_gemini_batch(PREFIX, [("0", {}), ("1", {})])
        self.assertEqual(set(out), {"0", "1"})

    def test_the_shared_gsearch_timeout_key_is_not_what_bounds_a_shard(self) -> None:
        """.env sets GEMINI_BATCH_TIMEOUT_SEC=1800 for gsearch's per-row batches. Reading
        it here abandoned every relationship shard after 30 minutes."""
        with mock.patch.dict(os.environ, {"GEMINI_BATCH_TIMEOUT_SEC": "1800"},
                             clear=False):
            os.environ.pop("GEMINI_BATCH_TIMEOUT_SEC", None)
            self.assertEqual(runner._batch_timeout_sec(), 172800)


if __name__ == "__main__":
    unittest.main()
