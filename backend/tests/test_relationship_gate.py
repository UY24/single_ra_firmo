# backend/tests/test_relationship_gate.py
import json
import unittest
from unittest.mock import patch

from app.services.serpwow.gemini_llm import (
    apply_relationship_gate,
    build_relationship_prompt,
    update_relationship_block,
)

CANDS = ["https://modal.com/", "https://example.org/"]
EVIDENCE = [{
    "phase": "phase1_relationship_and_url",
    "query": "q1",
    "text": "Eastlink invested in Modal.\n1. Website: https://modal.com/",
    "sources": [{"name": "Funding report", "url": "https://news.example/modal"}],
}]


class TestApplyRelationshipGate(unittest.TestCase):
    def test_confirmed_in_candidates_keeps_url(self):
        parsed = {"relationship_status": "confirmed",
                  "official_website": "https://modal.com/",
                  "relationship_confidence_score": 95,
                  "website_confidence_score": 88}
        url, status, flags = apply_relationship_gate(parsed, CANDS, "eastlinkcap.com")
        self.assertEqual(url, "https://modal.com/")
        self.assertEqual(status, "confirmed")
        self.assertEqual(parsed["confidence_score"], 88)

    def test_legacy_batch_confidence_populates_both_new_scores(self):
        parsed = {"relationship_status": "confirmed",
                  "official_website": "https://modal.com/",
                  "confidence_score": 83}
        url, status, _flags = apply_relationship_gate(parsed, CANDS, "")
        self.assertEqual(url, "https://modal.com/")
        self.assertEqual(status, "confirmed")
        self.assertEqual(parsed["relationship_confidence_score"], 83)
        self.assertEqual(parsed["website_confidence_score"], 83)
        self.assertEqual(parsed["confidence_score"], 83)

    def test_unconfirmed_strips_url_into_flag(self):
        parsed = {"relationship_status": "not_confirmed",
                  "official_website": "https://modal.com/",
                  "relationship_confidence_score": 30,
                  "website_confidence_score": 87}
        url, status, flags = apply_relationship_gate(parsed, CANDS, "")
        self.assertIsNone(url)
        self.assertEqual(status, "not_confirmed")
        self.assertTrue(any(f["flag"] == "url_found_no_relationship" and
                            "modal.com" in f["why"] for f in flags))
        self.assertEqual(parsed["relationship_confidence_score"], 30)
        self.assertEqual(parsed["website_confidence_score"], 87)
        self.assertEqual(parsed["confidence_score"], 0)

    def test_confirmed_without_url_preserves_relationship_confidence(self):
        parsed = {"relationship_status": "confirmed", "official_website": None,
                  "relationship_confidence_score": 96,
                  "website_confidence_score": 55}
        url, status, flags = apply_relationship_gate(parsed, CANDS, "")
        self.assertIsNone(url)
        self.assertEqual(status, "confirmed")
        self.assertEqual(parsed["relationship_confidence_score"], 96)
        self.assertEqual(parsed["website_confidence_score"], 0)
        self.assertEqual(parsed["confidence_score"], 0)
        self.assertTrue(any(f["flag"] == "relationship_confirmed_url_missing"
                            for f in flags))

    def test_unclear_strips_url_into_flag(self):
        parsed = {"relationship_status": "unclear",
                  "official_website": "https://modal.com/"}
        url, status, flags = apply_relationship_gate(parsed, CANDS, "")
        self.assertIsNone(url)
        self.assertEqual(status, "unclear")
        self.assertTrue(any(f["flag"] == "relationship_unclear" for f in flags))

    def test_relationship_block_treats_single_string_fields_as_one_item(self):
        parsed = {"relationship_summary": "summary",
                  "relationship_evidence": "one evidence statement",
                  "relationship_confidence_score": 90,
                  "website_confidence_score": 0,
                  "extra_flags": "company_closed"}
        relationship = update_relationship_block(
            {"flags": []}, parsed, "confirmed", [])
        self.assertEqual(relationship["evidence"], ["one evidence statement"])
        self.assertEqual(relationship["flags"], [{
            "flag": "company_closed", "why": "reported by LLM"}])

    def test_out_of_candidate_url_is_nulled(self):
        parsed = {"relationship_status": "confirmed",
                  "official_website": "https://invented.example/"}
        url, status, flags = apply_relationship_gate(parsed, CANDS, "")
        self.assertIsNone(url)
        self.assertTrue(any(f["flag"] == "llm_url_out_of_candidates" for f in flags))

    def test_x_domain_url_is_rejected(self):
        parsed = {"relationship_status": "confirmed",
                  "official_website": "https://www.eastlinkcap.com/team"}
        url, status, flags = apply_relationship_gate(
            parsed, ["https://www.eastlinkcap.com/team"], "eastlinkcap.com")
        self.assertIsNone(url)
        self.assertTrue(any(f["flag"] == "x_domain_candidate_dropped" for f in flags))

    def test_unknown_status_treated_as_unclear(self):
        parsed = {"relationship_status": "banana", "official_website": None}
        url, status, flags = apply_relationship_gate(parsed, CANDS, "")
        self.assertIsNone(url)
        self.assertEqual(status, "unclear")


class TestBuildRelationshipPrompt(unittest.TestCase):
    def test_prompt_contains_contract_and_evidence(self):
        prompt = build_relationship_prompt(
            "m25vc", "Sanzo", "https://m25vc.com/portfolio", CANDS,
            EVIDENCE, [{"attempt": "phase1_relationship_and_url", "query": "q1"}],
            "eastlinkcap.com")
        for needle in ("relationship_status", "confirmed", "not_confirmed", "unclear",
                       "official_website", "relationship_confidence_score",
                       "website_confidence_score",
                       "relationship_evidence", "extra_flags",
                       "Eastlink invested in Modal", "Funding report",
                       "m25vc", "Sanzo", "financial",
                       "company_x_domain", "eastlinkcap.com"):
            self.assertIn(needle, prompt)

    def test_prompt_falls_back_to_name_when_no_domain(self):
        # No domain: company_x_domain serializes to null but the X name is still present.
        prompt = build_relationship_prompt(
            "m25vc", "Sanzo", "https://m25vc.com/portfolio", CANDS,
            EVIDENCE, [{"attempt": "phase1_relationship_and_url", "query": "q1"}],
            "")
        self.assertIn("company_x_domain", prompt)
        self.assertIn('"company_x_domain": null', prompt)
        self.assertIn("m25vc", prompt)


if __name__ == "__main__":
    unittest.main()
