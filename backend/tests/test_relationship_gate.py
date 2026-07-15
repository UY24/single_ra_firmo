# backend/tests/test_relationship_gate.py
import json
import unittest
from unittest.mock import patch

from app.services.serpwow.gemini_llm import (
    apply_relationship_gate,
    build_relationship_prompt,
    choose_relationship_and_website,
)

CANDS = ["https://modal.com/", "https://example.org/"]
CANDIDATE_EVIDENCE = [{
    "evidence_id": "candidate-1", "url": "https://modal.com/",
    "phase": "phase1", "source_field": "knowledge_graph.website",
}]
RELATIONSHIP_EVIDENCE = [{
    "evidence_id": "relationship-1", "text": "Eastlink invested in Modal.",
    "phase": "phase1", "source_field": "ai_overview",
}]


class TestApplyRelationshipGate(unittest.TestCase):
    def test_confirmed_in_candidates_keeps_url(self):
        parsed = {"relationship_status": "confirmed",
                  "official_website": "https://modal.com/", "confidence_score": 90,
                  "supporting_evidence_ids": ["relationship-1"]}
        url, status, flags, accepted_ids = apply_relationship_gate(
            parsed, CANDS, "eastlinkcap.com",
            {"candidate-1", "relationship-1"}, {"relationship-1"})
        self.assertEqual(url, "https://modal.com")
        self.assertEqual(status, "confirmed")
        self.assertEqual(accepted_ids, ["relationship-1"])

    def test_unconfirmed_strips_url_into_flag(self):
        parsed = {"relationship_status": "not_confirmed",
                  "official_website": "https://modal.com/", "confidence_score": 40}
        url, status, flags, accepted_ids = apply_relationship_gate(parsed, CANDS, "")
        self.assertIsNone(url)
        self.assertEqual(status, "not_confirmed")
        self.assertTrue(any(f["flag"] == "url_found_no_relationship" and
                            "modal.com" in f["why"] for f in flags))

    def test_unclear_strips_url_into_flag(self):
        parsed = {"relationship_status": "unclear",
                  "official_website": "https://modal.com/"}
        url, status, flags, accepted_ids = apply_relationship_gate(parsed, CANDS, "")
        self.assertIsNone(url)
        self.assertEqual(status, "unclear")
        self.assertTrue(any(f["flag"] == "relationship_unclear" for f in flags))

    def test_out_of_candidate_url_is_nulled(self):
        parsed = {"relationship_status": "confirmed",
                  "official_website": "https://invented.example/",
                  "confidence_score": 88,
                  "supporting_evidence_ids": ["relationship-1"]}
        url, status, flags, accepted_ids = apply_relationship_gate(
            parsed, CANDS, "", {"relationship-1"}, {"relationship-1"})
        self.assertIsNone(url)
        self.assertEqual(parsed["confidence_score"], 0)
        self.assertIsNone(parsed["official_website"])
        self.assertTrue(any(f["flag"] == "llm_url_out_of_candidates" for f in flags))

    def test_x_domain_url_is_rejected(self):
        parsed = {"relationship_status": "confirmed",
                  "official_website": "https://www.eastlinkcap.com/team",
                  "confidence_score": 75,
                  "supporting_evidence_ids": ["relationship-1"]}
        url, status, flags, accepted_ids = apply_relationship_gate(
            parsed, ["https://www.eastlinkcap.com/team"], "eastlinkcap.com",
            {"relationship-1"}, {"relationship-1"})
        self.assertIsNone(url)
        self.assertEqual(parsed["confidence_score"], 0)
        self.assertTrue(any(f["flag"] == "x_domain_candidate_dropped" for f in flags))

    def test_disallowed_url_is_rejected_and_score_zeroed(self):
        parsed = {"relationship_status": "confirmed",
                  "official_website": "https://linkedin.com/company/modal",
                  "confidence_score": 75,
                  "supporting_evidence_ids": ["relationship-1"]}
        url, status, flags, accepted_ids = apply_relationship_gate(
            parsed, ["https://linkedin.com/company/modal"], "",
            {"relationship-1"}, {"relationship-1"})
        self.assertIsNone(url)
        self.assertEqual(parsed["confidence_score"], 0)
        self.assertTrue(any(f["flag"] == "disallowed_url_dropped" for f in flags))

    def test_unknown_status_treated_as_unclear(self):
        parsed = {"relationship_status": "banana", "official_website": None}
        url, status, flags, accepted_ids = apply_relationship_gate(parsed, CANDS, "")
        self.assertIsNone(url)
        self.assertEqual(status, "unclear")
        self.assertEqual(parsed["relationship_status"], "unclear")

    def test_confirmed_with_candidate_only_evidence_is_downgraded(self):
        parsed = {"relationship_status": "confirmed",
                  "official_website": "https://modal.com/", "confidence_score": 90,
                  "supporting_evidence_ids": ["candidate-1"]}
        url, status, flags, accepted_ids = apply_relationship_gate(
            parsed, CANDS, "", {"candidate-1", "relationship-1"}, {"relationship-1"})
        self.assertIsNone(url)
        self.assertEqual(status, "unclear")
        self.assertEqual(parsed["confidence_score"], 0)
        self.assertEqual(accepted_ids, ["candidate-1"])
        self.assertTrue(any(f["flag"] == "confirmed_without_evidence_id" for f in flags))

    def test_unknown_ids_are_dropped_and_valid_ids_are_deduplicated(self):
        parsed = {"relationship_status": "confirmed",
                  "official_website": "https://modal.com/", "confidence_score": 90,
                  "supporting_evidence_ids": [
                      "relationship-2", "missing", "relationship-1", "relationship-2"]}
        url, status, flags, accepted_ids = apply_relationship_gate(
            parsed, CANDS, "",
            {"relationship-1", "relationship-2"},
            {"relationship-1", "relationship-2"})
        self.assertEqual(accepted_ids, ["relationship-2", "relationship-1"])
        self.assertEqual(parsed["supporting_evidence_ids"], accepted_ids)
        self.assertTrue(any(f["flag"] == "unknown_evidence_id" for f in flags))

    def test_confirmed_with_only_unknown_id_is_downgraded(self):
        parsed = {"relationship_status": "confirmed",
                  "official_website": "https://modal.com/", "confidence_score": 90,
                  "supporting_evidence_ids": ["missing"]}
        url, status, flags, accepted_ids = apply_relationship_gate(
            parsed, CANDS, "", {"relationship-1"}, {"relationship-1"})
        self.assertEqual((url, status, accepted_ids), (None, "unclear", []))
        self.assertTrue(any(f["flag"] == "unknown_evidence_id" for f in flags))
        self.assertTrue(any(f["flag"] == "confirmed_without_evidence_id" for f in flags))

    def test_confidence_score_above_100_is_clamped(self):
        parsed = {"relationship_status": "not_confirmed", "confidence_score": 123}
        apply_relationship_gate(parsed, CANDS, "")
        self.assertEqual(parsed["confidence_score"], 100)

    def test_confidence_score_below_zero_is_clamped(self):
        parsed = {"relationship_status": "not_confirmed", "confidence_score": -5}
        apply_relationship_gate(parsed, CANDS, "")
        self.assertEqual(parsed["confidence_score"], 0)

    def test_nonnumeric_confidence_score_becomes_zero(self):
        parsed = {"relationship_status": "not_confirmed", "confidence_score": "high"}
        apply_relationship_gate(parsed, CANDS, "")
        self.assertEqual(parsed["confidence_score"], 0)

    def test_nonconfirmed_without_evidence_id_remains_valid(self):
        parsed = {"relationship_status": "not_confirmed",
                  "official_website": "https://modal.com/"}
        url, status, flags, accepted_ids = apply_relationship_gate(
            parsed, CANDS, "", {"relationship-1"}, {"relationship-1"})
        self.assertEqual((url, status, accepted_ids), (None, "not_confirmed", []))
        self.assertFalse(any(f["flag"] == "confirmed_without_evidence_id" for f in flags))


