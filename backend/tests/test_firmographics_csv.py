"""Firmographics input CSV: the website column and its aliases."""
import unittest

from app.services.serpwow.csv_input import parse_firmographics_csv_rows


def _rows(header: str):
    return parse_firmographics_csv_rows(
        f"{header},company_name\nhttps://acme.com,Acme\n".encode("utf-8"))


class FirmographicsWebsiteColumnTests(unittest.TestCase):
    def test_website_url_is_the_canonical_column(self):
        """Same name every other pipeline WRITES, so a found.csv can be fed straight
        back in for enrichment. It used to be rejected."""
        self.assertEqual(_rows("website_url")[0]["official_website"],
                         "https://acme.com")

    def test_legacy_and_short_aliases_still_work(self):
        for header in ("official_website", "website", "url", "domain"):
            with self.subTest(header=header):
                self.assertEqual(_rows(header)[0]["official_website"],
                                 "https://acme.com")

    def test_a_csv_with_no_website_column_says_which_one_it_wants(self):
        with self.assertRaises(ValueError) as ctx:
            parse_firmographics_csv_rows(b"company_name,country\nAcme,us\n")
        self.assertIn("website_url", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
