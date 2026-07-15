import unittest
from dataclasses import FrozenInstanceError

from app.services.serpwow.relationship_search import (
    RELATIONSHIP_PHASES,
    RelationshipPhase,
    RelationshipSearchInput,
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


class TestNormalizeSearchPolicy(unittest.TestCase):
    def test_only_case_insensitive_sequential_maps_to_sequential(self):
        self.assertEqual(normalize_search_policy("sequential"), "sequential")
        self.assertEqual(normalize_search_policy("SeQuEnTiAl"), "sequential")
        for value in ("adaptive", "", " sequential ", None, 123):
            with self.subTest(value=value):
                self.assertEqual(normalize_search_policy(value), "adaptive")


if __name__ == "__main__":
    unittest.main()