class TestChooseRelationshipAndWebsite(unittest.TestCase):
    def test_happy_path_parses_json_and_returns_usage(self):
        llm_json = json.dumps({
            "relationship_status": "confirmed",
            "relationship_summary": "Eastlink is an investor in Modal Labs.",
            "official_website": "https://modal.com/",
            "confidence_score": 92, "reason": "AI overview names both parties.",
            "supporting_evidence_ids": ["relationship-1"],
            "extra_flags": [],
        })
        usage = {"promptTokenCount": 100, "candidatesTokenCount": 50}
        with patch("app.services.serpwow.gemini_llm._gemini_generate_content_json",
                   return_value=(llm_json, usage, None)) as seam:
            parsed, error, model, out_usage = choose_relationship_and_website(
                "eastlinkcap", "Modal", "", "",
                CANDS, CANDIDATE_EVIDENCE, RELATIONSHIP_EVIDENCE,
                [{"attempt": "phase1_relationship", "query": "q"}], True,
                "eastlinkcap.com")
        self.assertIsNone(error)
        self.assertEqual(parsed["relationship_status"], "confirmed")
        self.assertEqual(out_usage, usage)
        prompt_sent = seam.call_args[0][1]
        self.assertIn("eastlinkcap", prompt_sent)
        self.assertIn("Modal", prompt_sent)
        self.assertIn("modal.com", prompt_sent)
        self.assertIn("eastlinkcap.com", prompt_sent)
        self.assertIn("company_x_domain", prompt_sent)

    def test_http_error_surfaces(self):
        with patch("app.services.serpwow.gemini_llm._gemini_generate_content_json",
                   return_value=(None, None, "Gemini HTTPError: 500")):
            parsed, error, model, usage = choose_relationship_and_website(
                "x", "y", "", "", CANDS, [], [], [], False)
        self.assertIsNone(parsed)
        self.assertIn("500", error)


