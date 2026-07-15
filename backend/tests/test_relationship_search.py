import unittest
from dataclasses import FrozenInstanceError

from app.services.serpwow.relationship_search import (
    RELATIONSHIP_PHASES,
    RelationshipPhase,
    RelationshipSearchInput,
    company_y_appears_on_x_site,
    extract_candidate_records,
    extract_evidence_records,
    has_positive_financial_evidence,
    normalize_search_policy,
    select_next_phase,
)


SEARCH_INPUT = RelationshipSearchInput(
    x_name="Acme Capital",
    y_name="Orbit Labs",
    input_url="https://acme.example/portfolio",
    x_domain="acme.example",
    city="Bengaluru",
    country="India",
)


class TestExtractCandidateRecords(unittest.TestCase):
    def test_structured_fields_are_canonicalized_in_provenance_order(self):
        raw_result = {
            "raw_response": {
                "knowledge_graph": {"website": "http://www.Alpha.io/"},
                "answer_box": {
                    "link": "https://alpha.io",
                    "url": "https://Beta.dev/",
                },
                "ai_overview": {
                    "ai_overview_sources": [
                        {"source_url": "https://www.Gamma.ai/product"},
                        {"source_url": "https://linkedin.com/company/gamma"},
                    ],
                },
                "organic_results": [
                    {
                        "link": "https://delta.co/",
                        "url": "https://epsilon.io/about",
                    },
                ],
            },
            "candidates": ["fallback.net", "https://beta.dev"],
        }

        self.assertEqual(
            extract_candidate_records(raw_result, "phase1", "owner.vc"),
            [
                {
                    "url": "https://alpha.io",
                    "original_text": "http://www.Alpha.io/",
                    "phase": "phase1",
                    "source_field": "knowledge_graph.website",
                    "evidence_id": "phase1.candidate.0",
                },
                {
                    "url": "https://beta.dev",
                    "original_text": "https://Beta.dev/",
                    "phase": "phase1",
                    "source_field": "answer_box.url",
                    "evidence_id": "phase1.candidate.1",
                },
                {
                    "url": "https://gamma.ai/product",
                    "original_text": "https://www.Gamma.ai/product",
                    "phase": "phase1",
                    "source_field": "ai_overview.ai_overview_sources[0].source_url",
                    "evidence_id": "phase1.candidate.2",
                },
                {
                    "url": "https://delta.co",
                    "original_text": "https://delta.co/",
                    "phase": "phase1",
                    "source_field": "organic_results[0].link",
                    "evidence_id": "phase1.candidate.3",
                },
                {
                    "url": "https://epsilon.io/about",
                    "original_text": "https://epsilon.io/about",
                    "phase": "phase1",
                    "source_field": "organic_results[0].url",
                    "evidence_id": "phase1.candidate.4",
                },
                {
                    "url": "https://fallback.net",
                    "original_text": "fallback.net",
                    "phase": "phase1",
                    "source_field": "candidates[0]",
                    "evidence_id": "phase1.candidate.5",
                },
            ],
        )

    def test_text_fields_extract_urls_and_bare_domains_but_not_email_domains(self):
        raw_result = {
            "raw_response": {
                "ai_overview": {
                    "ai_overview_contents": [{
                        "text": (
                            "Email team@modal.com or visit https://modal.com. "
                            "Its sibling is orbitlabs.ai; social: "
                            "https://linkedin.com/company/modal."
                        ),
                    }],
                },
                "organic_results": [{
                    "displayed_link": "www.displayed.dev › about",
                    "snippet": (
                        "More at snippet.co/path), owner.vc, and google.com."
                    ),
                }],
            },
        }

        records = extract_candidate_records(raw_result, "phase2", "owner.vc")

        self.assertEqual(
            [(record["url"], record["original_text"], record["source_field"])
             for record in records],
            [
                (
                    "https://modal.com",
                    "https://modal.com",
                    "ai_overview.ai_overview_contents[0].text",
                ),
                (
                    "https://orbitlabs.ai",
                    "orbitlabs.ai",
                    "ai_overview.ai_overview_contents[0].text",
                ),
                (
                    "https://displayed.dev",
                    "www.displayed.dev",
                    "organic_results[0].displayed_link",
                ),
                (
                    "https://snippet.co/path",
                    "snippet.co/path",
                    "organic_results[0].snippet",
                ),
            ],
        )

    def test_invalid_disallowed_and_company_x_structured_urls_are_rejected(self):
        raw_result = {
            "raw_response": {
                "knowledge_graph": {"website": "mailto:team@modal.com"},
                "answer_box": {
                    "link": "ftp://modal.com",
                    "url": "https://owner.vc/portfolio",
                },
                "organic_results": [
                    {"link": "https://x.com/modal"},
                    {"link": "https://files.example/report.pdf"},
                    {"link": "not a url"},
                    {"link": "https://not valid.example"},
                ],
            },
        }

        self.assertEqual(
            extract_candidate_records(raw_result, "phase3", "owner.vc"),
            [],
        )

    def test_structured_and_fallback_candidates_trim_trailing_punctuation(self):
        raw_result = {
            "raw_response": {
                "knowledge_graph": {"website": "https://Punctuated.dev)."},
            },
            "candidates": ["fallback.net,"],
        }

        records = extract_candidate_records(raw_result, "punctuation", "owner.vc")

        self.assertEqual(
            [(record["url"], record["original_text"]) for record in records],
            [
                ("https://punctuated.dev", "https://Punctuated.dev"),
                ("https://fallback.net", "fallback.net"),
            ],
        )

    def test_modal_f4_snippet_regression_keeps_typed_domain_provenance(self):
        raw_result = {
            "raw_response": {
                "organic_results": [{
                    "link": "https://eastlinkcap.com/portfolio/modal",
                    "snippet": (
                        "Portfolio company Modal announced a Series C. modal.com."
                    ),
                }],
            },
            "candidates": ["https://modal.com/"],
        }

        self.assertEqual(
            extract_candidate_records(raw_result, "f4", "eastlinkcap.com"),
            [{
                "url": "https://modal.com",
                "original_text": "modal.com",
                "phase": "f4",
                "source_field": "organic_results[0].snippet",
                "evidence_id": "f4.candidate.0",
            }],
        )


