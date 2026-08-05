"""AI Mode payload -> evidence + candidate set for the unchanged relationship gate."""
import unittest

from app.services.serpwow.modes.relationship import (
    build_evidence,
    extract_https_urls,
    flatten_text_blocks,
    row_fields,
)


class RowFieldsTests(unittest.TestCase):
    def test_headers_are_matched_case_and_alias_insensitively(self) -> None:
        fields = row_fields({"Input_URL": "https://acme.com/p", "COMPANY_X": "Acme",
                             "company_name_y": " Sanzo ", "Country": "US",
                             "row_index": 4})
        self.assertEqual(fields["x_name"], "Acme")
        self.assertEqual(fields["y_name"], "Sanzo")
        self.assertEqual(fields["input_url"], "https://acme.com/p")
        self.assertEqual(fields["row_index"], 4)
        # A Country column in the CSV is ignored: this pipeline has no location input.
        self.assertNotIn("country", fields)

    def test_absent_columns_become_empty_strings(self) -> None:
        fields = row_fields({"Company_Name_Y": "Y", "row_index": 0})
        self.assertEqual(fields["input_url"], "")
        self.assertEqual(fields["x_name"], "")

# Current shape: scrape.do's body sits VERBATIM under "response" (raw/ is its only
# copy). Every EvidenceTests case below therefore exercises the real stored shape.
PAYLOAD = {
    "text_blocks": [
        {"type": "paragraph", "snippet": "Acme Capital led Sanzo's Series A."},
        {"type": "paragraph",
         "snippet": "Sanzo's official website is https://drinksanzo.com and its "
                    "LinkedIn is https://linkedin.com/company/sanzo"},
    ],
    "references": [
        {"title": "Sanzo", "link": "https://drinksanzo.com", "source": "drinksanzo.com"},
        {"title": "Acme portfolio", "link": "https://acme.com/portfolio",
         "source": "acme.com"},
        {"title": "Crunchbase", "link": "https://crunchbase.com/org/sanzo",
         "source": "crunchbase.com"},
    ],
}
ENVELOPE = {"query": "prompt", "response": PAYLOAD, "error": None, "billed_empty": False}


class UrlExtractionTests(unittest.TestCase):
    def test_typed_urls_are_pulled_out_of_prose(self) -> None:
        urls = extract_https_urls("see https://a.com, and https://b.com.")
        self.assertEqual(urls, ["https://a.com", "https://b.com"])

    def test_trailing_punctuation_is_stripped_and_dupes_dropped(self) -> None:
        urls = extract_https_urls("https://a.com) https://a.com!")
        self.assertEqual(urls, ["https://a.com"])


class EvidenceTests(unittest.TestCase):
    def test_envelopes_written_before_the_response_key_still_read(self) -> None:
        """Runs scraped before the raw artifact became a verbatim copy inlined the two
        arrays at the top level. Those objects are still on S3 and must keep resolving."""
        legacy = {"query": "prompt", "error": None, **PAYLOAD}
        self.assertEqual(build_evidence(legacy, x_domain="acme.com")["candidates"],
                         build_evidence(ENVELOPE, x_domain="acme.com")["candidates"])

    def test_references_and_typed_urls_both_become_candidates(self) -> None:
        ev = build_evidence(ENVELOPE, x_domain="acme.com")
        self.assertIn("https://drinksanzo.com", ev["candidates"])

    def test_company_x_domain_is_never_a_candidate(self) -> None:
        ev = build_evidence(ENVELOPE, x_domain="acme.com")
        self.assertNotIn("https://acme.com/portfolio", ev["candidates"])

    def test_social_and_directory_urls_are_filtered(self) -> None:
        ev = build_evidence(ENVELOPE, x_domain="acme.com")
        joined = " ".join(ev["candidates"])
        self.assertNotIn("linkedin.com", joined)
        self.assertNotIn("crunchbase.com", joined)

    def test_text_blocks_are_joined_into_one_evidence_block(self) -> None:
        ev = build_evidence(ENVELOPE, x_domain="acme.com")
        self.assertEqual(len(ev["ai_overview_evidence"]), 1)
        block = ev["ai_overview_evidence"][0]
        self.assertIn("Series A", block["text"])
        self.assertTrue(any(s["url"] == "https://drinksanzo.com"
                            for s in block["sources"]))

    def test_one_search_attempt_is_recorded_for_the_single_call(self) -> None:
        ev = build_evidence(ENVELOPE, x_domain="acme.com")
        self.assertEqual(len(ev["search_attempts"]), 1)
        self.assertEqual(ev["search_attempts"][0]["status"], "candidates_found")

    def test_empty_200_yields_no_evidence_and_no_candidates(self) -> None:
        ev = build_evidence(
            {"query": "q", "text_blocks": [], "references": [], "error": None,
             "billed_empty": True},
            x_domain="acme.com")
        self.assertEqual(ev["candidates"], [])
        self.assertEqual(ev["ai_overview_evidence"], [])
        self.assertEqual(ev["search_attempts"][0]["status"], "no_candidates")

    def test_provider_error_is_recorded_on_the_attempt(self) -> None:
        ev = build_evidence(
            {"query": "q", "text_blocks": [], "references": [],
             "error": "HTTP 529", "error_category": "rate_limit", "billed_empty": False},
            x_domain="acme.com")
        self.assertEqual(ev["search_attempts"][0]["status"], "error")
        self.assertEqual(ev["search_attempts"][0]["error"], "HTTP 529")


