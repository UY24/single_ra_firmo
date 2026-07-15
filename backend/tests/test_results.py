# backend/tests/test_results.py
import unittest

from app.models.results import AttemptLogEntry, EntityResult, Flag


class TestEntityResultSerialization(unittest.TestCase):
    def _result(self):
        return EntityResult(
            company_name="Acme KK", company_local_name="アクメ", country="Japan",
            website_url="https://acme.jp", confidence=85,
            flags=[Flag("name_match", "exact ENG match"), Flag("tld_match", ".jp matches country")],
            attempt_log=[AttemptLogEntry("Acme KK Japan official site", "found acme.jp", "https://acme.jp"),
                         AttemptLogEntry("アクメ 株式会社", "same domain confirmed", None)],
            sno=1)

    def test_flags_csv(self):
        self.assertEqual(self._result().flags_csv(),
                         "name_match: exact ENG match\ntld_match: .jp matches country")

    def test_attempt_log_csv(self):
        self.assertEqual(self._result().attempt_log_csv(),
                         "1. Acme KK Japan official site → found acme.jp [https://acme.jp]\n"
                         "2. アクメ 株式会社 → same domain confirmed")

    def test_from_llm_object_tolerates_missing_fields(self):
        r = EntityResult.from_llm_object({"sno": 2, "company_name": "Beta"},
                                         fallback_country="Germany")
        self.assertEqual((r.sno, r.country, r.confidence, r.website_url),
                         (2, "Germany", 0, None))

    def test_from_llm_object_clamps_confidence(self):
        high = EntityResult.from_llm_object({"company_name": "A", "confidence": 250})
        low = EntityResult.from_llm_object({"company_name": "A", "confidence": -3})
        bad = EntityResult.from_llm_object({"company_name": "A", "confidence": "n/a"})
        self.assertEqual((high.confidence, low.confidence, bad.confidence), (100, 0, 0))

    def test_to_report_dict_round_trip_shape(self):
        d = self._result().to_report_dict()
        self.assertEqual(set(d), {"sno", "company_name", "company_local_name", "country",
                                  "website_url", "confidence", "flags", "attempt_log", "error",
                                  "error_source", "error_category", "degraded_search"})
        self.assertEqual(d["sno"], 1)
        self.assertEqual(d["company_name"], "Acme KK")
        self.assertEqual(d["company_local_name"], "アクメ")
        self.assertEqual(d["country"], "Japan")
        self.assertEqual(d["website_url"], "https://acme.jp")
        self.assertEqual(d["confidence"], 85)
        self.assertEqual(d["flags"], [{"flag": "name_match", "why": "exact ENG match"},
                                      {"flag": "tld_match", "why": ".jp matches country"}])
        self.assertEqual(d["attempt_log"],
                         [{"query": "Acme KK Japan official site", "result": "found acme.jp",
                           "url": "https://acme.jp"},
                          {"query": "アクメ 株式会社", "result": "same domain confirmed",
                           "url": None}])
        self.assertIsNone(d["error"])
        # Round-trip: feeding the report dict back through from_llm_object
        # reconstructs an equal EntityResult.
        self.assertEqual(EntityResult.from_llm_object(d), self._result())


class TestCleanupMessages(unittest.TestCase):
    def test_build_messages_uses_prompt_file(self):
        from app.services.ai_mode.cleanup import build_cleanup_messages
        msgs = build_cleanup_messages("RAW RESPONSE TEXT")
        self.assertEqual(msgs[0]["role"], "system")
        self.assertIn("attempt_log", msgs[0]["content"])
        self.assertIn("RAW RESPONSE TEXT", msgs[1]["content"])

    def test_build_messages_includes_entities_block(self):
        from app.models.entities import Entity
        from app.services.ai_mode.cleanup import build_cleanup_messages
        msgs = build_cleanup_messages(
            "RAW", entities=[Entity(company_name="Acme KK", country="Japan", sno=1)])
        self.assertIn("Input companies:", msgs[1]["content"])
        self.assertIn("1. Acme KK — Japan", msgs[1]["content"])