class TestExtractEvidenceRecords(unittest.TestCase):
    def test_collects_all_nonempty_overviews_and_only_financial_organic_snippets(self):
        raw_response = {
            "ai_overview": {
                "ai_overview_contents": [
                    {"text": "  Eastlink invested in Modal.  "},
                    {"text": "   "},
                    "not a content record",
                    {"text": "Modal and Eastlink announced a partnership."},
                ],
            },
            "organic_results": [
                {"snippet": "The companies announced a partnership."},
                {"snippet": "Modal secured funding led by Eastlink."},
                {"snippet": ""},
                {"snippet": "No investment relationship exists."},
            ],
        }

        self.assertEqual(
            extract_evidence_records(raw_response, "phase2"),
            [
                {
                    "evidence_id": "phase2.overview.0",
                    "phase": "phase2",
                    "source_field": "ai_overview.ai_overview_contents[0].text",
                    "text": "Eastlink invested in Modal.",
                },
                {
                    "evidence_id": "phase2.overview.3",
                    "phase": "phase2",
                    "source_field": "ai_overview.ai_overview_contents[3].text",
                    "text": "Modal and Eastlink announced a partnership.",
                },
                {
                    "evidence_id": "phase2.organic.1",
                    "phase": "phase2",
                    "source_field": "organic_results[1].snippet",
                    "text": "Modal secured funding led by Eastlink.",
                },
                {
                    "evidence_id": "phase2.organic.3",
                    "phase": "phase2",
                    "source_field": "organic_results[3].snippet",
                    "text": "No investment relationship exists.",
                },
            ],
        )

    def test_evidence_text_has_source_specific_bounds(self):
        raw_response = {
            "ai_overview": {
                "ai_overview_contents": [{"text": "invested " + "a" * 4000}],
            },
            "organic_results": [{"snippet": "funding " + "b" * 2000}],
        }

        records = extract_evidence_records(raw_response, "bounded")

        self.assertEqual(len(records[0]["text"]), 3000)
        self.assertEqual(len(records[1]["text"]), 1500)
        self.assertEqual(records[0]["evidence_id"], "bounded.overview.0")
        self.assertEqual(records[1]["evidence_id"], "bounded.organic.0")


