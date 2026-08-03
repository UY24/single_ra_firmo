import unittest
from app.services.serpwow import outcomes as o
from app.services.serpwow.constants import REL_ERROR_NOT_CONFIRMED, REL_ERROR_NO_EVIDENCE


def _result(official=None, phases=None, llm_error=None):
    fr = []
    for p in (phases or []):
        fr.append({"phase": p.get("phase", "x"), "success": p.get("used", False),
                   "error": p.get("error"), "status_code": p.get("status_code"),
                   "error_category": p.get("error_category")})
    ctx = {"formatted_results": fr}
    if llm_error is not None:
        ctx["llm_error"] = llm_error
    return {"official_website": official, "context": ctx}


class TestOutcomeInfo(unittest.TestCase):
    def test_row_status_error_is_failed(self):
        self.assertEqual(o.OutcomeInfo(o.OUTCOME_ERROR, o.SRC_SERPWOW, o.CAT_TIMEOUT).row_status, "failed")

    def test_row_status_notfound_is_completed(self):
        self.assertEqual(o.OutcomeInfo(o.OUTCOME_NOT_FOUND).row_status, "completed")
        self.assertEqual(o.OutcomeInfo(o.OUTCOME_FOUND).row_status, "completed")


class TestCategorizeHttp(unittest.TestCase):
    def test_429_rate_limit(self):
        self.assertEqual(o.categorize_http_error(429, "HTTP 429"), o.CAT_RATE_LIMIT)

    def test_401_403_auth(self):
        self.assertEqual(o.categorize_http_error(401, "x"), o.CAT_AUTH)
        self.assertEqual(o.categorize_http_error(403, "x"), o.CAT_AUTH)

    def test_5xx(self):
        self.assertEqual(o.categorize_http_error(502, "x"), o.CAT_HTTP_5XX)

    def test_timeout_from_repr(self):
        self.assertEqual(o.categorize_http_error(None, "ReadTimeout: timed out"), o.CAT_TIMEOUT)

    def test_connect_network(self):
        self.assertEqual(o.categorize_http_error(None, "ConnectError: name resolution"), o.CAT_NETWORK)

    def test_json_parse(self):
        self.assertEqual(o.categorize_http_error(None, "JSONDecodeError: line 1"), o.CAT_PARSE)

    def test_default_internal(self):
        self.assertEqual(o.categorize_http_error(None, "ValueError: nope"), o.CAT_INTERNAL)


class TestClassifyException(unittest.TestCase):
    def test_gemini_timeout(self):
        info = o.classify_exception(TimeoutError("timed out"), default_source=o.SRC_GEMINI)
        self.assertEqual((info.outcome, info.error_source, info.error_category),
                         (o.OUTCOME_ERROR, o.SRC_GEMINI, o.CAT_TIMEOUT))

    def test_server_default_internal(self):
        info = o.classify_exception(ValueError("bug"), default_source=o.SRC_SERVER)
        self.assertEqual(info.error_category, o.CAT_INTERNAL)
        self.assertIn("bug", info.error_detail)

    def test_error_source_override_from_exc_attr(self):
        exc = RuntimeError("relationship LLM error: boom")
        exc.error_source = o.SRC_GEMINI
        info = o.classify_exception(exc, default_source=o.SRC_SERVER)
        self.assertEqual(info.error_source, o.SRC_GEMINI)

    def test_error_source_falls_back_to_default_without_attr(self):
        info = o.classify_exception(RuntimeError("boom"), default_source=o.SRC_SERVER)
        self.assertEqual(info.error_source, o.SRC_SERVER)


