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

    def test_app_shell_has_accessible_mobile_navigation(self):
        res = self.client.get("/app")
        self.assertEqual(res.status_code, 200)
        for marker in (
            'id="sidebar-toggle"',
            'aria-controls="app-sidebar"',
            'id="sidebar-backdrop"',
            'id="app-sidebar"',
        ):
            self.assertIn(marker, res.text)

    def test_midnight_ledger_theme_contract_is_served(self):
        res = self.client.get("/static/css/app.css")
        self.assertEqual(res.status_code, 200)
        for token in (
            "--bg: #080d16",
            "--sidebar: #0b111c",
            "--accent: #65d6ad",
            "--blue: #72a7ff",
            "--blue-strong: #4d8df4",
            "--cyan: #65d6ad",
            "--amber: #f2b95f",
            "--red: #f07b84",
            ".outcome-summary",
            ".metric-strip",
            ".mobile-nav-toggle",
            ".sidebar-backdrop.hidden",
            "outline: 2px solid var(--accent)",
            "color: var(--subtle) !important",
            "color-mix(in srgb, var(--accent)",
            "prefers-reduced-motion",
        ):
            self.assertIn(token, res.text)

    def test_shell_script_has_accessible_drawer_state(self):
        res = self.client.get("/static/js/main.js")
        self.assertEqual(res.status_code, 200)
        for marker in (
            'main.toggleAttribute("inert", open)',
            'main.setAttribute("aria-hidden", "true")',
            'main.removeAttribute("aria-hidden")',
            'firstNavLink.focus({ preventScroll: true })',
            'a.setAttribute("aria-current", "page")',
            'a.removeAttribute("aria-current")',
            'matchMedia("(min-width: 768px)")',
        ):
            self.assertIn(marker, res.text)

    def test_all_console_views_are_static_assets(self):
        for path in (
            "/static/js/tools.js",
            "/static/js/operations.js",
            "/static/js/run_detail.js",
        ):
            res = self.client.get(path)
            self.assertEqual(res.status_code, 200, path)

    def test_static_assets_served(self):
        for path in ("/static/js/api.js", "/static/js/main.js",
                     "/static/js/ui.js",
                     "/static/js/dashboard.js", "/static/js/companies.js",
                     "/static/js/new_run.js", "/static/js/runs.js",
                     "/static/js/run_detail.js", "/static/js/operations.js",
                     "/static/css/app.css"):
            res = self.client.get(path)
            self.assertEqual(res.status_code, 200, path)

    def test_legacy_ui_redirects_to_app(self):
        res = self.client.get("/ui", follow_redirects=False)
        self.assertEqual(res.status_code, 307)
        self.assertEqual(res.headers["location"], "/app")

        res = self.client.get("/ui", follow_redirects=True)
        self.assertEqual(res.status_code, 200)
        self.assertIn("Forage Console", res.text)


if __name__ == "__main__":
    unittest.main()
