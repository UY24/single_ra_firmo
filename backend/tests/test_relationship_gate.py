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


class TestApplyRelationshipGate(unittest.TestCase):
    def test_confirmed_in_candidates_keeps_url(self):
        parsed = {"relationship_status": "confirmed",
                  "official_website": "https://modal.com/", "confidence_score": 90}
        url, status, flags = apply_relationship_gate(parsed, CANDS, "eastlinkcap.com")
        self.assertEqual(url, "https://modal.com/")
        self.assertEqual(status, "confirmed")

    def test_unconfirmed_strips_url_into_flag(self):
        parsed = {"relationship_status": "not_confirmed",
                  "official_website": "https://modal.com/", "confidence_score": 40}
        url, status, flags = apply_relationship_gate(parsed, CANDS, "")
        self.assertIsNone(url)
        self.assertEqual(status, "not_confirmed")
        self.assertTrue(any(f["flag"] == "url_found_no_relationship" and
                            "modal.com" in f["why"] for f in flags))

    def test_unclear_strips_url_into_flag(self):
        parsed = {"relationship_status": "unclear",
                  "official_website": "https://modal.com/"}
        url, status, flags = apply_relationship_gate(parsed, CANDS, "")
        self.assertIsNone(url)
        self.assertEqual(status, "unclear")
        self.assertTrue(any(f["flag"] == "relationship_unclear" for f in flags))

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


class TestChooseRelationshipAndWebsite(unittest.TestCase):
    def test_happy_path_parses_json_and_returns_usage(self):
        llm_json = json.dumps({
            "relationship_status": "confirmed",
            "relationship_summary": "Eastlink is an investor in Modal Labs.",
            "official_website": "https://modal.com/",
            "confidence_score": 92, "reason": "AI overview names both parties.",
            "extra_flags": [],
        })
        usage = {"promptTokenCount": 100, "candidatesTokenCount": 50}
        with patch("app.services.serpwow.gemini_llm._gemini_generate_content_json",
                   return_value=(llm_json, usage, None)) as seam:
            parsed, error, model, out_usage = choose_relationship_and_website(
                "eastlinkcap", "Modal", "", "",
                CANDS, ["Eastlink Capital is an investor in Modal Labs."],
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
                "x", "y", "", "", CANDS, [], [], False)
        self.assertIsNone(parsed)
        self.assertIn("500", error)


class TestBuildRelationshipPrompt(unittest.TestCase):
    def test_prompt_contains_contract_and_evidence(self):
        prompt = build_relationship_prompt(
            "m25vc", "Sanzo", "NYC", "US", CANDS,
            ["overview text A"], [{"attempt": "phase1_relationship", "query": "q1"}], True,
            "eastlinkcap.com")
        for needle in ("relationship_status", "confirmed", "not_confirmed", "unclear",
                       "official_website", "confidence_score", "extra_flags",
                       "overview text A", "m25vc", "Sanzo", "financial",
                       "company_x_domain", "eastlinkcap.com"):
            self.assertIn(needle, prompt)

    def test_prompt_falls_back_to_name_when_no_domain(self):
        # No domain: company_x_domain serializes to null but the X name is still present.
        prompt = build_relationship_prompt(
            "m25vc", "Sanzo", "NYC", "US", CANDS,
            ["overview text A"], [{"attempt": "phase1_relationship", "query": "q1"}], True,
            "")
        self.assertIn("company_x_domain", prompt)
        self.assertIn('"company_x_domain": null', prompt)
        self.assertIn("m25vc", prompt)


if __name__ == "__main__":
    unittest.main()
