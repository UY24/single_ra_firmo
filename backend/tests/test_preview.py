# backend/tests/test_preview.py
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers.ai_mode import build_preview


class TestPreview(unittest.TestCase):
    def test_preview_payload(self):
        csv = "company_name,country,local_name\nAcme,Japan,アクメ\nBeta,Germany,\n"
        p = build_preview(csv.encode())
        self.assertEqual(p["total_rows"], 2)
        self.assertEqual(len(p["sample_rows"]), 2)
        self.assertEqual(p["columns_detected"]["company_name"], "company_name")
        self.assertEqual(p["sample_rows"][0]["company_local_name"], "アクメ")

    def test_preview_invalid_csv(self):
        from app.models.entities import InvalidCSVError
        with self.assertRaises(InvalidCSVError):
            build_preview(b"")


class TestPreviewEndpoint(unittest.TestCase):
    def setUp(self):
        from app.routers.ai_mode import router

        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

    def test_preview_endpoint_ok(self):
        csv = (
            "company_name,country,local_name\n"
            "Acme,Japan,アクメ\nBeta,Germany,\nGamma,France,\n"
            "Delta,Italy,\nEpsilon,Spain,\nZeta,Poland,\n"
        )
        res = self.client.post(
            "/uploads/preview",
            files={"file": ("input.csv", csv.encode(), "text/csv")},
        )
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["total_rows"], 6)
        self.assertEqual(len(body["sample_rows"]), 5)  # capped at 5
        self.assertEqual(body["columns_detected"]["country"], "country")
        self.assertFalse(body["positional"])
        self.assertEqual(body["warnings"], [])
        self.assertEqual(body["sample_rows"][0]["company_name"], "Acme")

    def test_preview_endpoint_invalid_csv_400(self):
        res = self.client.post(
            "/uploads/preview",
            files={"file": ("input.csv", b"", "text/csv")},
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("CSV is empty", res.json()["detail"])


if __name__ == "__main__":
    unittest.main()
