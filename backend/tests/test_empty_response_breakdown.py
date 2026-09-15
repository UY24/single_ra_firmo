import unittest

from app.services.serpwow.reporting import empty_response_breakdown


def _phase(empty=True, error=None, overview=False, cands=0):
    return {"phase": "p", "error": error,
            "ai_overview_present": overview, "candidate_count": cands}


def _row(phases, source_row_indices=None):
    row = {"result": {"context": {"formatted_results": phases}}}
    if source_row_indices is not None:
        row["source_row_indices"] = source_row_indices
    return row


class TestEmptyResponseBreakdown(unittest.TestCase):
    def test_gsearch_all_vs_some(self):
        state = {"pipeline": "gsearch", "rows": [
            _row([_phase(), _phase(), _phase()]),               # all empty
            _row([_phase(), _phase(overview=True), _phase()]),  # some empty
            _row([_phase(cands=1), _phase(overview=True)]),     # none empty -> uncounted
        ]}
        self.assertEqual(empty_response_breakdown(state),
                         {"all_phases": 1, "some_phases": 1})

    def test_errored_phase_excluded_not_treated_as_empty(self):
        # A phase that errored is not an "empty 200"; a row with only an errored
        # phase is skipped entirely (nothing to classify).
        state = {"pipeline": "gsearch", "rows": [
            _row([_phase(error="ReadTimeout")]),                    # skipped
            _row([_phase(error="ReadTimeout"), _phase()]),          # 1 real phase, empty -> all
        ]}
        self.assertEqual(empty_response_breakdown(state),
                         {"all_phases": 1, "some_phases": 0})

    def test_missing_fields_and_no_phases_skipped(self):
        state = {"pipeline": "gsearch", "rows": [
            _row([]),                                    # no phases
            _row([{"phase": "p"}]),                       # legacy row, no fields
        ]}
        self.assertEqual(empty_response_breakdown(state),
                         {"all_phases": 0, "some_phases": 0})

    def test_other_pipelines_return_none(self):
        # gmaps has no state.json at all since 2026-08 and reports its own no_listing
        # count from gmaps_outputs, so it never routes through here.
        self.assertIsNone(empty_response_breakdown({"pipeline": "gmaps", "rows": []}))
        self.assertIsNone(empty_response_breakdown({"pipeline": "relationship", "rows": []}))

    def test_firmographics_counts_billed_rows_with_no_overview(self):
        """A billed search that yielded no AI overview is credits spent for nothing."""
        def _row(**scrapedo):
            return {"result": {"context": {"scrapedo": scrapedo}}}
        state = {"pipeline": "firmographics", "rows": [
            _row(billed_no_overview=True),                    # paid, nothing to extract
            _row(billed_no_overview=True, deferred=True),     # paid twice, still nothing
            _row(deferred=True),                              # deferred but recovered
            _row(),                                           # complete inline
            {"result": {"context": {}}},                      # provider never ran
        ]}
        self.assertEqual(empty_response_breakdown(state),
                         {"no_ai_overview": 2, "deferred": 2})


if __name__ == "__main__":
    unittest.main()