class TestParseCleanupResponse(unittest.TestCase):
    def test_missing_entity_gets_error_result(self):
        from app.models.entities import Entity
        from app.services.ai_mode.cleanup import parse_cleanup_response
        entities = [Entity(company_name="Acme KK", country="Japan", sno=1,
                           company_local_name="アクメ"),
                    Entity(company_name="Beta GmbH", country="Germany", sno=2)]
        parsed = [{"sno": 1, "company_name": "Acme KK", "country": "Japan",
                   "website_url": "https://acme.jp", "confidence": 85,
                   "flags": [{"flag": "name_match", "why": "exact"}],
                   "attempt_log": [{"query": "q", "result": "r", "url": None}]}]
        results = parse_cleanup_response(parsed, entities)
        self.assertEqual(len(results), 2)
        first, second = results
        self.assertEqual(first.website_url, "https://acme.jp")
        self.assertEqual(first.confidence, 85)
        self.assertEqual(first.company_local_name, "アクメ")
        self.assertIsNone(first.error)
        self.assertEqual(second.error, "missing from LLM response")
        self.assertEqual((second.sno, second.company_name, second.country),
                         (2, "Beta GmbH", "Germany"))
        self.assertIsNone(second.website_url)

    def test_non_list_response_marks_all_missing(self):
        from app.models.entities import Entity
        from app.services.ai_mode.cleanup import parse_cleanup_response
        entities = [Entity(company_name="Acme KK", country="Japan", sno=1)]
        results = parse_cleanup_response({"not": "a list"}, entities)
        self.assertEqual(results[0].error, "missing from LLM response")


class TestParseJsonArrayFromText(unittest.TestCase):
    def test_bare_array(self):
        from app.services.ai_mode.cleanup import parse_json_array_from_text
        self.assertEqual(parse_json_array_from_text('[{"sno": 1}]'), [{"sno": 1}])

    def test_markdown_fenced_array(self):
        from app.services.ai_mode.cleanup import parse_json_array_from_text
        text = "```json\n[{\"sno\": 1}, {\"sno\": 2}]\n```"
        self.assertEqual(parse_json_array_from_text(text), [{"sno": 1}, {"sno": 2}])

    def test_object_wrapped_array(self):
        from app.services.ai_mode.cleanup import parse_json_array_from_text
        self.assertEqual(parse_json_array_from_text('{"entities": [{"sno": 1}]}'),
                         [{"sno": 1}])

    def test_single_object_wrapper_key(self):
        from app.services.ai_mode.cleanup import parse_json_array_from_text
        self.assertEqual(parse_json_array_from_text('{"records": [{"sno": 1}]}'),
                         [{"sno": 1}])

    def test_bare_single_result_object(self):
        from app.services.ai_mode.cleanup import parse_json_array_from_text
        self.assertEqual(
            parse_json_array_from_text('{"sno": 1, "company_name": "Acme", "confidence": 5}'),
            [{"sno": 1, "company_name": "Acme", "confidence": 5}])

    def test_invalid_json_returns_none(self):
        from app.services.ai_mode.cleanup import parse_json_array_from_text
        self.assertIsNone(parse_json_array_from_text("not json"))
        self.assertIsNone(parse_json_array_from_text(""))

    def test_non_array_object_returns_none(self):
        from app.services.ai_mode.cleanup import parse_json_array_from_text
        self.assertIsNone(parse_json_array_from_text('{"foo": "bar", "baz": 1}'))

    def test_coerce_already_parsed(self):
        from app.services.ai_mode.cleanup import coerce_json_array
        self.assertEqual(coerce_json_array([{"sno": 1}]), [{"sno": 1}])
        self.assertEqual(coerce_json_array({"entities": [{"sno": 1}]}), [{"sno": 1}])
        self.assertIsNone(coerce_json_array("text"))
        self.assertIsNone(coerce_json_array(None))


if __name__ == "__main__":
    unittest.main()
