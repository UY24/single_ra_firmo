import unittest

from app.services.serpwow.relationship_csv import (
    InvalidRelationshipCSV,
    parse_relationship_csv,
)

CSV_OK = (
    "Input_URL,Company_Name_X,Box_No,Image_URL,Company_Name_Y,OCR_Status\n"
    "https://www.m25vc.com/portfolio,m25vc,50.0,img1,,NO_TEXT\n"
    "https://www.eastlinkcap.com/portfolio/,eastlinkcap,2.0,img2,Modal,SUCCESS\n"
    "https://www.eastlinkcap.com/portfolio/,eastlinkcap,3.0,img3,  Modal ,SUCCESS\n"
    "https://other.vc/p,othervc,4.0,img4,Modal,SUCCESS\n"
    "https://www.x.com/p,xvc,5.0,img5,Sanzo,SUCCESS\n"
).encode("utf-8")


class TestParseRelationshipCSV(unittest.TestCase):
    def test_happy_path_groups_pairs_and_counts_blanks(self):
        parsed = parse_relationship_csv(CSV_OK)
        self.assertEqual(parsed["header"][0], "Input_URL")
        self.assertEqual(len(parsed["original_rows"]), 5)
        self.assertEqual(parsed["blank_row_indices"], [0])
        # (eastlinkcap, Modal) deduped across rows 1+2; (othervc, Modal) separate.
        self.assertEqual(len(parsed["pairs"]), 3)
        by_key = {(p["x_name"], p["y_name"]): p for p in parsed["pairs"]}
        self.assertEqual(by_key[("eastlinkcap", "Modal")]["source_row_indices"], [1, 2])
        self.assertEqual(by_key[("othervc", "Modal")]["source_row_indices"], [3])
        self.assertEqual(by_key[("xvc", "Sanzo")]["source_row_indices"], [4])
        # pair_index is 1-based and unique
        self.assertEqual(sorted(p["pair_index"] for p in parsed["pairs"]), [1, 2, 3])
        # Input_URL captured for the phase-4 anchor
        self.assertEqual(by_key[("eastlinkcap", "Modal")]["input_url"],
                         "https://www.eastlinkcap.com/portfolio/")

    def test_dedupe_key_is_case_and_whitespace_insensitive_but_originals_kept(self):
        parsed = parse_relationship_csv(CSV_OK)
        modal = next(p for p in parsed["pairs"] if p["x_name"] == "eastlinkcap")
        self.assertEqual(modal["y_name"], "Modal")  # first-seen original, trimmed

    def test_optional_city_country_columns(self):
        raw = (
            "Company_Name_X,Company_Name_Y,City,Country\n"
            "m25vc,Sanzo,New York,United States\n"
        ).encode()
        parsed = parse_relationship_csv(raw)
        self.assertEqual(parsed["pairs"][0]["city"], "New York")
        self.assertEqual(parsed["pairs"][0]["country"], "United States")

    def test_blank_x_is_allowed_as_its_own_pair(self):
        raw = "Company_Name_X,Company_Name_Y\n,Sanzo\n".encode()
        parsed = parse_relationship_csv(raw)
        self.assertEqual(parsed["pairs"][0]["x_name"], "")

    def test_missing_y_column_raises(self):
        raw = "Company_Name_X,Whatever\nm25vc,zzz\n".encode()
        with self.assertRaises(InvalidRelationshipCSV):
            parse_relationship_csv(raw)

    def test_empty_file_raises(self):
        with self.assertRaises(InvalidRelationshipCSV):
            parse_relationship_csv(b"")

    def test_bom_tolerated(self):
        raw = "﻿Company_Name_X,Company_Name_Y\nm25vc,Sanzo\n".encode("utf-8")
        parsed = parse_relationship_csv(raw)
        self.assertEqual(parsed["pairs"][0]["y_name"], "Sanzo")


if __name__ == "__main__":
    unittest.main()
