# backend/tests/test_ui_shell.py
"""Task 18: /app shell + /static mounting (offline; no Supabase needed)."""
from pathlib import Path
import re
import subprocess
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
            ".page-intro",
            ".page-heading",
            ".page-copy",
            ".section-heading",
            ".metric-group",
            ".metric-item--default",
            ".metric-item--good",
            ".metric-item--muted",
            ".metric-item--warning",
            ".metric-item--danger",
            ".metric-item--info",
            ".metric-detail",
            ".detail-header",
            ".outcome-primary",
            ".outcome-value",
            ".outcome-rate",
            ".outcome-secondary",
            ".detail-section",
            ".detail-section-body",
            ".relationship-verdict",
            ".file-list",
            ".file-row",
            ".file-actions",
            ".modal-surface",
            ".code-block",
            ".pill-value",
            ".empty-state",
            "font-variant-numeric: tabular-nums",
            "grid-template-columns: repeat(4, minmax(0, 1fr))",
            ".metric-group > .metric-item:nth-child(4n + 1)",
            ".metric-strip > .metric-item:nth-child(n + 5)",
            "@media (max-width: 1024px)",
            ".metric-group > .metric-item:nth-child(odd)",
            ".metric-strip > .metric-item:nth-child(n + 3)",
            "@media (max-width: 640px)",
            ".metric-strip > .metric-item:nth-child(n + 2)",
            ".mobile-nav-toggle",
            ".sidebar-backdrop.hidden",
            "outline: 2px solid var(--accent)",
            "color: var(--subtle) !important",
            "color-mix(in srgb, var(--accent)",
            "prefers-reduced-motion",
        ):
            self.assertIn(token, res.text)

        ui = self.client.get("/static/js/ui.js")
        self.assertEqual(ui.status_code, 200)
        for helper in ("pageIntro", "sectionHeading", "metricItem", "emptyState"):
            self.assertIn(f"function {helper}", ui.text)

    def test_ui_helpers_dom_contract(self):
        backend_root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            ["node", "tests/ui_dom_contract.mjs"],
            cwd=backend_root,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_core_views_dom_contract(self):
        backend_root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            ["node", "tests/core_views_dom_contract.mjs"],
            cwd=backend_root,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_new_run_dom_contract(self):
        backend_root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            ["node", "tests/new_run_dom_contract.mjs"],
            cwd=backend_root,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_run_detail_dom_contract(self):
        backend_root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            ["node", "tests/run_detail_dom_contract.mjs"],
            cwd=backend_root,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_api_poll_dom_contract(self):
        backend_root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            ["node", "tests/api_poll_dom_contract.mjs"],
            cwd=backend_root,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_operations_and_tools_dom_contract(self):
        backend_root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            ["node", "tests/operations_tools_dom_contract.mjs"],
            cwd=backend_root,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_core_views_use_midnight_ledger_hierarchy(self):
        markers = {
            "/static/js/dashboard.js": ("pageIntro", "company-summary", "Recent runs"),
            "/static/js/companies.js": ("pageIntro", "Create company", "companiesTable"),
            "/static/js/runs.js": ("pageIntro", "Clear", "filter-toolbar"),
        }
        for path, expected in markers.items():
            res = self.client.get(path)
            self.assertEqual(res.status_code, 200, path)
            for marker in expected:
                self.assertIn(marker, res.text, f"{marker!r} missing from {path}")

    def test_operations_and_tools_use_dark_semantic_classes(self):
        for path in ("/static/js/operations.js", "/static/js/tools.js"):
            res = self.client.get(path)
            self.assertEqual(res.status_code, 200, path)
            for light_class in ("hover:bg-gray-50", "text-gray-700", "border-gray-200"):
                self.assertNotIn(light_class, res.text, f"{light_class!r} remains in {path}")

    def test_new_run_uses_step_and_selection_contract(self):
        res = self.client.get("/static/js/new_run.js")
        self.assertEqual(res.status_code, 200)
        for marker in (
            "workflow-step",
            "pipeline-option",
            "launch-summary",
            "aria-current",
        ):
            self.assertIn(marker, res.text)

    def test_run_detail_is_outcome_first(self):
        res = self.client.get("/static/js/run_detail.js")
        self.assertEqual(res.status_code, 200)
        for marker in (
            "outcomeSummary",
            "executionStrip",
            "filesSection",
            "deriveLegacyRunState",
            "Websites found",
            "Not found",
            "Errors",
        ):
            self.assertIn(marker, res.text)

        api = self.client.get("/static/js/api.js")
        self.assertEqual(api.status_code, 200)
        for marker in ("defaultTerminal", "isTerminal"):
            self.assertIn(marker, api.text)

        css = self.client.get("/static/css/app.css")
        self.assertEqual(css.status_code, 200)
        self.assertIn("@media (max-width: 768px)", css.text)

    def test_mobile_modal_actions_keep_intrinsic_width(self):
        js = self.client.get("/static/js/run_detail.js")
        self.assertEqual(js.status_code, 200)
        self.assertGreaterEqual(js.text.count("file-modal-action"), 2)

        css = self.client.get("/static/css/app.css")
        self.assertEqual(css.status_code, 200)
        self.assertRegex(
            css.text,
            re.compile(
                r"@media \(max-width: 767px\).*?"
                r"\.file-modal \.file-modal-action\s*\{[^}]*"
                r"width:\s*auto;[^}]*flex:\s*0 0 auto;",
                re.DOTALL,
            ),
        )

    def test_storage_copy_controls_are_accessible_buttons(self):
        ui = self.client.get("/static/js/ui.js")
        self.assertEqual(ui.status_code, 200)
        self.assertIn('el("button"', ui.text)
        self.assertIn('"aria-label": `Copy storage path:', ui.text)
        self.assertIn('class: "copy-control', ui.text)

        css = self.client.get("/static/css/app.css")
        self.assertEqual(css.status_code, 200)
        self.assertIn(".copy-control", css.text)

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
