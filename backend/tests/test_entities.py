import unittest
from app.models.entities import Entity, InvalidCSVError, parse_entities_csv

class TestParseEntitiesCSV(unittest.TestCase):
    def test_canonical_headers(self):
        csv = "company_name,country\nAcme KK,Japan\n"
        parsed = parse_entities_csv(csv)
        self.assertEqual(len(parsed.entities), 1)
        e = parsed.entities[0]
        self.assertEqual((e.company_name, e.country, e.sno), ("Acme KK", "Japan", 1))
        self.assertIsNone(e.company_local_name)

    def test_aliases_and_optionals(self):
        csv = ("Company,Nation,Local Name,Address,FirmID,Industry\n"
               "Acme,Japan,アクメ,Tokyo 1-2-3,F1,Manufacturing\n")
        e = parse_entities_csv(csv).entities[0]
        self.assertEqual(e.company_local_name, "アクメ")
        self.assertEqual(e.address, "Tokyo 1-2-3")
        self.assertEqual(e.firm_id, "F1")
        self.assertEqual(e.industry, "Manufacturing")

    def test_old_company_mode_format_rejected(self):
        csv = "Company Name ENG,Company Name Local,Country Code,ISIC\nAcme,アクメ,JP,2200\n"
        with self.assertRaises(InvalidCSVError) as ctx:
            parse_entities_csv(csv)
        self.assertIn("company_name", str(ctx.exception))

    def test_positional_fallback_when_headerless(self):
        csv = "Acme KK,Japan\nBeta GmbH,Germany\n"
        parsed = parse_entities_csv(csv)
        self.assertEqual(len(parsed.entities), 2)
        self.assertTrue(any("positional" in w for w in parsed.warnings))

    def test_empty_rows_skipped_and_zero_rows_rejected(self):
        with self.assertRaises(InvalidCSVError):
            parse_entities_csv("company_name,country\n,\n")

    def test_headerish_row_without_required_columns_rejected_not_positional(self):
        # "Entity Name" normalizes to entity_name (a company alias) but
        # "Country Code" is not a country alias -> header row detected,
        # required country column missing -> reject, do NOT parse positionally.
        csv = "Entity Name,Country Code\nAcme,JP\n"
        with self.assertRaises(InvalidCSVError):
            parse_entities_csv(csv)

    def test_bytes_input_with_bom(self):
        raw = "﻿company_name,country\nAcme KK,Japan\n".encode("utf-8")
        parsed = parse_entities_csv(raw)
        self.assertEqual(parsed.entities[0].company_name, "Acme KK")
        self.assertFalse(parsed.positional)

    def test_positional_sno_and_missing_country(self):
        csv = "Acme KK,Japan\nSoloCo\n"
        parsed = parse_entities_csv(csv)
        self.assertEqual([(e.sno, e.company_name, e.country) for e in parsed.entities],
                         [(1, "Acme KK", "Japan"), (2, "SoloCo", "")])

    def test_columns_detected_mapping(self):
        csv = "Company,Nation\nAcme,Japan\n"
        parsed = parse_entities_csv(csv)
        self.assertEqual(parsed.columns_detected["company_name"], "Company")
        self.assertEqual(parsed.columns_detected["country"], "Nation")

class TestFormatEntitiesForPrompt(unittest.TestCase):
    def test_minimal_fields(self):
        from app.models.entities import Entity, format_entities_for_prompt
        block = format_entities_for_prompt([Entity("Acme KK", "Japan", 1)])
        self.assertEqual(block, "1. Acme KK — Japan")

    def test_all_optional_fields_appended_only_when_present(self):
        from app.models.entities import Entity, format_entities_for_prompt
        e = Entity("Acme KK", "Japan", 1, company_local_name="アクメ株式会社",
                   address="1-2-3 Shibuya, Tokyo", industry="manufacturing")
        block = format_entities_for_prompt([e])
        self.assertEqual(block, "1. Acme KK (local: アクメ株式会社) — Japan — "
                                "1-2-3 Shibuya, Tokyo — industry: manufacturing")

if __name__ == "__main__":
    unittest.main()
