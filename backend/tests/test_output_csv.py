"""Offline tests for the CSV export of an upload's output payload (output_export)."""
import csv
import io
import json
import unittest

from app.services.serpwow.output_export import (
    build_upload_output_csv_bytes,
    build_upload_output_table,
    build_upload_output_xlsx_bytes,
)


def _payload():
    """One firmographics row, shaped like a real /output result entry."""
    result = {
        "official_website": "http://www.bagpolska.pl",
        "summary": "Firmographic extraction completed from provided official website.",
        # Non-ASCII in three scripts: Polish, Turkish, an em dash.
        "address": "ul. Leśna 10, 64-120 Krzemieniewo, Poland — Türkiye şube",
        "phone": "+48 61 651 01 03",
        "email": "info@bagpolska.pl",
        "industry": "Agricultural equipment sales",
        "products": ["Silage press machines", "Grain packing machines"],
        "services": ["Forage ensiling", "Grain crushing"],
        "serpwow_cost_usd": 0.00035,
        "gemini_cost_usd": 0.0001958,
        "total_cost_usd": 0.0005458,
        "context": {"pipeline": "firmographics", "timing": {"total_seconds": 32.153}},
    }
    return {
        "upload_id": "UP1",
        "status": "completed",
        "total_rows": 1,
        "processed_rows": 1,
        "success_rows": 1,
        "failed_rows": 0,
        "results": [{
            "row_index": 1,
            "input": {"company_name": "BAG Polska", "country": "Poland",
                      "official_website": "http://www.bagpolska.pl"},
            "status": "completed",
            "error": None,
            "output": result,
        }],
    }


def _read(data: bytes) -> list[list[str]]:
    # utf-8-sig strips the BOM the writer adds, exactly as a downstream reader would.
    return list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))


class OutputCsvTests(unittest.TestCase):
    def test_columns_match_the_xlsx_export(self) -> None:
        """One table builder feeds both formats, so the header can never drift."""
        headers, _ = build_upload_output_table(_payload())
        self.assertEqual(_read(build_upload_output_csv_bytes(_payload()))[0], headers)

    def test_bom_present_for_excel(self) -> None:
        # Without this Excel decodes the file as the OS legacy codepage and mangles
        # every non-ASCII address.
        self.assertTrue(build_upload_output_csv_bytes(_payload()).startswith(b"\xef\xbb\xbf"))

    def test_non_ascii_survives_round_trip(self) -> None:
        rows = _read(build_upload_output_csv_bytes(_payload()))
        row = dict(zip(rows[0], rows[1]))
        self.assertEqual(row["output_address"],
                         "ul. Leśna 10, 64-120 Krzemieniewo, Poland — Türkiye şube")
        # The JSON column keeps real characters too (ensure_ascii=False), so it is
        # readable rather than \uXXXX soup.
        self.assertIn("Leśna", row["output_json"])
        self.assertEqual(json.loads(row["output_json"])["email"], "info@bagpolska.pl")

    def test_embedded_newline_stays_inside_one_field(self) -> None:
        payload = _payload()
        payload["results"][0]["output"]["address"] = "Line one\nLine two"
        rows = _read(build_upload_output_csv_bytes(payload))
        self.assertEqual(len(rows), 2, "quoted newline split the row")
        self.assertEqual(dict(zip(rows[0], rows[1]))["output_address"], "Line one\nLine two")

    def test_lists_join_and_none_becomes_empty(self) -> None:
        rows = _read(build_upload_output_csv_bytes(_payload()))
        row = dict(zip(rows[0], rows[1]))
        self.assertEqual(row["output_products"],
                         "Silage press machines, Grain packing machines")
        self.assertEqual(row["row_error"], "")

    def test_control_chars_dropped_but_value_not_truncated(self) -> None:
        payload = _payload()
        payload["results"][0]["output"]["summary"] = "a\x00b" + ("x" * 40000)
        row = dict(zip(*_read(build_upload_output_csv_bytes(payload))[:2]))
        self.assertNotIn("\x00", row["output_summary"])
        # The XLSX path caps cells at 32767; CSV has no such limit and must not silently
        # lose the tail.
        self.assertEqual(len(row["output_summary"]), 40002)

    def test_empty_results_still_yields_a_header(self) -> None:
        rows = _read(build_upload_output_csv_bytes({"upload_id": "UP1", "results": []}))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "upload_id")

    def test_one_honest_raw_key_column(self) -> None:
        """s3_html_key + s3_serpwow_json_key were two columns of the SAME value, both
        misnamed: nothing ever held HTML, and the SerpWow one carried a scrape.do payload
        for every migrated pipeline. They collapsed into one on 2026-08-19."""
        headers, _ = build_upload_output_table(_payload())
        self.assertIn("raw_response_s3_key", headers)
        self.assertNotIn("s3_html_key", headers)
        self.assertNotIn("s3_serpwow_json_key", headers)

    def test_xlsx_export_still_builds(self) -> None:
        """The refactor that extracted the shared table must not break the XLSX path."""
        self.assertTrue(build_upload_output_xlsx_bytes(_payload()).startswith(b"PK"))


if __name__ == "__main__":
    unittest.main()
