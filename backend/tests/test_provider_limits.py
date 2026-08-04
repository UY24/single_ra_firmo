"""Shared scrape.do concurrency gate + atomic JSON writes.

Both are throughput/durability fixes for the 2026-08 gmaps work: the gate lets
WORKER_CONCURRENCY rise without exceeding scrape.do's per-ACCOUNT cap, and the atomic
write stops a crash mid-write from destroying a whole run's state.
"""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.services.common import provider_limits


class ScrapedoGateTests(unittest.TestCase):
    def setUp(self) -> None:
        provider_limits.reset_scrapedo_limit()

    tearDown = setUp

    def _max_overlap(self, workers: int, limit: str) -> int:
        """Run `workers` tasks through the gate; return peak simultaneous holders."""
        peak = 0
        inflight = 0

        async def one():
            nonlocal peak, inflight
            async with provider_limits.scrapedo_slot():
                inflight += 1
                peak = max(peak, inflight)
                await asyncio.sleep(0)      # yield so others can pile up
                await asyncio.sleep(0)
                inflight -= 1

        async def go():
            await asyncio.gather(*(one() for _ in range(workers)))

        with mock.patch.dict(os.environ, {"SCRAPEDO_CONCURRENCY": limit}, clear=False):
            asyncio.run(go())
        return peak

    def test_gate_caps_concurrent_calls(self) -> None:
        self.assertEqual(self._max_overlap(workers=50, limit="5"), 5)

    def test_gate_allows_the_full_plan_when_configured(self) -> None:
        self.assertEqual(self._max_overlap(workers=100, limit="100"), 100)

    def test_limit_is_never_below_one(self) -> None:
        with mock.patch.dict(os.environ, {"SCRAPEDO_CONCURRENCY": "0"}, clear=False):
            self.assertEqual(provider_limits.scrapedo_limit(), 1)
        with mock.patch.dict(os.environ, {"SCRAPEDO_CONCURRENCY": "garbage"}, clear=False):
            self.assertGreaterEqual(provider_limits.scrapedo_limit(), 1)

    def test_gmaps_and_ai_mode_share_one_gate(self) -> None:
        """The cap is per scrape.do ACCOUNT, and both pipelines run in the same worker
        process — so they must contend for the same slots, not get one cap each."""
        from app.services.ai_mode import worker as ai_worker
        from app.services.serpwow import scrapedo_maps_client as maps

        self.assertIs(maps.scrapedo_slot, provider_limits.scrapedo_slot)
        self.assertIs(ai_worker.scrapedo_slot, provider_limits.scrapedo_slot)


class OneKnobTests(unittest.TestCase):
    """A deployment that runs one pipeline at a time should only have to set
    WORKER_CONCURRENCY; the others exist for when they need to differ."""

    def test_ai_mode_falls_back_to_the_shared_worker_knob(self) -> None:
        from app.services.ai_mode import broker

        with mock.patch.dict(os.environ, {"WORKER_CONCURRENCY": "100"}, clear=True):
            self.assertEqual(broker.worker_concurrency(), 100)

    def test_ai_mode_specific_knob_still_wins(self) -> None:
        from app.services.ai_mode import broker

        with mock.patch.dict(os.environ, {"WORKER_CONCURRENCY": "100",
                                          "AI_MODE_WORKER_CONCURRENCY": "5"}, clear=True):
            self.assertEqual(broker.worker_concurrency(), 5)

    def test_scrapedo_cap_is_independent_of_worker_slots(self) -> None:
        """The provider cap must NOT track the slot count — otherwise raising worker
        slots would silently raise the vendor limit, defeating the whole point."""
        with mock.patch.dict(os.environ, {"WORKER_CONCURRENCY": "500"}, clear=True):
            self.assertEqual(provider_limits.scrapedo_limit(), 100)