class TestClassifyFinalizedRow(unittest.TestCase):
    def test_found(self):
        info = o.classify_finalized_row(_result(official="https://x.com"),
                                        pipeline="gsearch", ctx_row_error=None, skip_llm=False)
        self.assertEqual(info.outcome, o.OUTCOME_FOUND)

    def test_not_found_when_a_phase_searched(self):
        r = _result(official=None, phases=[{"used": True}])
        info = o.classify_finalized_row(r, pipeline="gsearch", ctx_row_error=None, skip_llm=False)
        self.assertEqual(info.outcome, o.OUTCOME_NOT_FOUND)
        self.assertFalse(info.degraded_search)

    def test_degraded_when_some_phases_errored_but_one_searched(self):
        r = _result(official=None, phases=[{"used": True},
                                           {"used": False, "error": "boom", "error_category": o.CAT_TIMEOUT}])
        info = o.classify_finalized_row(r, pipeline="gsearch", ctx_row_error=None, skip_llm=False)
        self.assertEqual(info.outcome, o.OUTCOME_NOT_FOUND)
        self.assertTrue(info.degraded_search)

    def test_error_serpwow_when_all_phases_errored(self):
        r = _result(official=None, phases=[{"used": False, "error": "429", "error_category": o.CAT_RATE_LIMIT},
                                           {"used": False, "error": "429", "error_category": o.CAT_RATE_LIMIT}])
        info = o.classify_finalized_row(r, pipeline="gsearch", ctx_row_error=None, skip_llm=False)
        self.assertEqual((info.outcome, info.error_source, info.error_category),
                         (o.OUTCOME_ERROR, o.SRC_SERPWOW, o.CAT_RATE_LIMIT))

    def test_blank_error_preserves_explicit_timeout_category(self):
        r = _result(official=None, phases=[
            {"used": False, "error": "", "error_category": o.CAT_TIMEOUT},
        ])
        info = o.classify_finalized_row(
            r, pipeline="relationship", ctx_row_error=None, skip_llm=True
        )
        self.assertEqual(
            (info.outcome, info.error_source, info.error_category),
            (o.OUTCOME_ERROR, o.SRC_SERPWOW, o.CAT_TIMEOUT),
        )

    def test_relationship_sentinel_is_not_found(self):
        info = o.classify_finalized_row(_result(official=None), pipeline="relationship",
                                        ctx_row_error=REL_ERROR_NOT_CONFIRMED, skip_llm=True)
        self.assertEqual(info.outcome, o.OUTCOME_NOT_FOUND)

    def test_relationship_no_evidence_does_not_hide_total_serpwow_failure(self):
        r = _result(official=None, phases=[
            {"used": False, "error": "timeout", "error_category": o.CAT_TIMEOUT},
            {"used": False, "error": "timeout", "error_category": o.CAT_TIMEOUT},
            {"used": False, "error": "timeout", "error_category": o.CAT_TIMEOUT},
        ])
        info = o.classify_finalized_row(
            r, pipeline="relationship", ctx_row_error=REL_ERROR_NO_EVIDENCE,
            skip_llm=True)
        self.assertEqual(
            (info.outcome, info.error_source, info.error_category),
            (o.OUTCOME_ERROR, o.SRC_SERPWOW, o.CAT_TIMEOUT),
        )

    def test_no_phases_no_sentinel_is_not_found(self):
        # nothing errored, nothing searched, no website -> conservative not_found
        info = o.classify_finalized_row(_result(official=None), pipeline="gsearch",
                                        ctx_row_error=None, skip_llm=False)
        self.assertEqual(info.outcome, o.OUTCOME_NOT_FOUND)

    def test_found_with_llm_error_is_degraded_found(self):
        # gsearch falls back to the raw first candidate as official_website, so a
        # per-row Gemini selection failure still yields a FOUND row -- but it must
        # be marked degraded so the LLM failure is visible (no error, no retry).
        r = _result(official="https://x.com", phases=[{"used": True}],
                    llm_error="Gemini HTTPError: 429")
        info = o.classify_finalized_row(r, pipeline="gsearch", ctx_row_error=None, skip_llm=False)
        self.assertEqual(info.outcome, o.OUTCOME_FOUND)
        self.assertTrue(info.degraded_search)

    def test_found_without_llm_error_is_not_degraded(self):
        # CONTROL: a found row with no Gemini failure is not degraded.
        r = _result(official="https://x.com", phases=[{"used": True}])
        info = o.classify_finalized_row(r, pipeline="gsearch", ctx_row_error=None, skip_llm=False)
        self.assertEqual(info.outcome, o.OUTCOME_FOUND)
        self.assertFalse(info.degraded_search)

    def test_succeeded_phase_no_llm_error_is_not_found(self):
        # CONTROL: same as above but no llm_error -> unchanged not_found.
        r = _result(official=None, phases=[{"used": True}])
        info = o.classify_finalized_row(r, pipeline="gsearch", ctx_row_error=None, skip_llm=False)
        self.assertEqual(info.outcome, o.OUTCOME_NOT_FOUND)
