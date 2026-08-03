import unittest

from app.services.serpwow.relationship_csv import (
    InvalidRelationshipCSV,
    parse_relationship_csv,
)

CSV_OK = (
    "Input_URL,Company_Name_X,Box_No,Image_URL,Company_Name_Y,OCR_Status\n"
    "https://www.eastlinkcap.com/portfolio/,eastlinkcap,2.0,img2,Modal,SUCCESS\n"
    "https://www.eastlinkcap.com/portfolio/,eastlinkcap,3.0,img3,  Modal ,SUCCESS\n"
    "https://other.vc/p,othervc,4.0,img4,Modal,SUCCESS\n"
    "https://www.x.com/p,xvc,5.0,img5,Sanzo,SUCCESS\n"
).encode("utf-8")


class TestParseRelationshipCSV(unittest.TestCase):
    def test_every_row_becomes_its_own_pair_no_dedup(self):
        parsed = parse_relationship_csv(CSV_OK)
        self.assertEqual(parsed["header"][0], "Input_URL")
        self.assertEqual(len(parsed["original_rows"]), 4)
        self.assertEqual(parsed["blank_row_indices"], [])
        # No (X, Y) dedup: 4 rows in → 4 pairs out, each with its own single index.
        self.assertEqual(len(parsed["pairs"]), 4)
        for i, p in enumerate(parsed["pairs"]):
            self.assertEqual(p["source_row_indices"], [i])
            self.assertEqual(p["pair_index"], i + 1)
        # Per-row whitespace is trimmed (row 1's Y was "  Modal ").
        self.assertEqual(parsed["pairs"][1]["y_name"], "Modal")
        self.assertEqual(parsed["pairs"][0]["input_url"],
                         "https://www.eastlinkcap.com/portfolio/")

    def test_optional_city_country_columns(self):
        raw = (
            "Input_URL,Company_Name_X,Company_Name_Y,City,Country\n"
            "https://m25vc.com/portfolio,m25vc,Sanzo,New York,United States\n"
        ).encode()
        parsed = parse_relationship_csv(raw)
        self.assertEqual(parsed["pairs"][0]["city"], "New York")
        self.assertEqual(parsed["pairs"][0]["country"], "United States")

    def test_blank_required_values_report_csv_row_number(self):
        raw = (
            "Input_URL,Company_Name_X,Company_Name_Y\n"
            "https://m25vc.com/portfolio,m25vc,Sanzo\n"
            ",,\n"
        ).encode()
        with self.assertRaises(InvalidRelationshipCSV) as ctx:
            parse_relationship_csv(raw)
        message = str(ctx.exception)
        self.assertIn("row 3", message)
        self.assertIn("Input_URL", message)
        self.assertIn("Company_Name_X", message)
        self.assertIn("Company_Name_Y", message)

    def test_missing_y_column_raises(self):
        raw = "Company_Name_X,Whatever\nm25vc,zzz\n".encode()
        with self.assertRaises(InvalidRelationshipCSV):
            parse_relationship_csv(raw)

    def test_missing_x_column_raises(self):
        raw = "Input_URL,Company_Name_Y,Whatever\nhttps://m25vc.com/p,Sanzo,zzz\n".encode()
        with self.assertRaises(InvalidRelationshipCSV) as ctx:
            parse_relationship_csv(raw)
        self.assertIn("Company_Name_X", str(ctx.exception))

    def test_missing_input_url_column_raises(self):
        raw = "Company_Name_X,Company_Name_Y\nm25vc,Sanzo\n".encode()
        with self.assertRaises(InvalidRelationshipCSV) as ctx:
            parse_relationship_csv(raw)
        self.assertIn("Input_URL", str(ctx.exception))

    def test_empty_file_raises(self):
        with self.assertRaises(InvalidRelationshipCSV):
            parse_relationship_csv(b"")

    def test_bom_tolerated(self):
        raw = (
            "﻿Input_URL,Company_Name_X,Company_Name_Y\n"
            "https://m25vc.com/p,m25vc,Sanzo\n"
        ).encode("utf-8")
        parsed = parse_relationship_csv(raw)
        self.assertEqual(parsed["pairs"][0]["y_name"], "Sanzo")

    def test_preserves_error_marker_text_as_company_y(self):
        raw = (
            "Input_URL,Company_Name_X,Company_Name_Y\n"
            "https://a.example,a,FETCH_ERROR: 403 Forbidden\n"
            "https://b.example,b,error: 503 unavailable\n"
        ).encode()
        parsed = parse_relationship_csv(raw)
        self.assertEqual(
            [pair["y_name"] for pair in parsed["pairs"]],
            ["FETCH_ERROR: 403 Forbidden", "error: 503 unavailable"],
        )

    def test_allows_error_word_when_it_is_not_a_marker_prefix(self):
        raw = (
            "Input_URL,Company_Name_X,Company_Name_Y\n"
            "https://a.example,a,Error Coffee Company\n"
        ).encode()
        self.assertEqual(
            parse_relationship_csv(raw)["pairs"][0]["y_name"],
            "Error Coffee Company",
        )


if __name__ == "__main__":
    unittest.main()