STRUCTURED = {
    "text_blocks": [
        {"type": "heading", "snippet": "1. RELATIONSHIP", "level": 3},
        {"type": "paragraph", "snippet": "A FINANCIAL relationship is [CONFIRMED]."},
        {"type": "heading", "snippet": "2. EVIDENCE", "level": 3},
        {"type": "list", "list": [
            {"snippet": "From Founder Lodge: \"Pitchly, March 14 2023, Series A, "
                        "$7,000,000\" backed by Great North Ventures."},
            {"snippet": "From Parsers VC: ... associated with the domain pitchly.com."},
        ]},
        {"type": "heading", "snippet": "3. WEBSITE", "level": 3},
        {"type": "paragraph", "snippet": "See https://www.pitchly.com for details."},
    ],
    "references": [],
}


class FlattenTextBlocksTests(unittest.TestCase):
    """The EVIDENCE bullets — dates, amounts, round names, source attributions — live in
    `list` blocks, which have no `snippet`. Reading only `snippet` dropped every one of
    them, so the verdict LLM was asked to confirm a financial relationship with the
    citations proving it removed."""

    def test_list_items_reach_the_evidence_text(self) -> None:
        text = build_evidence({"response": STRUCTURED}, "gnv.com")[
            "ai_overview_evidence"][0]["text"]
        self.assertIn("$7,000,000", text)
        self.assertIn("Founder Lodge", text)
        self.assertIn("Parsers VC", text)

    def test_the_answers_own_sections_survive(self) -> None:
        text = build_evidence({"response": STRUCTURED}, "gnv.com")[
            "ai_overview_evidence"][0]["text"]
        for heading in ("1. RELATIONSHIP", "2. EVIDENCE", "3. WEBSITE"):
            self.assertIn(heading, text)
        # Order preserved, so the model reads the sections as written.
        self.assertLess(text.index("1. RELATIONSHIP"), text.index("2. EVIDENCE"))
        self.assertLess(text.index("2. EVIDENCE"), text.index("3. WEBSITE"))

    def test_a_url_typed_inside_the_answer_is_still_a_candidate(self) -> None:
        ev = build_evidence({"response": STRUCTURED}, "gnv.com")
        self.assertIn("https://www.pitchly.com", ev["candidates"])

    def test_a_nested_list_is_not_dropped(self) -> None:
        blocks = [{"type": "list", "list": [
            {"snippet": "outer", "list": [{"snippet": "inner detail"}]}]}]
        self.assertIn("inner detail", flatten_text_blocks(blocks))

    def test_junk_blocks_are_skipped_not_crashed_on(self) -> None:
        self.assertEqual(flatten_text_blocks([None, 7, {}, {"type": "list"}]), "")


if __name__ == "__main__":
    unittest.main()
