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

    def test_email_subdomains_do_not_leak_suffix_domain_candidates(self):
        raw_result = {
            "raw_response": {
                "ai_overview": {
                    "ai_overview_contents": [
                        {"text": "Contact team@sub.modal.com for details."},
                        {"text": "Write to team@deep.sub.orbit.ai instead."},
                    ],
                },
            },
        }

        self.assertEqual(
            extract_candidate_records(raw_result, "email", "owner.vc"),
            [],
        )

    def test_explicit_domain_after_subdomain_email_is_still_extracted(self):
        raw_result = {
            "raw_response": {
                "ai_overview": {
                    "ai_overview_contents": [{
                        "text": "Email team@sub.modal.com or visit modal.com.",
                    }],
                },
            },
        }

        records = extract_candidate_records(raw_result, "same-text", "owner.vc")

        self.assertEqual(
            [(record["url"], record["original_text"]) for record in records],
            [("https://modal.com", "modal.com")],
        )

    def test_explicit_domain_in_different_text_survives_email_filter(self):
        raw_result = {
            "raw_response": {
                "ai_overview": {
                    "ai_overview_contents": [{
                        "text": "Email team@deep.sub.modal.com.",
                    }],
                },
                "organic_results": [{"displayed_link": "modal.com"}],
            },
        }

        records = extract_candidate_records(
            raw_result, "different-text", "owner.vc")

        self.assertEqual(
            [(record["url"], record["source_field"]) for record in records],
            [("https://modal.com", "organic_results[0].displayed_link")],
        )

    def test_dotted_email_local_part_is_masked_before_domain_scanning(self):
        raw_result = {
            "raw_response": {
                "ai_overview": {
                    "ai_overview_contents": [{
                        "text": (
                            "Email first.dev@deep.sub.modal.com, then visit "
                            "orbit.ai."
                        ),
                    }],
                },
            },
        }

        records = extract_candidate_records(
            raw_result, "dotted-email", "owner.vc")

        self.assertEqual(
            [(record["url"], record["original_text"]) for record in records],
            [("https://orbit.ai", "orbit.ai")],
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

    def test_bare_filenames_are_not_domain_candidates(self):
        raw_result = {
            "raw_response": {
                "ai_overview": {
                    "ai_overview_contents": [{
                        "text": (
                            "Files: requirements.txt, config.py, and setup.sh. "
                            "Website: modal.com."
                        ),
                    }],
                },
            },
        }

        records = extract_candidate_records(raw_result, "files", "owner.vc")

        self.assertEqual(
            [(record["url"], record["original_text"]) for record in records],
            [("https://modal.com", "modal.com")],
        )

    def test_fixture_like_filenames_are_rejected_from_every_candidate_source(self):
        raw_result = {
            "raw_response": {
                "knowledge_graph": {"website": "requirements.txt"},
                "answer_box": {"link": "https://config.py"},
                "organic_results": [{"link": "https://setup.sh"}],
            },
            "candidates": ["requirements.txt", "https://company.sh"],
        }

        records = extract_candidate_records(raw_result, "files", "owner.vc")

        self.assertEqual(
            [(record["url"], record["source_field"]) for record in records],
            [("https://company.sh", "candidates[1]")],
        )

    def test_ip_literal_candidates_are_rejected(self):
        raw_result = {
            "raw_response": {
                "knowledge_graph": {"website": "https://8.8.8.8"},
                "answer_box": {"link": "https://192.168.1.2"},
                "organic_results": [
                    {"link": "https://[2001:4860:4860::8888]"},
                    {"link": "https://[::1]"},
                ],
            },
            "candidates": ["1.1.1.1", "https://10.0.0.1", "http://127.1"],
        }

        self.assertEqual(
            extract_candidate_records(raw_result, "ips", "owner.vc"),
            [],
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

    def test_non_string_provider_text_is_ignored(self):
        raw_response = {
            "ai_overview": {
                "ai_overview_contents": [
                    {"text": {"value": "Acme invested in Modal."}},
                    {"text": ["Acme funded Modal."]},
                    {"text": "Acme invested in Modal."},
                ],
            },
            "organic_results": [
                {"snippet": {"value": "funding from Acme"}},
                {"snippet": ["investment by Acme"]},
                {"snippet": "Modal received funding from Acme."},
            ],
        }

        self.assertEqual(
            extract_evidence_records(raw_response, "types"),
            [
                {
                    "evidence_id": "types.overview.2",
                    "phase": "types",
                    "source_field": "ai_overview.ai_overview_contents[2].text",
                    "text": "Acme invested in Modal.",
                },
                {
                    "evidence_id": "types.organic.2",
                    "phase": "types",
                    "source_field": "organic_results[2].snippet",
                    "text": "Modal received funding from Acme.",
                },
            ],
        )


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
            "Acme finances Modal.",
            "Acme provided financing to Modal.",
            "Modal received financial backing from Acme.",
            "Modal is an Acme portfolio company.",
            "Modal is in Eastlink's portfolio.",
            "Modal is backed by Acme.",
            "Eastlink provides backing to Modal.",
            "Eastlink financially backs Modal.",
            "Acme announced an acquisition of Modal.",
            "Modal was acquired by Acme.",
            "Acme is Modal's parent company.",
            "Modal is an Acme subsidiary.",
            "Acme disclosed ownership of Modal.",
            "Acme led Modal's Series C.",
            "Acme participated in Modal's Series C.",
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
            "There is no evidence of investment by Acme in Modal.",
            "Modal is not backed by Acme; backed claims were only rumors.",
            "Modal is not a subsidiary of Acme; subsidiary claims were denied.",
            "Acme is not the parent company; parent company reports were wrong.",
            "Acme is not Modal's parent company; parent company claims were wrong.",
            "Acme never invested in Modal; investment reports were wrong.",
            "Acme didn't invest in Modal; investment reports were wrong.",
            "Modal wasn't acquired by Acme; acquisition reports were wrong.",
            "Modal isn't backed by Acme; backing reports were wrong.",
            "Acme invested in Modal but cannot disclose the terms.",
            "Acme invested in Modal but can't discuss the terms.",
            "Acme invested in Modal but couldn't disclose the terms.",
            "Acme invested in Modal but won't disclose the terms.",
            "Acme invested in Modal but wouldn't disclose the terms.",
            "Acme invested in Modal but doesn't discuss the terms.",
            "Acme invested in Modal but don't quote the announcement.",
        ]

        for text in negative_texts:
            with self.subTest(text=text):
                self.assertFalse(has_positive_financial_evidence([{"text": text}]))

    def test_partnership_and_co_mention_are_not_financial_evidence(self):
        nonfinancial_texts = [
            "Acme and Modal announced a strategic partnership together.",
            "The partnership is with Modal, an investment platform.",
            "The minister backed legislation during the debate.",
            "The employee acquired skills during training.",
        ]

        for text in nonfinancial_texts:
            with self.subTest(text=text):
                self.assertFalse(has_positive_financial_evidence([{"text": text}]))

    def test_supplied_company_context_requires_both_names_in_positive_record(self):
        self.assertFalse(has_positive_financial_evidence(
            [{"text": "Acme invested in Other."}],
            x_name="Acme",
            y_name="Modal",
        ))
        self.assertFalse(has_positive_financial_evidence(
            [{"text": "Acme invested in Modal."}],
            x_name="",
            y_name="Modal",
        ))
        self.assertTrue(has_positive_financial_evidence(
            [{"text": "Acme Capital invested in Modal Labs."}],
            x_name="Acme Capital",
            y_name="Modal Labs",
        ))

    def test_grounded_classifier_rejects_unrelated_same_record_constructions(self):
        false_texts = [
            "Acme invested in OtherCo, while merely partnering with Modal.",
            "Acme and Modal met after the minister backed legislation.",
            "Acme hired an employee who acquired skills at Modal.",
        ]

        for text in false_texts:
            with self.subTest(text=text):
                self.assertFalse(has_positive_financial_evidence(
                    [{"text": text}], x_name="Acme", y_name="Modal"))

    def test_grounded_classifier_accepts_narrow_active_and_passive_pairs(self):
        positive_texts = [
            "Acme invested in Modal.",
            "Acme invests in Modal.",
            "Acme is an investor in Modal.",
            "Modal was funded by Acme.",
            "Modal was financed by Acme.",
            "Modal is backed by Acme.",
            "Acme funded Modal.",
            "Acme financed Modal.",
            "Acme backed Modal.",
            "Acme acquired Modal.",
            "Modal was acquired by Acme.",
            "Acme led Modal's Series C.",
            "Acme participated in Modal's Series C.",
            "Modal's Series C was led by Acme.",
            "Modal's Series C closed with participation by Acme.",
            "Modal is a portfolio company of Acme.",
            "Acme's portfolio includes Modal.",
            "Acme is the parent company of Modal.",
            "Modal is a subsidiary of Acme.",
            "Acme is Modal's parent company.",
            "Modal is Acme's subsidiary.",
        ]

        for text in positive_texts:
            with self.subTest(text=text):
                self.assertTrue(has_positive_financial_evidence(
                    [{"text": text}], x_name="Acme", y_name="Modal"))

    def test_grounded_classifier_uses_exact_casefolded_unicode_names(self):
        cases = [
            ("甲 invested in 乙.", "甲", "乙"),
            ("モーダル was funded by アクメ.", "アクメ", "モーダル"),
            ("STRASSE invested in MODAL.", "Straße", "Modal"),
        ]

        for text, x_name, y_name in cases:
            with self.subTest(text=text):
                self.assertTrue(has_positive_financial_evidence(
                    [{"text": text}], x_name=x_name, y_name=y_name))

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
