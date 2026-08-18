"""The single relationship AI Mode search query."""
import unittest

from app.services.serpwow.query_builders import (
    build_relationship_search_query,
    load_relationship_prompt,
)


class PromptLoadingTests(unittest.TestCase):
    def test_prompt_file_loads_and_is_url_safe_length(self) -> None:
        prompt = load_relationship_prompt()
        self.assertIn("{y_name}", prompt)
        self.assertIn("{x_name}", prompt)
        # It ships as a q= URL parameter. ai_bulk_search.txt is ~3.5k and works.
        self.assertLess(len(prompt), 4000)

    def test_prompt_is_cached_not_reread(self) -> None:
        self.assertIs(load_relationship_prompt(), load_relationship_prompt())

    def test_prompt_uses_only_the_four_supported_placeholders(self) -> None:
        """The prompt file is operator-editable and goes through .format(), so an
        unsupported placeholder (e.g. the removed {city}) raises KeyError on EVERY row
        of a run. Fail here instead."""
        import re

        used = set(re.findall(r"\{(\w+)\}", load_relationship_prompt()))
        self.assertEqual(
            used - {"x_name", "y_name", "x_domain", "input_url"}, set())


class QueryBuildingTests(unittest.TestCase):
    def test_every_placeholder_is_substituted(self) -> None:
        q = build_relationship_search_query(
            x_name="Acme Capital", y_name="SANZO POMELO", x_domain="acme.com",
            input_url="https://acme.com/portfolio")
        self.assertIn("Acme Capital", q)
        self.assertIn("SANZO POMELO", q)
        self.assertIn("acme.com", q)
        self.assertIn("https://acme.com/portfolio", q)
        # No unsubstituted braces left behind.
        self.assertNotIn("{", q)

    def test_blank_optional_fields_do_not_leave_none_in_the_query(self) -> None:
        q = build_relationship_search_query(
            x_name="Acme", y_name="Y Co", x_domain="", input_url="")
        self.assertNotIn("None", q)
        self.assertNotIn("{", q)

    def test_y_name_is_passed_verbatim_including_ocr_noise(self) -> None:
        # OCR noise is meaningful input, never sanitised — see HANDOFF 2026-07-23.
        noisy = "ERROR: 503 YUZU SPARKLINGWE SANZO"
        q = build_relationship_search_query(
            x_name="Acme", y_name=noisy, x_domain="acme.com",
            input_url="https://acme.com/p")
        self.assertIn(noisy, q)


if __name__ == "__main__":
    unittest.main()