class TestFinancialEvidenceClassifier(unittest.TestCase):
    def test_financial_marker_families_are_positive(self):
        positive_texts = [
            "Acme will invest in Modal.",
            "Acme is an investor in Modal.",
            "Acme made an investment in Modal.",
            "Acme invested in Modal.",
            "Acme created a fund for Modal.",
            "Acme funded Modal.",
            "Modal announced new funding from Acme.",
            "Acme will finance Modal.",
            "Acme financed Modal.",
            "Acme provided financing to Modal.",
            "Modal received financial backing from Acme.",
            "Modal is an Acme portfolio company.",
            "Modal is backed by Acme.",
            "Acme announced an acquisition of Modal.",
            "Modal was acquired by Acme.",
            "Acme is Modal's parent company.",
            "Modal is an Acme subsidiary.",
            "Acme disclosed ownership of Modal.",
        ]

        for text in positive_texts:
            with self.subTest(text=text):
                self.assertTrue(has_positive_financial_evidence([{"text": text}]))

    def test_explicit_negative_wording_overrides_positive_terms_in_same_record(self):
        negative_texts = [
            "There is no investment relationship; investment was only rumored.",
            "Acme did not invest in Modal, although an investment was discussed.",
            "Modal has funding without any funding relationship with Acme.",
            "There was no acquisition; acquisition speculation followed.",
        ]

        for text in negative_texts:
            with self.subTest(text=text):
                self.assertFalse(has_positive_financial_evidence([{"text": text}]))

    def test_partnership_and_co_mention_are_not_financial_evidence(self):
        records = [{
            "text": "Acme and Modal announced a strategic partnership together.",
        }]

        self.assertFalse(has_positive_financial_evidence(records))

    def test_positive_record_is_not_cancelled_by_a_separate_negative_record(self):
        records = [
            {"text": "One result says there was no investment relationship."},
            {"text": "Acme funded Modal's Series C."},
        ]

        self.assertTrue(has_positive_financial_evidence(records))


class TestCompanyYAppearsOnXSite(unittest.TestCase):
    def test_detects_x_domain_in_either_organic_url_field(self):
        for result in (
            {"link": "https://eastlinkcap.com/portfolio/modal"},
            {"url": "https://insights.eastlinkcap.com/modal"},
        ):
            with self.subTest(result=result):
                self.assertTrue(company_y_appears_on_x_site(
                    {"organic_results": [result]}, "eastlinkcap.com"))

    def test_ignores_non_x_and_nonorganic_urls(self):
        raw_response = {
            "knowledge_graph": {"website": "https://eastlinkcap.com"},
            "organic_results": [
                {"link": "https://modal.com"},
                {"url": "https://x.com/eastlinkcap"},
            ],
        }

        self.assertFalse(company_y_appears_on_x_site(
            raw_response, "eastlinkcap.com"))


