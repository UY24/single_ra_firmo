import json
import tempfile
import unittest
from pathlib import Path

from app.services.serpwow import engine
from app.services.serpwow import outcomes as o


class TestErrorDump(unittest.TestCase):
    def test_writes_error_json_per_error_row(self):
        with tempfile.TemporaryDirectory() as d:
            rows = [
                {"row_index": 3, "company_name": "Bad Co", "status": "failed",
                 "outcome": o.OUTCOME_ERROR, "error_source": o.SRC_SERPWOW,
                 "error_category": o.CAT_RATE_LIMIT, "error": "429 too many",
                 "result": {"context": {"formatted_results": [
                     {"phase": "p1", "success": False, "error": "429", "status_code": 429,
                      "error_category": o.CAT_RATE_LIMIT}]}}},
                {"row_index": 4, "company_name": "OK Co", "status": "completed",
                 "outcome": o.OUTCOME_NOT_FOUND},
            ]
            paths = engine._write_error_dumps(Path(d), {"rows": rows})
            self.assertEqual(len(paths), 1)
            data = json.loads(list(paths.values())[0].read_text())
            self.assertEqual(data["error_source"], o.SRC_SERPWOW)
            self.assertEqual(data["phases"][0]["status_code"], 429)

    def test_no_files_when_no_error_rows(self):
        with tempfile.TemporaryDirectory() as d:
            rows = [
                {"row_index": 1, "company_name": "OK Co", "status": "completed",
                 "outcome": o.OUTCOME_NOT_FOUND},
            ]
            paths = engine._write_error_dumps(Path(d), {"rows": rows})
            self.assertEqual(paths, {})
            self.assertFalse((Path(d) / "errors").exists())

    def test_filename_and_content_shape(self):
        with tempfile.TemporaryDirectory() as d:
            rows = [
                {"row_index": 7, "company_name": "Weird & Co!", "status": "failed",
                 "outcome": o.OUTCOME_ERROR, "error_source": o.SRC_GEMINI,
                 "error_category": o.CAT_TIMEOUT, "error": "timed out",
                 "result": {"context": {"formatted_results": [
                     {"phase": "p1", "success": False, "error": "timeout",
                      "status_code": None, "error_category": o.CAT_TIMEOUT},
                     {"phase": "p2", "success": True, "error": None,
                      "status_code": 200, "error_category": None},
                 ]}}},
            ]
            paths = engine._write_error_dumps(Path(d), {"rows": rows})
            self.assertEqual(len(paths), 1)
            name, path = next(iter(paths.items()))
            self.assertEqual(name, "000007_Weird_Co_error.json")
            self.assertTrue(path.exists())
            self.assertEqual(path.parent, Path(d) / "errors")
            data = json.loads(path.read_text())
            self.assertEqual(data["row_index"], 7)
            self.assertEqual(data["company_name"], "Weird & Co!")
            self.assertEqual(data["error_source"], o.SRC_GEMINI)
            self.assertEqual(data["error_category"], o.CAT_TIMEOUT)
            self.assertEqual(data["error_detail"], "timed out")
            self.assertEqual(data["http_status"], None)
            self.assertEqual(len(data["phases"]), 2)
            self.assertEqual(data["phases"][1]["status_code"], 200)


if __name__ == "__main__":
    unittest.main()
