"""A Gemini shard Google never answered must leave its rows RETRYABLE.

``is_terminal()`` is true for SUCCEEDED, FAILED, CANCELLED and EXPIRED alike — it answers
"stop polling", not "there are results". Before this, both batch runners treated a dead job
as an answered one and wrote an empty ``cleaned/`` object for every key in the shard. That
object is the row's done-marker, so those rows were blank FOREVER: no error surfaced, no
retry possible, and the scrape.do credits already spent on them wasted.

The retry path this protects needs no new artifact: ``raw/`` + ``rows/`` already hold the
scrape, ``pending_llm/`` already names the rows awaiting an LLM, and the prompt is rebuilt
from ``rows/``. Leaving those markers in place IS the retry.
"""
import unittest

from app.services.ai_mode import gemini_batch
from app.services.common import llm_batch
from app.services.serpwow import firmographics_runner, relationship_runner

RUNNERS = (firmographics_runner, relationship_runner)


class FakeBatchModule:
    """Stands in for ai_mode.gemini_batch, but DELEGATES the state logic to the real thing.

    Deliberately not a reimplementation: a hand-copied is_success is how this bug survives
    a rewrite — the copy keeps saying "expired is fine" long after the original stops.
    """

    state_name = staticmethod(lambda obj: gemini_batch.state_name(obj))
    is_success = staticmethod(gemini_batch.is_success)
    is_terminal = staticmethod(gemini_batch.is_terminal)


class ShardFailureTests(unittest.TestCase):
    def test_only_a_succeeded_job_counts_as_answered(self) -> None:
        gb = FakeBatchModule()
        self.assertIsNone(
            llm_batch.shard_failure(gb, {"state": "JOB_STATE_SUCCEEDED", "done": True}))
        for state in ("JOB_STATE_EXPIRED", "JOB_STATE_FAILED", "JOB_STATE_CANCELLED"):
            with self.subTest(state=state):
                self.assertEqual(llm_batch.shard_failure(gb, {"state": state}), state,
                                 f"{state} was treated as an answered shard")

    def test_a_done_job_carrying_an_error_is_a_failure(self) -> None:
        """`done: true` with an error body is Google saying it gave up, not that it
        answered — the flag alone must not be enough to persist empty results."""
        gb = FakeBatchModule()
        obj = {"state": "", "done": True, "error": {"message": "quota"}}
        # The label is whatever state_name could make of it ("DONE" here); what matters is
        # that it is NOT None, i.e. nothing gets persisted for this shard.
        self.assertIsNotNone(llm_batch.shard_failure(gb, obj))

    def test_every_state_the_real_module_calls_dead_is_reported_dead(self) -> None:
        """Bound to gemini_batch.FAILED_STATES rather than a literal list, so a state added
        there cannot quietly start persisting empty results again."""
        gb = FakeBatchModule()
        for state in sorted(gemini_batch.FAILED_STATES):
            with self.subTest(state=state):
                self.assertEqual(
                    llm_batch.shard_failure(gb, {"state": state, "done": True}), state)


class AbortIfShardDiedTests(unittest.TestCase):
    """Both runners keep their own copy of the loop, so both are pinned."""

    def _record_deletes(self, module):
        deleted = []
        module_store = module.store
        original = module_store.delete_object
        module_store.delete_object = lambda key: deleted.append(key)
        self.addCleanup(setattr, module_store, "delete_object", original)
        return deleted

    def test_succeeded_shard_is_left_alone(self) -> None:
        for module in RUNNERS:
            with self.subTest(module=module.__name__):
                deleted = self._record_deletes(module)
                module._abort_if_shard_died(
                    FakeBatchModule(), {"state": "JOB_STATE_SUCCEEDED", "done": True},
                    "batches/1-5.json", "batches/abc")
                self.assertEqual(deleted, [], "a good shard had its job record dropped")

    def test_dead_shard_raises_so_no_cleaned_marker_is_written(self) -> None:
        """The raise is the whole mechanism: it aborts before _write_fields, so the rows
        keep their pending marker and the next drive resubmits ONLY them."""
        for module in RUNNERS:
            with self.subTest(module=module.__name__):
                self._record_deletes(module)
                with self.assertRaises(RuntimeError) as caught:
                    module._abort_if_shard_died(
                        FakeBatchModule(), {"state": "JOB_STATE_EXPIRED", "done": True},
                        "batches/1-5.json", "batches/abc")
                self.assertIn("JOB_STATE_EXPIRED", str(caught.exception))

    def test_dead_shard_forgets_the_job_so_the_next_drive_RESUBMITS(self) -> None:
        """Without this the next drive re-attaches to a job that can never answer, and the
        rows are stuck in a loop that re-polls a corpse instead of buying a new shard."""
        for module in RUNNERS:
            with self.subTest(module=module.__name__):
                deleted = self._record_deletes(module)
                with self.assertRaises(RuntimeError):
                    module._abort_if_shard_died(
                        FakeBatchModule(), {"state": "JOB_STATE_FAILED", "done": True},
                        "batches/1-5.json", "batches/abc")
                self.assertEqual(deleted, ["batches/1-5.json"])

    def test_a_delete_failure_still_raises(self) -> None:
        """Dropping the record is best-effort; NOT writing empty results is not."""
        for module in RUNNERS:
            with self.subTest(module=module.__name__):
                module_store = module.store
                original = module_store.delete_object

                def boom(key):
                    raise RuntimeError("S3 down")

                module_store.delete_object = boom
                self.addCleanup(setattr, module_store, "delete_object", original)
                with self.assertRaises(RuntimeError):
                    module._abort_if_shard_died(
                        FakeBatchModule(), {"state": "JOB_STATE_EXPIRED", "done": True},
                        "batches/1-5.json", "batches/abc")


if __name__ == "__main__":
    unittest.main()
