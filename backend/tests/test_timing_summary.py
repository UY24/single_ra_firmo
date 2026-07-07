import asyncio
import os
import sys
import types
import unittest
from unittest.mock import patch

from app.services.serpwow import engine as app_module
from app.services.serpwow.engine import (
    build_upload_output_payload,
    build_processing_timing_summary,
    gmaps_details,
    gmaps_discover,
    gmaps_search,
    gsearch_discover,
    summarize_upload_state,
    update_summary_cache,
    upload_summaries_cache,
)


class FakeClientSession:
    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class ProcessingTimingSummaryTests(unittest.TestCase):
    def test_mixed_rows_only_count_rows_with_timing(self) -> None:
        rows = [
            {
                "status": "completed",
                "result": {"context": {"timing": {"total_seconds": 1.2}}},
            },
            {
                "status": "failed",
                "result": {"context": {"timing": {"total_seconds": "2.3"}}},
            },
            {
                "status": "completed",
                "output": {"context": {"timing": {"total_seconds": 0.5}}},
            },
            {"status": "failed", "error": "missing result"},
            {"status": "completed", "result": {}},
            {
                "status": "completed",
                "result": {"context": {"timing": {"total_seconds": -1}}},
            },
        ]

        summary = build_processing_timing_summary(rows)

        self.assertEqual(summary["processing_seconds_total"], 4.0)
        self.assertEqual(summary["processing_seconds_avg"], 1.333)
        self.assertEqual(summary["processing_seconds_count"], 3)

    def test_empty_rows_returns_zero_summary(self) -> None:
        summary = build_processing_timing_summary([])

        self.assertEqual(summary["processing_seconds_total"], 0.0)
        self.assertEqual(summary["processing_seconds_avg"], 0.0)
        self.assertEqual(summary["processing_seconds_count"], 0)

    def test_upload_summary_cache_and_output_payload_include_timing(self) -> None:
        upload_id = "timing-summary-test"
        state = {
            "upload_id": upload_id,
            "pipeline": "gmaps",
            "created_at": "2026-06-08T00:00:00+00:00",
            "updated_at": "2026-06-08T00:00:00+00:00",
            "rows": [
                {
                    "row_index": 1,
                    "company_name": "Example Inc",
                    "country": "United States",
                    "status": "completed",
                    "result": {"context": {"timing": {"total_seconds": 1.25}}},
                }
            ],
        }

        try:
            summary = summarize_upload_state(dict(state))
            output = build_upload_output_payload(summary)
            update_summary_cache(upload_id, summary)
            cached = upload_summaries_cache[upload_id]

            self.assertEqual(summary["processing_seconds_total"], 1.25)
            self.assertEqual(summary["processing_seconds_avg"], 1.25)
            self.assertEqual(summary["processing_seconds_count"], 1)
            self.assertEqual(output["processing_seconds_total"], 1.25)
            self.assertEqual(output["processing_seconds_avg"], 1.25)
            self.assertEqual(output["processing_seconds_count"], 1)
            self.assertEqual(cached["processing_seconds_total"], 1.25)
            self.assertEqual(cached["processing_seconds_avg"], 1.25)
            self.assertEqual(cached["processing_seconds_count"], 1)
        finally:
            upload_summaries_cache.pop(upload_id, None)


class StandaloneEndpointTimingTests(unittest.TestCase):
    def test_gmaps_standalone_endpoints_return_processing_seconds(self) -> None:
        async def fake_fetch_data_cids(session, query, gl_override=None):
            return ["cid-1"]

        async def fake_fetch_place_detail(session, cid, sem):
            return {"data_cid": cid, "title": "Example"}

        async def fake_process_gmaps_query(query, country=None):
            return {"query": query, "country": country, "results": []}

        fake_gmaps = types.SimpleNamespace(
            country_to_gl=lambda country: "us",
            get_gl_from_query=lambda query: "us",
            fetch_data_cids=fake_fetch_data_cids,
            fetch_place_detail=fake_fetch_place_detail,
            process_gmaps_query=fake_process_gmaps_query,
        )
        fake_aiohttp = types.SimpleNamespace(
            ClientTimeout=lambda total: object(),
            ClientSession=FakeClientSession,
        )

        with patch.dict(os.environ, {"SERPWOW_API_KEY": "test-key"}), patch.dict(
            sys.modules,
            {"app.services.serpwow.gmaps_client": fake_gmaps, "aiohttp": fake_aiohttp},
        ):
            discover = asyncio.run(gmaps_discover("Example Inc", country="US"))
            details = asyncio.run(gmaps_details("cid-1"))
            search = asyncio.run(gmaps_search("Example Inc", country="US"))

        self.assertIn("processing_seconds", discover)
        self.assertIn("processing_seconds", details)
        self.assertIn("processing_seconds", search)
        self.assertGreaterEqual(discover["processing_seconds"], 0)
        self.assertGreaterEqual(details["processing_seconds"], 0)
        self.assertGreaterEqual(search["processing_seconds"], 0)

    def test_gsearch_discover_returns_processing_seconds(self) -> None:
        async def fake_run_serpwow_search(query, country=None, client=None):
            return {
                "used": True,
                "error": None,
                "search_url": f"https://example.test/search?q={query}",
                "candidates": ["https://example.com"],
                "raw_response": {"organic_results": []},
            }

        with patch.dict(os.environ, {"SERPWOW_API_KEY": "test-key"}), patch.object(
            app_module,
            "run_serpwow_search",
            fake_run_serpwow_search,
        ):
            data = asyncio.run(
                gsearch_discover(
                    company_name="Example Inc",
                    country="United States",
                    phase="fallback",
                )
            )

        self.assertIn("processing_seconds", data)
        self.assertGreaterEqual(data["processing_seconds"], 0)


if __name__ == "__main__":
    unittest.main()
