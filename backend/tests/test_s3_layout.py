"""Offline tests for the SerpWow per-company/per-pipeline S3 key layout."""
import unittest

from app.services.serpwow import engine as la


class UploadS3PrefixTests(unittest.TestCase):
    def tearDown(self) -> None:
        la._s3_run_prefix_cache.clear()

    def test_company_and_pipeline(self) -> None:
        self.assertEqual(la._upload_s3_prefix("UP1", "Acme Inc", "gmaps"),
                         "acme-inc/gmaps/UP1")

    def test_company_only_falls_back(self) -> None:
        self.assertEqual(la._upload_s3_prefix("UP1", "Acme Inc", ""),
                         "acme-inc/UP1")

    def test_no_company_falls_back_to_id(self) -> None:
        self.assertEqual(la._upload_s3_prefix("UP1", "", ""), "UP1")

    def test_state_and_output_keys_share_folder(self) -> None:
        self.assertEqual(la._state_s3_key("UP1", "Acme Inc", "gmaps"),
                         "acme-inc/gmaps/UP1/state.json")
        self.assertEqual(la._output_s3_key("UP1", "Acme Inc", "gmaps"),
                         "acme-inc/gmaps/UP1/output.json")

    def test_batch_keys(self) -> None:
        self.assertEqual(la._batch_input_jsonl_s3_key("UP1", "Acme Inc", "gmaps"),
                         "acme-inc/gmaps/UP1/gemini_batch_input.jsonl")
        self.assertEqual(la._batch_output_json_s3_key("UP1", "Acme Inc", "gmaps"),
                         "acme-inc/gmaps/UP1/gemini_batch_output.json")

    def test_all_artifact_keys_use_resolved_legacy_prefix(self) -> None:
        la._s3_run_prefix_cache["UP1"] = "Acme_Inc/gmaps/UP1"

        self.assertEqual(la._state_s3_key("UP1", "Acme Inc", "gmaps"),
                         "Acme_Inc/gmaps/UP1/state.json")
        self.assertEqual(la._output_s3_key("UP1", "Acme Inc", "gmaps"),
                         "Acme_Inc/gmaps/UP1/output.json")
        self.assertEqual(la._batch_input_jsonl_s3_key("UP1", "Acme Inc", "gmaps"),
                         "Acme_Inc/gmaps/UP1/gemini_batch_input.jsonl")
        self.assertEqual(la._batch_output_json_s3_key("UP1", "Acme Inc", "gmaps"),
                         "Acme_Inc/gmaps/UP1/gemini_batch_output.json")

    def test_normalized_key_lookup_does_not_seed_prefix_cache(self) -> None:
        self.assertEqual(la._state_s3_key("NEW1", "Acme Inc", "gmaps"),
                         "acme-inc/gmaps/NEW1/state.json")
        self.assertNotIn("NEW1", la._s3_run_prefix_cache)

    def test_output_payload_carries_company_name(self) -> None:
        state = {"upload_id": "UP1", "company_name": "Acme Inc", "pipeline": "gmaps",
                 "status": "completed", "total_rows": 0, "processed_rows": 0,
                 "success_rows": 0, "failed_rows": 0, "rows": []}
        payload = la.build_upload_output_payload(state)
        self.assertEqual(payload["company_name"], "Acme Inc")
        self.assertEqual(payload["pipeline"], "gmaps")


if __name__ == "__main__":
    unittest.main()
