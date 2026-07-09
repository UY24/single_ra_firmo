import json, tempfile, unittest
from pathlib import Path
from app.services.serpwow import serpwow_reporting as rep
from app.services.serpwow import outcomes as o


def _row(company, status, outcome, website=None, src=None, cat=None):
    return {"company_name": company, "country": "US", "status": status, "outcome": outcome,
            "error_source": src, "error_category": cat,
            "result": {"official_website": website, "context": {}}}


class TestErrorReporting(unittest.TestCase):
    def test_breakdowns_in_summary(self):
        state = {"pipeline": "gsearch", "upload_id": "u1", "rows": [
            _row("A", "completed", o.OUTCOME_FOUND, website="https://a.com"),
            _row("B", "completed", o.OUTCOME_NOT_FOUND),
            _row("C", "failed", o.OUTCOME_ERROR, src=o.SRC_SERPWOW, cat=o.CAT_RATE_LIMIT),
        ]}
        results = rep.state_to_entity_results(state)
        summary = rep.build_summary(state, results)
        self.assertEqual(summary["outcome_breakdown"], {"found": 1, "not_found": 1, "errored": 1})
        self.assertEqual(summary["error_breakdown"]["by_source"], {"serpwow": 1})
        self.assertEqual(summary["error_breakdown"]["by_category"], {"rate_limit": 1})

    def test_notfound_row_still_in_notfound_csv(self):
        state = {"pipeline": "gsearch", "upload_id": "u1", "rows": [
            _row("B", "completed", o.OUTCOME_NOT_FOUND)]}
        with tempfile.TemporaryDirectory() as d:
            rep.write_outputs(Path(d), state)
            nf = (Path(d) / "notFound.csv").read_text()
        self.assertIn("B", nf)


if __name__ == "__main__":
    unittest.main()
