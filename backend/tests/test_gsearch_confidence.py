import unittest
from unittest import mock

from app.services.serpwow import engine as legacy_app
# The Gemini HTTP seam + choose_final_website_with_gemini now live in gemini_llm;
# choose_final resolves _gemini_generate_content_json in gemini_llm's namespace, so the
# mock must patch it there (legacy_app re-exports it for backward-compatible calls).
from app.services.serpwow import gemini_llm


class TestChooseFinalWebsite(unittest.TestCase):
    def _patch_gemini(self, text):
        return mock.patch.object(
            gemini_llm, "_gemini_generate_content_json",
            return_value=(text, {"promptTokenCount": 100, "candidatesTokenCount": 20}, None),
        )

    def test_in_candidate_pick_is_scored(self):
        candidates = ["https://acme-motors.com", "https://other.com"]
        text = ('{"official_website":"https://acme-motors.com","confidence_score":88,'
                '"confidence":"high","reason":"domain matches","evidence":[],"alternatives":[]}')
        with self._patch_gemini(text):
            out, err, model, usage = legacy_app.choose_final_website_with_gemini(
                "Acme Motors", "us", "", "", candidates, [], None, {})
        self.assertIsNone(err)
        self.assertEqual(out["official_website"], "https://acme-motors.com")
        self.assertEqual(out["confidence_score"], 88)

    def test_implausible_in_candidate_pick_is_kept_with_flag(self):
        # Domain has no company-name token (heuristic fails) but it IS a candidate:
        # keep it (don't null) and flag domain_name_mismatch.
        candidates = ["https://brandxyz.com"]
        text = ('{"official_website":"https://brandxyz.com","confidence_score":70,'
                '"confidence":"medium","reason":"r","evidence":[],"alternatives":[]}')
        with self._patch_gemini(text):
            out, err, model, usage = legacy_app.choose_final_website_with_gemini(
                "Totally Different Name", "us", "", "", candidates, [], None, {})
        self.assertEqual(out["official_website"], "https://brandxyz.com")  # kept, not nulled
        self.assertTrue(out.get("domain_name_mismatch"))
        self.assertEqual(out["confidence_score"], 70)

    def test_out_of_candidate_pick_is_rejected_to_zero(self):
        candidates = ["https://acme-motors.com"]
        text = ('{"official_website":"https://invented-elsewhere.com","confidence_score":90,'
                '"confidence":"high","reason":"x","evidence":[],"alternatives":[]}')
        with self._patch_gemini(text):
            out, err, model, usage = legacy_app.choose_final_website_with_gemini(
                "Acme Motors", "us", "", "", candidates, [], None, {})
        self.assertIsNone(out["official_website"])
        self.assertEqual(out["confidence_score"], 0)

    def test_score_clamped(self):
        """A score of 150 returned by the LLM must be clamped to 100."""
        candidates = ["https://acme.com"]
        text = ('{"official_website":"https://acme.com","confidence_score":150,'
                '"confidence":"high","reason":"match","evidence":[],"alternatives":[]}')
        with self._patch_gemini(text):
            out, err, model, usage = legacy_app.choose_final_website_with_gemini(
                "Acme", "us", "", "", candidates, [], None, {})
        self.assertIsNone(err)
        self.assertEqual(out["confidence_score"], 100)

    def test_non_json_handled(self):
        with mock.patch.object(gemini_llm, "_gemini_generate_content_json",
                               return_value=("not json", {}, None)):
            out, err, model, usage = legacy_app.choose_final_website_with_gemini(
                "Acme", "us", "", "", ["https://acme.com"], [], None, {})
        self.assertIsNone(out)
        self.assertIn("non-JSON", err)
