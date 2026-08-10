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
    gmaps_search,
    gsearch_discover,
    summarize_upload_state,
    update_summary_cache,
    upload_summaries_cache,
)


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

    def test_total_is_wall_clock_not_sum_of_parallel_rows(self) -> None:
        # Regression: 3 rows each "took" 20 min of work but ran in parallel and the
        # whole run finished within 3 min. Total must be the 3-min wall-clock span
        # (start→last completion), NOT the 60-min sum (the old overcount bug).
        state = {
            "upload_id": "wall", "pipeline": "gsearch", "status": "processing",
            "created_at": "2026-06-08T00:00:00+00:00",
            "updated_at": "2026-06-08T00:03:00+00:00",
            "rows": [
                {"row_index": 1, "status": "completed",
                 "status_updated_at": "2026-06-08T00:02:40+00:00",
                 "result": {"context": {"timing": {"total_seconds": 1200}}}},
                {"row_index": 2, "status": "completed",
                 "status_updated_at": "2026-06-08T00:02:55+00:00",
                 "result": {"context": {"timing": {"total_seconds": 1200}}}},
                {"row_index": 3, "status": "completed",
                 "status_updated_at": "2026-06-08T00:03:00+00:00",
                 "result": {"context": {"timing": {"total_seconds": 1200}}}},
            ],
        }
        summary = summarize_upload_state(state)
        self.assertEqual(summary["processing_seconds_total"], 180.0)   # wall-clock
        self.assertEqual(summary["processing_seconds_avg"], 1200.0)    # mean per-row work
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
                    # row finished 5s after the run started (wall-clock span)
                    "status_updated_at": "2026-06-08T00:00:05+00:00",
                    "result": {"context": {"timing": {"total_seconds": 1.25}}},
                }
            ],
        }

        try:
            summary = summarize_upload_state(dict(state))
            output = build_upload_output_payload(summary)
            update_summary_cache(upload_id, summary)
            cached = upload_summaries_cache[upload_id]

            # total = wall-clock start→completion (5s); avg = mean per-row work (1.25s)
            self.assertEqual(summary["processing_seconds_total"], 5.0)
            self.assertEqual(summary["processing_seconds_avg"], 1.25)
            self.assertEqual(summary["processing_seconds_count"], 1)
            self.assertEqual(output["processing_seconds_total"], 5.0)
            self.assertEqual(output["processing_seconds_avg"], 1.25)
            self.assertEqual(output["processing_seconds_count"], 1)
            self.assertEqual(cached["processing_seconds_total"], 5.0)
            self.assertEqual(cached["processing_seconds_avg"], 1.25)
            self.assertEqual(cached["processing_seconds_count"], 1)
        finally:
            upload_summaries_cache.pop(upload_id, None)


class StandaloneEndpointTimingTests(unittest.TestCase):
    def test_gmaps_standalone_endpoint_returns_processing_seconds(self) -> None:
        # /gmaps/discover and /gmaps/details went away with the SerpWow two-step flow;
        # scrape.do's maps/search is a single call, so only /gmaps/search remains.
        async def fake_process_gmaps_query(query, gl="us"):
            return {"query": query, "gl": gl, "results": []}

        fake_gmaps = types.SimpleNamespace(process_gmaps_query=fake_process_gmaps_query)

        with patch.dict(os.environ, {"SCRAPEDO_TOKEN": "test-token"}), patch.dict(
            sys.modules, {"app.services.serpwow.scrapedo_maps_client": fake_gmaps},
        ):
            search = asyncio.run(gmaps_search("Example Inc", country="US"))

        self.assertIn("processing_seconds", search)
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
