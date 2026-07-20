import unittest
import unittest.mock

from app.services.serpwow import engine as legacy_app


class TestFileLinks(unittest.TestCase):
    def test_gsearch_links_include_new_files(self):
        with unittest.mock.patch.dict("os.environ", {"S3_BUCKET": "bkt"}):
            links = legacy_app._upload_file_links("up1", "Acme Motors", legacy_app.PIPELINE_GSEARCH)
        self.assertIn("report.json", links)
        self.assertIn("found.csv", links)
        self.assertIn("notFound.csv", links)
        self.assertIn("run.log", links)
        self.assertEqual(links["report.json"], "s3://bkt/acme-motors/gsearch/up1/report.json")

    def test_non_gsearch_links_unchanged(self):
        with unittest.mock.patch.dict("os.environ", {"S3_BUCKET": "bkt"}):
            links = legacy_app._upload_file_links("up1", "Acme", legacy_app.PIPELINE_FIRMOGRAPHICS)
        self.assertNotIn("report.json", links)
        self.assertIn("state.json", links)


class TestResultRoute(unittest.TestCase):
    def test_route_registered_with_allowlist(self):
        paths = {r.path for r in legacy_app.app.routes}
        self.assertIn("/uploads/{upload_id}/result", paths)
        self.assertIn("found.csv", legacy_app._GSEARCH_RESULT_FILES)
        self.assertIn("report.json", legacy_app._GSEARCH_RESULT_FILES)


if __name__ == "__main__":
    unittest.main()
