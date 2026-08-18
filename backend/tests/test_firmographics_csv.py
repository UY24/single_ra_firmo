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


class FirmographicsPreviewTests(unittest.TestCase):
    """The New Run preview must use the parser the pipeline actually uses. The shared
    /uploads/preview runs parse_entities_csv, which needs company_name + country — a
    firmographics CSV has neither, so it fell through to POSITIONAL parsing and reported
    "col 1 = company name, col 2 = country" for a file whose first column is a URL."""

    CSV = (b"Website,Company,Country,ISIC\n"
           b"https://acme.com,Acme,us,1234\n"
           b"https://beta.io,Beta,de,5678\n")

    def _preview(self, body=None):
        import io

        from fastapi.testclient import TestClient

        from app.services.serpwow.engine import app
        return TestClient(app).post(
            "/uploads/firmographics/preview",
            files={"file": ("firmo.csv", io.BytesIO(body or self.CSV), "text/csv")})

    def test_preview_maps_the_real_headers_and_never_goes_positional(self):
        resp = self._preview()
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["total_rows"], 2)
        self.assertFalse(body.get("positional"))
        self.assertEqual(body["columns_detected"]["website_url"], "Website")
        self.assertEqual(body["columns_detected"]["company_name"], "Company")
        # A column we don't map isn't invented into the mapping table.
        self.assertNotIn("ISIC", body["columns_detected"].values())
        self.assertEqual(body["sample_rows"][0]["website_url"], "https://acme.com")

    def test_a_csv_with_no_website_column_is_a_400_not_a_positional_guess(self):
        resp = self._preview(b"company_name,country\nAcme,us\n")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("website_url", resp.json()["detail"])


class FirmographicsCompanyAliasTests(unittest.TestCase):
    def test_no_company_column_leaves_the_name_empty(self):
        """Use the columns the file has, invent nothing. It used to fill company_name
        with the URL's domain, which reads like real data in the output and is not."""
        rows = parse_firmographics_csv_rows(b"website_url\nhttps://acme.com\n")
        self.assertEqual(rows[0]["company_name"], "")

    def test_company_aliases_match_the_canonical_parser(self):
        """found.csv writes the input's own company header — often entity_name, which
        parse_entities_csv accepts. Firmographics rejecting it silently fell back to the
        domain, so a round-tripped file lost every company name."""
        rows = parse_firmographics_csv_rows(
            b"entity_name,country,website_url\nBag Polska,Poland,http://bagpolska.pl\n")
        self.assertEqual(rows[0]["company_name"], "Bag Polska")