class TestRelationshipPhaseRegistry(unittest.TestCase):
    def test_registry_has_ordered_names_and_goals(self):
        self.assertEqual(
            [(phase.name, phase.goal, phase.enabled) for phase in RELATIONSHIP_PHASES],
            [
                ("phase1_relationship_and_url", "combined", True),
                ("phase2_financial_evidence", "relationship_evidence", True),
                ("phase3_official_url_recovery", "official_url", True),
            ],
        )

    def test_phase_one_query_has_exact_intent_and_substitutions(self):
        self.assertEqual(
            RELATIONSHIP_PHASES[0].build_query(SEARCH_INPUT, ()),
            'Company X is "Acme Capital" and its official website is '
            '"https://acme.example/portfolio".\n\n'
            'What documented financial relationship exists between Company X and '
            'the company identified as "Orbit Labs"?\n\n'
            'Identify the exact Company Y and type its official website as one complete '
            'plain-text URL beginning with https://. Do not use hyperlink text. Do not '
            "return Company X's website. If no financial relationship is documented, "
            'say so explicitly.',
        )

    def test_phase_two_query_has_exact_intent_and_substitutions(self):
        self.assertEqual(
            RELATIONSHIP_PHASES[1].build_query(SEARCH_INPUT, ()),
            'What documented financial relationship—such as investment, funding-round '
            'participation, portfolio ownership, acquisition, or financial backing—exists '
            'between "Acme Capital" (official website: '
            '"https://acme.example/portfolio") and the company identified as '
            '"Orbit Labs"?\n\n'
            'Name the exact Company Y and describe the specific transaction, funding '
            'round, portfolio listing, acquisition, or investment evidence. Mere '
            'partnership or co-mention is not sufficient. If no financial relationship '
            'is documented, say so explicitly.',
        )

    def test_phase_three_query_joins_nonempty_evidence_with_exact_intent(self):
        evidence = [
            {"evidence_id": "ev-1", "text": "Acme led Orbit's Series A."},
            {"evidence_id": "ignored", "text": ""},
            {"evidence_id": "ev-2", "text": "Orbit appears in Acme's portfolio."},
        ]

        self.assertEqual(
            RELATIONSHIP_PHASES[2].build_query(SEARCH_INPUT, evidence),
            'The following search evidence describes a documented financial relationship '
            'between "Acme Capital" (official website: '
            '"https://acme.example/portfolio") and the company identified as '
            '"Orbit Labs":\n\n'
            '"[ev-1] Acme led Orbit\'s Series A.\n[ev-2] Orbit appears in Acme\'s '
            'portfolio."\n\n'
            'What is the official website of the exact Company Y described by this '
            'evidence?\n\n'
            'Return one complete plain-text URL beginning with https://. Do not use '
            "hyperlink text. Do not return Company X's website, a social profile, "
            'directory, database, news article, or search-result page.',
        )

    def test_phase_three_bounds_joined_evidence_to_6000_characters(self):
        query = RELATIONSHIP_PHASES[2].build_query(
            SEARCH_INPUT,
            [{"evidence_id": "ev", "text": "x" * 7000}],
        )
        evidence_text = query.split('\n\n"', 1)[1].split('"\n\n', 1)[0]

        self.assertEqual(len(evidence_text), 6000)
        self.assertTrue(evidence_text.startswith("[ev] "))

    def test_phase_three_omits_evidence_without_an_id(self):
        query = RELATIONSHIP_PHASES[2].build_query(
            SEARCH_INPUT,
            [
                {"text": "This missing-ID evidence must not appear."},
                {"evidence_id": "ev-valid", "text": "Valid evidence."},
            ],
        )

        self.assertNotIn("missing-ID evidence", query)
        self.assertIn("[ev-valid] Valid evidence.", query)

    def test_models_are_frozen(self):
        with self.assertRaises(FrozenInstanceError):
            SEARCH_INPUT.x_name = "Changed"
        with self.assertRaises(FrozenInstanceError):
            RELATIONSHIP_PHASES[0].enabled = False


