# backend/tests/test_ui_shell.py
"""Task 18: /app shell + /static mounting (offline; no Supabase needed)."""
import unittest

from fastapi.testclient import TestClient


class TestUiShell(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.main import app  # full app: legacy + routers + static mount

        cls.client = TestClient(app)

    def test_app_shell_served(self):
        res = self.client.get("/app")
        self.assertEqual(res.status_code, 200)
        self.assertIn("text/html", res.headers["content-type"])
        self.assertIn("Forage Console", res.text)

    def test_static_assets_served(self):
        for path in ("/static/js/api.js", "/static/js/main.js",
                     "/static/js/ui.js",
                     "/static/js/dashboard.js", "/static/js/companies.js",
                     "/static/css/app.css"):
            res = self.client.get(path)
            self.assertEqual(res.status_code, 200, path)

    def test_legacy_ui_still_served(self):
        res = self.client.get("/ui")
        self.assertEqual(res.status_code, 200)
        self.assertIn("text/html", res.headers["content-type"])


if __name__ == "__main__":
    unittest.main()