class TestBuildRelationshipPrompt(unittest.TestCase):
    def test_prompt_contains_contract_and_evidence(self):
        prompt = build_relationship_prompt(
            "m25vc", "Sanzo", "NYC", "US", CANDS,
            CANDIDATE_EVIDENCE, RELATIONSHIP_EVIDENCE,
            [{"attempt": "phase1_relationship", "query": "q1"}], True,
            "eastlinkcap.com")
        for needle in ("relationship_status", "confirmed", "not_confirmed", "unclear",
                       "official_website", "confidence_score", "extra_flags",
                       "supporting_evidence_ids", "Candidate Evidence",
                       "Supplied Relationship Evidence", "candidate-1", "relationship-1",
                       "Eastlink invested in Modal.", "m25vc", "Sanzo", "financial",
                       "company_x_domain", "eastlinkcap.com"):
            self.assertIn(needle, prompt)
        self.assertIn("do not use outside knowledge", prompt.lower())
        self.assertIn("Do not create company identity, transaction, quotation, URL, or evidence item", prompt)
        self.assertIn("exactly match one Candidate URL", prompt)

    def test_prompt_falls_back_to_name_when_no_domain(self):
        # No domain: company_x_domain serializes to null but the X name is still present.
        prompt = build_relationship_prompt(
            "m25vc", "Sanzo", "NYC", "US", CANDS,
            CANDIDATE_EVIDENCE, RELATIONSHIP_EVIDENCE,
            [{"attempt": "phase1_relationship", "query": "q1"}], True,
            "")
        self.assertIn("company_x_domain", prompt)
        self.assertIn('"company_x_domain": null', prompt)
        self.assertIn("m25vc", prompt)


if __name__ == "__main__":
    unittest.main()
