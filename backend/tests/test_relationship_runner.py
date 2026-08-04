"""Phase 1: resume-by-object-presence, concurrency, and credit accounting."""
import asyncio
import os
import time
import unittest
from unittest import mock

from app.services.serpwow import relationship_runner as runner
from app.services.serpwow import relationship_store as store
from tests.test_relationship_store import FakeS3, _patched

CSV = (b"Input_URL,Company_Name_X,Company_Name_Y,country\n"
       b"https://acme.com/p,Acme,Sanzo,US\n"
       b"https://acme.com/p,Acme,Yuzu,US\n"
       b"https://acme.com/p,Acme,Pomelo,US\n")

OK_ENVELOPE = {
    "query": "q", "request_count": 1, "successful_requests": 1, "failed_requests": 0,
    "credits": 10, "text_blocks": [{"snippet": "Acme invested in it."}],
    "references": [{"title": "Y", "link": "https://y.com"}],
    "billed_empty": False, "error": None, "error_category": None,
}
ERR_ENVELOPE = {
    "query": "q", "request_count": 4, "successful_requests": 0, "failed_requests": 4,
    "credits": 0, "text_blocks": [], "references": [], "billed_empty": False,
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
            mock.patch.dict(os.environ, {"RELATIONSHIP_CONCURRENCY": "2"}, clear=False):
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
            store.put_object(store.raw_key(PREFIX, 0), OK_ENVELOPE)
            store.put_object(store.error_key(PREFIX, 1), ERR_ENVELOPE)

        calls, _ = _drive(fake, [OK_ENVELOPE])
        self.assertEqual(len(calls), 1)          # only row 2 was scraped
        self.assertIn("Pomelo", calls[0])

    def test_a_fully_scraped_run_issues_zero_calls(self) -> None:
        fake = FakeS3()
        _seed(fake)
        with _patched(fake):
            for idx in (0, 1, 2):
                store.put_object(store.raw_key(PREFIX, idx), OK_ENVELOPE)
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
                mock.patch.dict(os.environ, {"RELATIONSHIP_CONCURRENCY": "2"},
                                clear=False):
            counters = store.Counters(PREFIX, rows_total=3)
            asyncio.run(runner.run_scrape_phase(PREFIX, counters))

        # Never more rows pulled from the CSV than the concurrency window (2) allows.
        # (rows_total is also 3 here, so asserting against 3 would pass either way —
        # the real claim is bounded by RELATIONSHIP_CONCURRENCY, not by row count.)
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
                mock.patch.dict(os.environ, {"RELATIONSHIP_CONCURRENCY": "2"},
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
                mock.patch.dict(os.environ, {"RELATIONSHIP_CONCURRENCY": "2"},
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
                mock.patch.object(runner, "_STOP_CHECK_INTERVAL_SEC", 0.02), \
                mock.patch.dict(os.environ, {"RELATIONSHIP_CONCURRENCY": "20"},
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


class VerdictPhaseTests(unittest.TestCase):
    def _seed_raw(self, fake, n=3):
        _seed(fake)
        with _patched(fake):
            for idx in range(n):
                store.put_object(store.raw_key(PREFIX, idx), {
                    **OK_ENVELOPE, "row_index": idx, "x_domain": "acme.com",
                    "fields": {"row_index": idx, "x_name": "Acme", "y_name": f"Y{idx}",
                               "input_url": "https://acme.com/p", "city": "",
                               "country": "US"},
                })

    def test_a_verdict_object_is_written_per_row_with_its_candidate_set(self) -> None:
        fake = FakeS3()
        self._seed_raw(fake)

        def fake_batch(prefix_arg, items):
            return {key: {"relationship_status": "confirmed",
                          "official_website": "https://y.com",
                          "relationship_confidence_score": 90,
                          "website_confidence_score": 90} for key, _body in items}

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

        def fake_batch(prefix_arg, items):
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

        def fake_batch(prefix_arg, items):
            submitted["keys"] = [k for k, _b in items]
            return {}

        with _patched(fake), mock.patch.object(runner, "_run_gemini_batch", fake_batch):
            asyncio.run(runner.run_verdict_phase(
                PREFIX, store.Counters(PREFIX, rows_total=1)))

        self.assertEqual(submitted["keys"], [])


if __name__ == "__main__":
    unittest.main()
