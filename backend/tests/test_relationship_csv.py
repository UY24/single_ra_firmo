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
    def test_every_row_becomes_its_own_row_no_dedup(self):
        parsed = parse_relationship_csv(CSV_OK)
        self.assertEqual(parsed["header"][0], "Input_URL")
        self.assertEqual(parsed["total_rows"], 4)
        # No (X, Y) dedup: 4 rows in -> 4 rows out, each with its own index.
        self.assertEqual(len(parsed["rows"]), 4)
        for i, r in enumerate(parsed["rows"]):
            self.assertEqual(r["row_index"], i)
        # Per-row whitespace is trimmed (row 1's Y was "  Modal ").
        self.assertEqual(parsed["rows"][1]["y_name"], "Modal")
        self.assertEqual(parsed["rows"][0]["input_url"],
                         "https://www.eastlinkcap.com/portfolio/")

    def test_a_row_carries_only_x_y_and_url(self):
        """No location: the OCR'd portfolio page supplies none, so City/Country columns
        are ignored rather than silently plumbed into the prompt."""
        raw = (
            "Input_URL,Company_Name_X,Company_Name_Y,City,Country\n"
            "https://m25vc.com/portfolio,m25vc,Sanzo,New York,United States\n"
        ).encode()
        row = parse_relationship_csv(raw)["rows"][0]
        self.assertEqual(set(row), {"row_index", "x_name", "y_name", "input_url"})

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
        self.assertEqual(parsed["rows"][0]["y_name"], "Sanzo")

    def test_preserves_error_marker_text_as_company_y(self):
        raw = (
            "Input_URL,Company_Name_X,Company_Name_Y\n"
            "https://a.example,a,FETCH_ERROR: 403 Forbidden\n"
            "https://b.example,b,error: 503 unavailable\n"
        ).encode()
        parsed = parse_relationship_csv(raw)
        self.assertEqual(
            [r["y_name"] for r in parsed["rows"]],
            ["FETCH_ERROR: 403 Forbidden", "error: 503 unavailable"],
        )

    def test_allows_error_word_when_it_is_not_a_marker_prefix(self):
        raw = (
            "Input_URL,Company_Name_X,Company_Name_Y\n"
            "https://a.example,a,Error Coffee Company\n"
        ).encode()
        self.assertEqual(
            parse_relationship_csv(raw)["rows"][0]["y_name"],
            "Error Coffee Company",
        )


if __name__ == "__main__":
    unittest.main()


class SampleLimitTests(unittest.TestCase):
    """Only the sample is retained; every row is still counted and validated. This runs
    in the API process, so a 500k CSV must not become 500k dicts in memory."""

    @staticmethod
    def _csv(n):
        head = b"Input_URL,Company_Name_X,Company_Name_Y\n"
        return head + b"".join(
            b"https://x.test/p,X Co,Y %d\n" % i for i in range(n))

    def test_row_count_is_full_but_retained_rows_are_capped(self):
        parsed = parse_relationship_csv(self._csv(50), sample_limit=10)
        self.assertEqual(parsed["total_rows"], 50)
        self.assertEqual(len(parsed["rows"]), 10)
        self.assertEqual(parsed["rows"][0]["y_name"], "Y 0")
        self.assertEqual(parsed["rows"][9]["row_index"], 9)

    def test_upload_path_keeps_no_rows_at_all(self):
        parsed = parse_relationship_csv(self._csv(50), sample_limit=0)
        self.assertEqual(parsed["total_rows"], 50)
        self.assertEqual(parsed["rows"], [])

    def test_a_bad_row_past_the_sample_still_fails(self):
        # The cap is on RETENTION, not validation — a blank required value at row 40
        # must still 400 the upload, not slip through because the sample ended at 10.
        raw = self._csv(50) + b",,\n"
        with self.assertRaises(InvalidRelationshipCSV) as ctx:
            parse_relationship_csv(raw, sample_limit=10)
        self.assertIn("row 52", str(ctx.exception))