class StateMirrorThrottleTests(unittest.TestCase):
    """state.json is re-PUT to the SAME S3 key on every row update and grows with the
    run — a 100-row run uploaded ~84MB of near-identical copies, saturating uplink and
    hot-spotting one key (the documented SlowDown cause)."""

    def _write(self, upload_id, status, interval="5", tmp=None):
        """Run write_upload_artifact; return True if an S3 mirror was scheduled."""
        from app.services.serpwow import engine

        scheduled = []
        state = {"company_name": "Acme", "pipeline": "gmaps", "status": status}
        with mock.patch.object(engine, "_state_file",
                               return_value=Path(tmp) / f"{upload_id}.json"), \
             mock.patch.object(engine, "_find_upload_dir", return_value=Path(tmp)), \
             mock.patch.object(engine, "_write_json"), \
             mock.patch.object(engine, "_write_json_to_s3_sync"), \
             mock.patch.object(engine.asyncio, "create_task",
                               side_effect=lambda coro: (scheduled.append(1),
                                                         coro.close())[0]), \
             mock.patch.dict(os.environ, {"S3_BUCKET": "b",
                                          "SERPWOW_S3_STATE_FLUSH_SEC": interval}):
            asyncio.run(engine.write_upload_artifact(upload_id, "state", state))
        return bool(scheduled)

    def setUp(self) -> None:
        from app.services.serpwow import engine
        engine._last_state_s3_flush.clear()

    def test_rapid_mid_run_updates_are_coalesced(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            first = self._write("u-throttle", "processing", tmp=d)
            second = self._write("u-throttle", "processing", tmp=d)
            third = self._write("u-throttle", "processing", tmp=d)
        self.assertTrue(first, "the first update must mirror")
        self.assertFalse(second, "a second update within the interval must be skipped")
        self.assertFalse(third)

    def test_terminal_state_always_mirrors(self) -> None:
        """Non-negotiable: whatever we skip mid-run, the FINAL state must reach S3 or a
        cold-start resume would read a stale run."""
        with tempfile.TemporaryDirectory() as d:
            self._write("u-term", "processing", tmp=d)          # consumes the window
            self.assertTrue(self._write("u-term", "completed", tmp=d))
            self.assertTrue(self._write("u-term", "completed_with_errors", tmp=d))
            self.assertTrue(self._write("u-term", "failed", tmp=d))

    def test_terminal_clears_the_tracking_entry(self) -> None:
        from app.services.serpwow import engine

        with tempfile.TemporaryDirectory() as d:
            self._write("u-clean", "processing", tmp=d)
            self.assertIn("u-clean", engine._last_state_s3_flush)
            self._write("u-clean", "completed", tmp=d)
        self.assertNotIn("u-clean", engine._last_state_s3_flush,
                         "must not leak an entry per run forever")

    def test_zero_interval_disables_throttling(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(self._write("u-off", "processing", interval="0", tmp=d))
            self.assertTrue(self._write("u-off", "processing", interval="0", tmp=d))

    def test_local_disk_is_always_written_even_when_s3_is_skipped(self) -> None:
        """The throttle must only affect the S3 copy — local disk stays the source of
        truth and every row's result must survive a crash."""
        from app.services.serpwow import engine

        with tempfile.TemporaryDirectory() as d:
            writes = []
            state = {"company_name": "Acme", "pipeline": "gmaps", "status": "processing"}
            with mock.patch.object(engine, "_state_file", return_value=Path(d) / "s.json"), \
                 mock.patch.object(engine, "_find_upload_dir", return_value=Path(d)), \
                 mock.patch.object(engine, "_write_json",
                                   side_effect=lambda *a, **k: writes.append(1)), \
                 mock.patch.object(engine, "_write_json_to_s3_sync"), \
                 mock.patch.object(engine.asyncio, "create_task",
                                   side_effect=lambda c: c.close()), \
                 mock.patch.dict(os.environ, {"S3_BUCKET": "b",
                                              "SERPWOW_S3_STATE_FLUSH_SEC": "9999"}):
                for _ in range(3):
                    asyncio.run(engine.write_upload_artifact("u-local", "state", state))
            self.assertEqual(len(writes), 3, "every update must hit local disk")


class AtomicWriteTests(unittest.TestCase):
    def test_write_is_atomic_and_leaves_no_temp_file(self) -> None:
        from app.services.serpwow.engine import _write_json

        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "state.json"
            _write_json(target, {"rows": [1, 2, 3]})
            self.assertEqual(json.loads(target.read_text())["rows"], [1, 2, 3])
            self.assertEqual([p.name for p in Path(d).iterdir()], ["state.json"])

    def test_failed_write_leaves_the_previous_file_intact(self) -> None:
        """The whole point: a crash mid-write must not truncate an existing state.json,
        because that loses every row's progress, not one row's."""
        from app.services.serpwow.engine import _write_json

        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "state.json"
            _write_json(target, {"good": True})

            class Unserializable:
                pass

            with self.assertRaises(TypeError):
                _write_json(target, {"bad": Unserializable()})

            # Old content survived, and no debris was left behind.
            self.assertEqual(json.loads(target.read_text()), {"good": True})
            self.assertEqual([p.name for p in Path(d).iterdir()], ["state.json"])


if __name__ == "__main__":
    unittest.main()
