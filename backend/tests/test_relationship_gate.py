# backend/tests/test_relationship_gate.py
import json
import unittest
from unittest.mock import patch

from app.services.serpwow.gemini_llm import (
    _relationship_evidence_gate_inputs,
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


def _prompt_section(prompt, label):
    start = prompt.index(f"{label}: ") + len(label) + 2
    end = prompt.index("\n\n", start)
    return json.loads(prompt[start:end])


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

    def test_explicit_filename_shaped_host_is_not_globally_dropped_at_gate(self):
        parsed = {
            "relationship_status": "confirmed",
            "official_website": "https://report.pdf",
            "confidence_score": 90,
            "supporting_evidence_ids": ["relationship-1"],
        }

        url, status, flags, _ = apply_relationship_gate(
            parsed, ["https://report.pdf"], "",
            {"relationship-1"}, {"relationship-1"})

        self.assertEqual(url, "https://report.pdf")
        self.assertEqual(status, "confirmed")
        self.assertFalse(any(f["flag"] == "disallowed_url_dropped" for f in flags))

    def test_unknown_status_treated_as_unclear(self):
        parsed = {"relationship_status": "banana", "official_website": None,
                  "confidence_score": 88}
        url, status, flags, accepted_ids = apply_relationship_gate(parsed, CANDS, "")
        self.assertIsNone(url)
        self.assertEqual(status, "unclear")
        self.assertEqual(parsed["relationship_status"], "unclear")
        self.assertEqual(parsed["confidence_score"], 0)

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

    def test_confirmed_without_url_keeps_relationship_confidence(self):
        parsed = {"relationship_status": "confirmed",
                  "official_website": None, "confidence_score": 86,
                  "supporting_evidence_ids": ["relationship-1"]}
        url, status, flags, accepted_ids = apply_relationship_gate(
            parsed, CANDS, "", {"relationship-1"}, {"relationship-1"})
        self.assertEqual((url, status), (None, "confirmed"))
        self.assertEqual(parsed["confidence_score"], 86)

    def test_candidate_relationship_id_collision_is_rejected_from_trust_set(self):
        candidate = [{"evidence_id": "collision", "url": "https://modal.com"}]
        relationship = [{"evidence_id": "collision", "text": "X invested in Y."}]
        allowed, relationship_ids, supplied = _relationship_evidence_gate_inputs(
            candidate, relationship)
        parsed = {"relationship_status": "confirmed",
                  "official_website": "https://modal.com", "confidence_score": 90,
                  "supporting_evidence_ids": ["collision"]}
        url, status, flags, accepted_ids = apply_relationship_gate(
            parsed, ["https://modal.com"], "", allowed, relationship_ids)
        self.assertEqual((allowed, relationship_ids, supplied), (set(), set(), []))
        self.assertEqual((url, status, accepted_ids), (None, "unclear", []))
        self.assertTrue(any(flag["flag"] == "unknown_evidence_id" for flag in flags))

    def test_negative_and_nonfinancial_records_cannot_confirm(self):
        evidence = [
            {"evidence_id": "negative", "text": "No investment is documented between Eastlink and Modal."},
            {"evidence_id": "nonfinancial", "text": "Eastlink and Modal announced a partnership."},
        ]
        allowed, relationship_ids, supplied = _relationship_evidence_gate_inputs(
            [], evidence, x_name="Eastlink", y_name="Modal")
        parsed = {
            "relationship_status": "confirmed",
            "official_website": "https://modal.com",
            "confidence_score": 90,
            "supporting_evidence_ids": ["negative", "nonfinancial"],
        }

        url, status, _, accepted_ids = apply_relationship_gate(
            parsed, ["https://modal.com"], "", allowed, relationship_ids)

        self.assertEqual(allowed, {"negative", "nonfinancial"})
        self.assertEqual(relationship_ids, set())
        self.assertEqual(supplied, evidence)
        self.assertEqual((url, status), (None, "unclear"))
        self.assertEqual(accepted_ids, ["negative", "nonfinancial"])


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

    def test_prompt_keeps_negative_records_as_contrary_context(self):
        evidence = [{
            "evidence_id": "negative",
            "text": "No investment is documented between Eastlink and Modal.",
        }]
        prompt = build_relationship_prompt(
            "Eastlink", "Modal", "", "", CANDS, [], evidence, [], False, "")

        self.assertIn("No investment is documented", prompt)

    def test_prompt_sections_are_complete_bounded_json_and_gate_uses_same_subset(self):
        candidate_records = [
            {"evidence_id": f"candidate-{index}", "url": f"https://{index}.example",
             "original_text": "c" * 1800}
            for index in range(10)
        ]
        relationship_records = [
            {"evidence_id": f"relationship-{index}",
             "text": "X invested in Y. " + "r" * 2500}
            for index in range(10)
        ]
        prompt = build_relationship_prompt(
            "X", "Y", "", "", CANDS, candidate_records,
            relationship_records, [], False, "")
        prompt_candidates = _prompt_section(prompt, "Candidate Evidence")
        prompt_relationship = _prompt_section(prompt, "Supplied Relationship Evidence")
        allowed, relationship_ids, supplied = _relationship_evidence_gate_inputs(
            candidate_records, relationship_records)

        self.assertLessEqual(len(json.dumps(
            prompt_candidates, ensure_ascii=True, sort_keys=True,
            separators=(",", ":"))), 8000)
        self.assertLessEqual(len(json.dumps(
            prompt_relationship, ensure_ascii=True, sort_keys=True,
            separators=(",", ":"))), 14000)
        self.assertEqual(
            allowed,
            {record["evidence_id"] for record in prompt_candidates + prompt_relationship},
        )
        self.assertEqual(
            relationship_ids,
            {record["evidence_id"] for record in prompt_relationship},
        )
        self.assertNotIn("candidate-9", allowed)
        self.assertNotIn("relationship-9", relationship_ids)
        self.assertEqual(supplied, prompt_candidates + prompt_relationship)


if __name__ == "__main__":
    unittest.main()
