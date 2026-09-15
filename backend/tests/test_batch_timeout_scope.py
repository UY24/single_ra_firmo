"""GEMINI_BATCH_TIMEOUT_SEC bounds ONE Gemini shard's poll, not the run.

The question this answers: "if scraping takes 5 days, does the 48h batch timeout blow up?"
No — and it must stay no. The deadline is computed INSIDE ``_poll_to_terminal``, which only
runs in phase 2, after the scrape phase has already returned. Phases are sequential
statements in ``_phases``. If anyone ever hoists that deadline to run start (or to the top
of the LLM phase), a long scrape would silently eat the batch's budget and every shard
would time out at once — losing verdicts for work already paid for on Google's side.

Both S3-only batch pipelines have the identical loop, so both are pinned here.
"""
import time
import unittest
from unittest import mock

from app.services.serpwow import firmographics_runner, relationship_runner

# Longer than any real scrape phase: if the deadline were anchored anywhere earlier than
# the poll itself, five days of monotonic time would already be past it.
FIVE_DAYS = 5 * 24 * 3600


class FakeClock:
    """A monotonic clock that only moves when something sleeps."""

    def __init__(self, start: float) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeBatch:
    """Stands in for ai_mode.gemini_batch: terminal after `polls_needed` polls."""

    def __init__(self, polls_needed: int) -> None:
        self.polls_needed = polls_needed
        self.polls = 0

    def get_batch(self, name):
        self.polls += 1
        return {"done": self.polls >= self.polls_needed, "name": name}

    def state_name(self, obj):
        return "JOB_STATE_SUCCEEDED" if obj.get("done") else "JOB_STATE_RUNNING"

    def is_terminal(self, state, done):
        return bool(done)


class BatchDeadlineStartsAtThePollTests(unittest.TestCase):
    ENV = {"GEMINI_BATCH_TIMEOUT_SEC": "3600", "GEMINI_BATCH_POLL_SEC": "15"}

    def _poll(self, module, clock, gb):
        with mock.patch.dict("os.environ", self.ENV), \
                mock.patch.object(time, "monotonic", clock.monotonic), \
                mock.patch.object(time, "sleep", clock.sleep):
            return module._poll_to_terminal(gb, "batches/abc", None)

    def test_five_days_of_scraping_does_not_consume_the_batch_budget(self) -> None:
        for module in (firmographics_runner, relationship_runner):
            with self.subTest(module=module.__name__):
                # The clock is already 5 days in: that is the scrape phase having run.
                clock = FakeClock(start=FIVE_DAYS)
                gb = FakeBatch(polls_needed=4)
                obj = self._poll(module, clock, gb)
                self.assertTrue(obj["done"], "shard did not complete")
                self.assertEqual(gb.polls, 4)
                # 3 sleeps x 15s: the shard spent its own seconds, not the run's.
                self.assertEqual(clock.now, FIVE_DAYS + 45)

    def test_the_deadline_is_real_when_the_SHARD_itself_overruns(self) -> None:
        """The companion, or the test above passes vacuously: a shard that genuinely
        outlives the timeout must still raise, so the batch record survives and the next
        drive re-attaches instead of paying Google for a second identical job."""
        for module in (firmographics_runner, relationship_runner):
            with self.subTest(module=module.__name__):
                clock = FakeClock(start=FIVE_DAYS)
                gb = FakeBatch(polls_needed=10**9)     # never finishes
                with self.assertRaises(TimeoutError):
                    self._poll(module, clock, gb)
                # It waited its full hour before giving up, from when polling began.
                self.assertGreaterEqual(clock.now - FIVE_DAYS, 3600)

    def test_each_shard_gets_its_own_clock(self) -> None:
        """20 sequential waves at 5-inflight is the documented 500k shape, so the LLM
        phase as a whole may far exceed the timeout — only one shard's wait is bounded."""
        clock = FakeClock(start=FIVE_DAYS)
        for _ in range(3):
            gb = FakeBatch(polls_needed=200)           # ~50 min each
            self._poll(firmographics_runner, clock, gb)
        # Nearly 2.5h of LLM phase against a 1h per-shard timeout, and nothing raised.
        self.assertGreater(clock.now - FIVE_DAYS, 3600 * 2)


if __name__ == "__main__":
    unittest.main()