class TestSelectNextPhase(unittest.TestCase):
    def test_adaptive_runs_first_phase_first(self):
        selected = select_next_phase(
            RELATIONSHIP_PHASES, set(), "adaptive", False, False)
        self.assertEqual(selected, RELATIONSHIP_PHASES[0])

    def test_adaptive_targets_relationship_evidence_when_missing(self):
        for valid_url_found in (False, True):
            with self.subTest(valid_url_found=valid_url_found):
                selected = select_next_phase(
                    RELATIONSHIP_PHASES,
                    {"phase1_relationship_and_url"},
                    "adaptive",
                    False,
                    valid_url_found,
                )
                self.assertEqual(selected, RELATIONSHIP_PHASES[1])

    def test_adaptive_targets_official_url_when_relationship_is_evidenced(self):
        selected = select_next_phase(
            RELATIONSHIP_PHASES,
            {"phase1_relationship_and_url"},
            "adaptive",
            True,
            False,
        )
        self.assertEqual(selected, RELATIONSHIP_PHASES[2])

    def test_adaptive_stops_when_complete_or_target_is_exhausted(self):
        self.assertIsNone(select_next_phase(
            RELATIONSHIP_PHASES,
            {"phase1_relationship_and_url"},
            "adaptive",
            True,
            True,
        ))
        self.assertIsNone(select_next_phase(
            RELATIONSHIP_PHASES,
            {"phase1_relationship_and_url", "phase2_financial_evidence"},
            "adaptive",
            False,
            False,
        ))

    def test_sequential_runs_each_enabled_phase_in_registry_order(self):
        executed = set()
        selected_names = []
        while phase := select_next_phase(
                RELATIONSHIP_PHASES, executed, "sequential", False, False):
            selected_names.append(phase.name)
            executed.add(phase.name)

        self.assertEqual(
            selected_names,
            [phase.name for phase in RELATIONSHIP_PHASES],
        )

    def test_sequential_stops_when_search_is_complete(self):
        self.assertIsNone(select_next_phase(
            RELATIONSHIP_PHASES, set(), "sequential", True, True))

    def test_selector_uses_supplied_shortened_and_reordered_registry(self):
        build_query = lambda search_input, evidence: "query"
        registry = (
            RelationshipPhase("recover-first", "official_url", build_query),
            RelationshipPhase("disabled", "combined", build_query, enabled=False),
            RelationshipPhase("prove-second", "relationship_evidence", build_query),
        )

        self.assertEqual(
            select_next_phase(registry, set(), "adaptive", False, False).name,
            "recover-first",
        )
        self.assertEqual(
            select_next_phase(
                registry, {"recover-first"}, "adaptive", False, False).name,
            "prove-second",
        )
        self.assertIsNone(select_next_phase(
            registry, {"recover-first"}, "adaptive", True, False))
        self.assertEqual(
            select_next_phase(registry, set(), "sequential", False, False).name,
            "recover-first",
        )
        self.assertEqual(
            select_next_phase(
                registry, {"recover-first"}, "sequential", False, False).name,
            "prove-second",
        )

    def test_adaptive_does_not_restart_first_phase_after_another_phase_ran(self):
        build_query = lambda search_input, evidence: "query"
        registry = (
            RelationshipPhase("combined-first", "combined", build_query),
            RelationshipPhase("url-already-run", "official_url", build_query),
            RelationshipPhase(
                "evidence-target", "relationship_evidence", build_query),
        )

        selected = select_next_phase(
            registry, {"url-already-run"}, "adaptive", False, False)

        self.assertEqual(selected.name, "evidence-target")

    def test_adaptive_does_not_restart_first_enabled_phase(self):
        build_query = lambda search_input, evidence: "query"
        registry = (
            RelationshipPhase(
                "disabled-first", "combined", build_query, enabled=False),
            RelationshipPhase("combined-first-enabled", "combined", build_query),
            RelationshipPhase("url-already-run", "official_url", build_query),
            RelationshipPhase(
                "evidence-target", "relationship_evidence", build_query),
        )

        selected = select_next_phase(
            registry, {"url-already-run"}, "adaptive", False, False)

        self.assertEqual(selected.name, "evidence-target")

    def test_adaptive_uses_remaining_combined_phase_for_missing_relationship(self):
        build_query = lambda search_input, evidence: "query"
        registry = (
            RelationshipPhase("combined-already-run", "combined", build_query),
            RelationshipPhase("combined-fallback", "combined", build_query),
        )

        selected = select_next_phase(
            registry, {"combined-already-run"}, "adaptive", False, False)

        self.assertEqual(selected, registry[1])

    def test_adaptive_uses_remaining_combined_phase_for_missing_official_url(self):
        build_query = lambda search_input, evidence: "query"
        registry = (
            RelationshipPhase("combined-already-run", "combined", build_query),
            RelationshipPhase("combined-fallback", "combined", build_query),
        )

        selected = select_next_phase(
            registry, {"combined-already-run"}, "adaptive", True, False)

        self.assertEqual(selected, registry[1])


class TestNormalizeSearchPolicy(unittest.TestCase):
    def test_only_case_insensitive_sequential_maps_to_sequential(self):
        self.assertEqual(normalize_search_policy("sequential"), "sequential")
        self.assertEqual(normalize_search_policy("SeQuEnTiAl"), "sequential")
        self.assertEqual(normalize_search_policy(" sequential "), "sequential")
        for value in ("adaptive", "", None, 123):
            with self.subTest(value=value):
                self.assertEqual(normalize_search_policy(value), "adaptive")


if __name__ == "__main__":
    unittest.main()
