"""Offline tests for the shared Gemini batch config resolver (common/llm_batch.py).

One value must move all four pipelines. Before this module existed, the shard size lived under
two key names with identical defaults and the model resolved differently per pipeline.

Since 2026-08-20 that applies to the TOGGLE too: ``LLM_BATCH`` is the only one, and the three
per-pipeline overrides are deleted rather than deprecated — see
``test_deleted_per_pipeline_toggles_have_no_effect``.
"""
import os
import unittest
from unittest import mock

from app.services.common import llm_batch as lb
from app.services.serpwow import engine, relationship_runner

# Every key the resolver reads, blanked, so a stray value in the developer's environment
# cannot make a test pass or fail by accident.
_CLEAN = {k: "" for k in (
    "LLM_BATCH",
    "GEMINI_BATCH_MODEL", "GEMINI_MODEL", "GEMINI_BATCH_SHARD_SIZE",
    "GEMINI_BATCH_MAX_INFLIGHT", "GEMINI_BATCH_TIMEOUT_SEC", "GEMINI_BATCH_POLL_SEC")}


def _env(**overrides):
    return mock.patch.dict(os.environ, {**_CLEAN, **overrides})


class ToggleTests(unittest.TestCase):
    def test_off_by_default(self) -> None:
        with _env():
            for pipe in ("ai_bulk", "ai_deep", "gsearch", "firmographics"):
                self.assertFalse(lb.batch_enabled(pipe), pipe)

    def test_one_global_switch_moves_every_toggleable_pipeline(self) -> None:
        with _env(LLM_BATCH="true"):
            for pipe in ("ai_bulk", "ai_deep", "gsearch", "firmographics"):
                self.assertTrue(lb.batch_enabled(pipe), pipe)

    def test_blank_reads_as_unset_not_as_off(self) -> None:
        """.env.example ships the key as `NAME=`, and get_bool_env only falls back on an
        ABSENT variable — so blank has to mean "unset", which is what _flag's tri-state is
        for. With one toggle left, blank and false happen to agree; the distinction stays
        because a caller that treats "not configured" differently must still be able to."""
        with _env(LLM_BATCH="   "):
            for pipe in ("ai_bulk", "ai_deep", "gsearch", "firmographics"):
                self.assertFalse(lb.batch_enabled(pipe), pipe)
        self.assertIsNone(lb._flag("LLM_BATCH_DEFINITELY_UNSET_KEY"))

    def test_relationship_is_always_batch(self) -> None:
        """Its Gemini call IS the verdict; there is no inline path to switch to, so a toggle
        would be a setting that cannot be honoured."""
        with _env(LLM_BATCH="false"):
            self.assertTrue(lb.batch_enabled("relationship"))

    def test_gmaps_has_no_llm_at_all(self) -> None:
        with _env(LLM_BATCH="true"):
            self.assertFalse(lb.batch_enabled("gmaps"))

    def test_shared_row_batch_is_a_different_question(self) -> None:
        """relationship batches, but through its own driver — engine's gate must say no, or it
        seeds a second duplicate job for the same run."""
        with _env(LLM_BATCH="true"):
            self.assertTrue(lb.batch_enabled("relationship"))
            self.assertFalse(lb.uses_shared_row_batch("relationship"))
            self.assertFalse(engine._batch_postprocess_enabled_for("relationship"))
            self.assertTrue(engine._batch_postprocess_enabled_for("gsearch"))
            self.assertTrue(engine._batch_postprocess_enabled_for("firmographics"))


class ModelTests(unittest.TestCase):
    def test_batch_model_wins_then_plain_model_then_default(self) -> None:
        with _env(GEMINI_BATCH_MODEL="m-batch", GEMINI_MODEL="m-plain"):
            self.assertEqual(lb.batch_model(), "m-batch")
        with _env(GEMINI_MODEL="m-plain"):
            self.assertEqual(lb.batch_model(), "m-plain")
        with _env():
            self.assertEqual(lb.batch_model(), "gemini-2.5-flash-lite")

    def test_no_pipeline_reads_a_model_or_shard_key_directly(self) -> None:
        """The live bug was relationship_runner reading GEMINI_BATCH_MODEL itself, with no
        GEMINI_MODEL fallback — so setting only GEMINI_MODEL moved three pipelines and left it
        on the hardcoded default. A second direct read anywhere reintroduces exactly that, so
        assert the invariant rather than one instance of it.
        """
        import pathlib as _p
        root = _p.Path(engine.__file__).parent.parent          # app/services
        offenders = []
        for path in list(root.glob("serpwow/**/*.py")) + list(root.glob("ai_mode/*.py")):
            if path.name == "llm_batch.py":
                continue
            text = path.read_text()
            for key in ("GEMINI_BATCH_MODEL", "GEMINI_BATCH_SHARD_SIZE",
                        "GEMINI_BATCH_MAX_INFLIGHT", "GEMINI_BATCH_TIMEOUT_SEC"):
                # A mention inside a comment or docstring is fine; an env read is not.
                for marker in (f'getenv("{key}"', f'_env("{key}"', f"getenv('{key}'"):
                    if marker in text:
                        offenders.append(f"{path.name}: {key}")
        self.assertEqual(offenders, [], f"env read outside the resolver: {offenders}")


