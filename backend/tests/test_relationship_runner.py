"""Phase 1: resume-by-object-presence, concurrency, and credit accounting."""
import asyncio
import os
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
        self.assertLessEqual(live["max"], 2)


if __name__ == "__main__":
    unittest.main()
