"""AI Mode found/not_found/error taxonomy parity with SerpWow (Task 10).

Verifies the 3-way outcome classification, error source/category breakdowns, and
that the Supabase mapping counts genuine errors (not not_found) as failures.
"""
import unittest

from app.models.results import EntityResult
from app.services.ai_mode.ai_mode_service import (
    _build_run_update,
    classify_ai_mode_outcomes,
    classify_one_result,
)
from app.services.serpwow.outcomes import (
    CAT_INTERNAL,
    CAT_RATE_LIMIT,
    SRC_GEMINI,
    SRC_SCRAPEDO,
)


class TestClassifyOutcomes(unittest.TestCase):
    def _results(self):
        return [
            # 1 found
            EntityResult(company_name="Alpha", country="US", sno=1,
                         website_url="https://alpha.com", confidence=90),
            # 1 genuine not_found (looked, found nothing; no error)
            EntityResult(company_name="Beta", country="US", sno=2),
            # 1 scrape.do error (tagged upstream in Phase 3)
            EntityResult(company_name="Gamma", country="US", sno=3,
                         error="scrape.do error: Scrape.do request failed: HTTP 429 Too Many Requests",
                         error_source=SRC_SCRAPEDO, error_category=CAT_RATE_LIMIT),
        ]

    def test_outcome_breakdown(self):
        outcome, _ = classify_ai_mode_outcomes(self._results())
        self.assertEqual(outcome, {"found": 1, "not_found": 1, "errored": 1})

    def test_error_breakdown_by_source_has_scrapedo(self):
        _, errors = classify_ai_mode_outcomes(self._results())
        self.assertEqual(errors["by_source"], {SRC_SCRAPEDO: 1})
        self.assertEqual(errors["by_category"], {CAT_RATE_LIMIT: 1})

    def test_invariant_found_notfound_errored_equals_total(self):
        results = self._results()
        outcome, _ = classify_ai_mode_outcomes(results)
        self.assertEqual(
            outcome["found"] + outcome["not_found"] + outcome["errored"], len(results)
        )

    def test_dropped_entity_is_gemini_error_not_notfound(self):
        # "missing from LLM response": submitted + scraped OK but LLM omitted it.
        # Genuine gemini failure -> errored (tagged in-place), NEVER not_found.
        results = [EntityResult(company_name="Delta", country="US", sno=1,
                                error="missing from LLM response")]
        outcome, errors = classify_ai_mode_outcomes(results)
        self.assertEqual(outcome, {"found": 0, "not_found": 0, "errored": 1})
        self.assertEqual(errors["by_source"], {SRC_GEMINI: 1})
        self.assertEqual(errors["by_category"], {CAT_INTERNAL: 1})
        self.assertEqual(results[0].error_source, SRC_GEMINI)


class TestClassifyOneResult(unittest.TestCase):
    """Per-result classifier used by the streaming report (PR1)."""

    def test_found(self):
        r = EntityResult(company_name="A", country="US", sno=1,
                         website_url="https://a.com")
        self.assertEqual(classify_one_result(r), "found")

    def test_not_found(self):
        r = EntityResult(company_name="B", country="US", sno=2)
        self.assertEqual(classify_one_result(r), "not_found")

    def test_tagged_error_kept(self):
        r = EntityResult(company_name="C", country="US", sno=3,
                         error="scrape.do error: HTTP 429",
                         error_source=SRC_SCRAPEDO, error_category=CAT_RATE_LIMIT)
        self.assertEqual(classify_one_result(r), "errored")
        self.assertEqual(r.error_source, SRC_SCRAPEDO)
        self.assertEqual(r.error_category, CAT_RATE_LIMIT)

    def test_untagged_error_attributed_to_gemini_in_place(self):
        r = EntityResult(company_name="D", country="US", sno=4,
                         error="missing from LLM response")
        self.assertEqual(classify_one_result(r), "errored")
        self.assertEqual(r.error_source, SRC_GEMINI)
        self.assertEqual(r.error_category, CAT_INTERNAL)

    def test_parity_with_aggregate_classifier(self):
        results = [
            EntityResult(company_name="A", country="US", sno=1,
                         website_url="https://a.com", confidence=90),
            EntityResult(company_name="B", country="US", sno=2),
            EntityResult(company_name="C", country="US", sno=3,
                         error="scrape.do error: HTTP 429",
                         error_source=SRC_SCRAPEDO, error_category=CAT_RATE_LIMIT),
            EntityResult(company_name="D", country="US", sno=4,
                         error="missing from LLM response"),
        ]
        counts: dict[str, int] = {"found": 0, "not_found": 0, "errored": 0}
        for r in results:
            counts[classify_one_result(r)] += 1
        outcome, _ = classify_ai_mode_outcomes(
            [
                EntityResult(company_name="A", country="US", sno=1,
                             website_url="https://a.com", confidence=90),
                EntityResult(company_name="B", country="US", sno=2),
                EntityResult(company_name="C", country="US", sno=3,
                             error="scrape.do error: HTTP 429",
                             error_source=SRC_SCRAPEDO, error_category=CAT_RATE_LIMIT),
                EntityResult(company_name="D", country="US", sno=4,
                             error="missing from LLM response"),
            ]
        )
        self.assertEqual(counts, outcome)


class TestBuildRunUpdateOutcome(unittest.TestCase):
    def test_success_is_found_and_failed_is_errored_not_notfound(self):
        # 1 found, 1 not_found, 1 errored: success_count=1, failed_count=1 (NOT 2).
        summary = {
            "status": "completed_with_errors",
            "websites_found": 1,
            "websites_not_found": 2,  # includes the errored entity (no url)
            "llm_errors": 0,
            "failed_request_count": 1,
            "outcome_breakdown": {"found": 1, "not_found": 1, "errored": 1},
            "error_breakdown": {"by_source": {SRC_SCRAPEDO: 1}, "by_category": {CAT_RATE_LIMIT: 1}},
        }
        update = _build_run_update(summary, {})
        self.assertEqual(update["success_count"], 1)
        self.assertEqual(update["failed_count"], 1)
        # websites_found / websites_not_found are unchanged passthrough.
        self.assertEqual(update["websites_found"], 1)
        self.assertEqual(update["websites_not_found"], 2)


if __name__ == "__main__":
    unittest.main()