class MechanicalKnobTests(unittest.TestCase):
    def test_defaults(self) -> None:
        with _env():
            self.assertEqual(lb.shard_size(), 5000)
            self.assertEqual(lb.max_inflight(), 5)
            # 48h == Gemini's job expiry. Was 1800 for gsearch/firmographics, which could not
            # outlast a real Batch job (HANDOFF blocker #1).
            self.assertEqual(lb.timeout_sec(), 172800)

    def test_one_shard_key_reaches_every_pipeline(self) -> None:
        with _env(GEMINI_BATCH_SHARD_SIZE="250", GEMINI_BATCH_MAX_INFLIGHT="9"):
            self.assertEqual(lb.shard_size(), 250)
            self.assertEqual(lb.max_inflight(), 9)
            # relationship read the same keys before; gsearch/firmographics read
            # GSEARCH_GEMINI_* and so ignored these. Both now resolve here.
            self.assertEqual(relationship_runner._shard_size(), 250)
            self.assertEqual(relationship_runner._max_inflight(), 9)

    def test_one_timeout_key_reaches_relationship(self) -> None:
        with _env(GEMINI_BATCH_TIMEOUT_SEC="600"):
            self.assertEqual(lb.timeout_sec(), 600)
            self.assertEqual(relationship_runner._batch_timeout_sec(), 600)

    def test_floors_are_enforced(self) -> None:
        with _env(GEMINI_BATCH_SHARD_SIZE="0", GEMINI_BATCH_MAX_INFLIGHT="0",
                  GEMINI_BATCH_TIMEOUT_SEC="1", GEMINI_BATCH_POLL_SEC="0"):
            self.assertEqual(lb.shard_size(), 1)
            self.assertEqual(lb.max_inflight(), 1)
            self.assertEqual(lb.timeout_sec(), 60)
            self.assertEqual(lb.poll_sec(), 5)

    def test_deleted_keys_have_no_effect(self) -> None:
        """GSEARCH_GEMINI_* and RELATIONSHIP_BATCH_TIMEOUT_SEC are gone; setting them must not
        quietly work, or two names for one number come back."""
        with _env(GSEARCH_GEMINI_CHUNK_SIZE="7", GSEARCH_GEMINI_MAX_INFLIGHT="7",
                  RELATIONSHIP_BATCH_TIMEOUT_SEC="7"):
            self.assertEqual(lb.shard_size(), 5000)
            self.assertEqual(lb.max_inflight(), 5)
            self.assertEqual(lb.timeout_sec(), 172800)

    def test_deleted_per_pipeline_toggles_have_no_effect(self) -> None:
        """The three overrides were deleted 2026-08-20. A leftover line in someone's .env
        must be INERT, not a ghost key that still quietly wins over the global — that is the
        exact failure the collapse to one toggle was meant to end."""
        with _env(LLM_BATCH="false", AI_MODE_LLM_BATCH="true",
                  GSEARCH_LLM_BATCH="true", FIRMOGRAPHICS_LLM_BATCH="true"):
            for pipe in ("ai_bulk", "ai_deep", "gsearch", "firmographics"):
                self.assertFalse(lb.batch_enabled(pipe), pipe)
        with _env(LLM_BATCH="true", AI_MODE_LLM_BATCH="false",
                  GSEARCH_LLM_BATCH="false", FIRMOGRAPHICS_LLM_BATCH="false"):
            for pipe in ("ai_bulk", "ai_deep", "gsearch", "firmographics"):
                self.assertTrue(lb.batch_enabled(pipe), pipe)


if __name__ == "__main__":
    unittest.main()
