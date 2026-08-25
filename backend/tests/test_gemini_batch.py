"""Offline unit tests for :mod:`gemini_batch` (no network).

The HTTP primitives ``_http_post_json`` / ``_http_get_json`` / ``_http_get_bytes``
are monkeypatched so nothing touches the network. Uses ``unittest`` to match the
style of the existing ``tests/`` suite (see ``test_timing_summary.py``).
"""

import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from app.services.ai_mode import gemini_batch


class MessagesToGeminiRequestTests(unittest.TestCase):
    def test_user_only_has_no_system_instruction(self) -> None:
        request = gemini_batch.messages_to_gemini_request(
            [{"role": "user", "content": "hello"}]
        )

        self.assertEqual(
            request["generationConfig"],
            {"responseMimeType": "application/json", "temperature": 0},
        )
        self.assertNotIn("systemInstruction", request)
        self.assertEqual(len(request["contents"]), 1)
        self.assertEqual(request["contents"][0]["role"], "user")
        self.assertEqual(request["contents"][0]["parts"][0]["text"], "hello")

    def test_system_message_goes_into_system_instruction(self) -> None:
        request = gemini_batch.messages_to_gemini_request(
            [
                {"role": "system", "content": "be terse"},
                {"role": "user", "content": "hi"},
            ]
        )

        self.assertIn("systemInstruction", request)
        self.assertEqual(
            request["systemInstruction"]["parts"][0]["text"], "be terse"
        )
        # The user message stays in contents with role "user".
        self.assertEqual(len(request["contents"]), 1)
        self.assertEqual(request["contents"][0]["role"], "user")
        self.assertEqual(request["contents"][0]["parts"][0]["text"], "hi")


class BuildJsonlTests(unittest.TestCase):
    def test_two_items_yield_two_lines_with_key_and_request(self) -> None:
        text = gemini_batch.build_jsonl(
            [("k1", {"contents": []}), ("k2", {"contents": [{"role": "user"}]})]
        )
        lines = [line for line in text.splitlines() if line.strip()]

        self.assertEqual(len(lines), 2)
        first = json.loads(lines[0])
        second = json.loads(lines[1])
        self.assertEqual(set(first.keys()), {"key", "request"})
        self.assertEqual(set(second.keys()), {"key", "request"})
        self.assertEqual(first["key"], "k1")
        self.assertEqual(first["request"], {"contents": []})
        self.assertEqual(second["key"], "k2")


class StateHelperTests(unittest.TestCase):
    def test_metadata_state_succeeded_is_terminal_and_success(self) -> None:
        obj = {"metadata": {"state": "JOB_STATE_SUCCEEDED"}}
        state = gemini_batch.state_name(obj)
        done = bool(obj.get("done"))

        self.assertEqual(state, "JOB_STATE_SUCCEEDED")
        self.assertTrue(gemini_batch.is_terminal(state, done))
        self.assertTrue(gemini_batch.is_success(state, done, obj))

    def test_running_state_is_not_terminal(self) -> None:
        obj = {"state": {"name": "JOB_STATE_RUNNING"}}
        state = gemini_batch.state_name(obj)
        done = bool(obj.get("done"))

        self.assertEqual(state, "JOB_STATE_RUNNING")
        self.assertFalse(gemini_batch.is_terminal(state, done))
        self.assertFalse(gemini_batch.is_success(state, done, obj))

    def test_done_with_error_is_terminal_but_not_success(self) -> None:
        obj = {"done": True, "error": {"code": 1}}
        state = gemini_batch.state_name(obj)
        done = bool(obj.get("done"))

        self.assertTrue(gemini_batch.is_terminal(state, done))
        self.assertFalse(gemini_batch.is_success(state, done, obj))


    def test_a_dead_state_is_never_a_success_even_with_done_true(self) -> None:
        """An expired/cancelled job can report done=true with NO error body, which the
        `not batch_obj.get("error")` fallback read as success — so both S3-only runners
        persisted an empty `cleaned/` object for every row in the shard and marked them
        permanently done. engine's chunk driver had a private copy of this set; now the
        rule lives here, once."""
        for state in sorted(gemini_batch.FAILED_STATES):
            with self.subTest(state=state):
                obj = {"state": state, "done": True}
                self.assertTrue(gemini_batch.is_terminal(state, True))
                self.assertFalse(gemini_batch.is_success(state, True, obj),
                                 f"{state} reported as a successful shard")


class CollectResultsInlineTests(unittest.TestCase):
    def test_inline_response_yields_single_record(self) -> None:
        batch_obj = {
            "response": {
                "inlinedResponses": {
                    "inlinedResponses": [
                        {
                            "key": "batch-000001",
                            "response": {
                                "candidates": [
                                    {"content": {"parts": [{"text": '{"x":1}'}]}}
                                ],
                                "usageMetadata": {
                                    "promptTokenCount": 10,
                                    "candidatesTokenCount": 5,
                                },
                            },
                        }
                    ]
                }
            }
        }

        results = gemini_batch.collect_results(batch_obj)

        self.assertEqual(len(results), 1)
        record = results[0]
        self.assertEqual(record["key"], "batch-000001")
        self.assertEqual(record["text"], '{"x":1}')
        self.assertEqual(
            record["usage"],
            {"promptTokenCount": 10, "candidatesTokenCount": 5},
        )


class CollectResultsFileTests(unittest.TestCase):
    def test_file_result_is_downloaded_and_parsed(self) -> None:
        batch_obj = {"response": {"responsesFile": "files/out"}}
        jsonl_bytes = (
            b'{"key":"batch-000002","response":{"candidates":'
            b'[{"content":{"parts":[{"text":"{\\"y\\":2}"}]}}]}}\n'
        )

        with patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}), patch.object(
            gemini_batch, "_http_get_bytes", return_value=jsonl_bytes
        ):
            results = gemini_batch.collect_results(batch_obj)

        self.assertEqual(len(results), 1)
        record = results[0]
        self.assertEqual(record["key"], "batch-000002")
        self.assertEqual(record["text"], '{"y":2}')


class CreateBatchFileTests(unittest.TestCase):
    def test_payload_is_uploaded_as_file_and_referenced_by_name(self) -> None:
        captured: dict[str, object] = {}

        def fake_upload(jsonl_text, display_name):
            captured["jsonl_text"] = jsonl_text
            captured["display_name"] = display_name
            return "files/abc"

        def fake_post(url, body, timeout=120):
            captured["url"] = url
            captured["body"] = body
            return {"name": "batches/test"}

        with patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}), patch.object(
            gemini_batch, "upload_jsonl_file", side_effect=fake_upload
        ), patch.object(gemini_batch, "_http_post_json", side_effect=fake_post):
            create_obj = gemini_batch.create_batch(
                "gemini-2.5-flash-lite",
                [("k", {"contents": []})],
                display_name="t",
            )

        self.assertEqual(gemini_batch.batch_name_from_create(create_obj), "batches/test")

        input_config = captured["body"]["batch"]["input_config"]
        # File-API path is the ONLY path now: file_name present, no inline requests.
        self.assertEqual(input_config["file_name"], "files/abc")
        self.assertNotIn("requests", input_config)

    def test_uploaded_jsonl_uses_top_level_key_and_round_trips(self) -> None:
        """The JSONL we upload uses build_jsonl's top-level-key shape, and that key
        is what collect_results would read back via _response_key."""
        captured: dict[str, object] = {}

        def fake_upload(jsonl_text, display_name):
            captured["jsonl_text"] = jsonl_text
            return "files/abc"

        with patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}), patch.object(
            gemini_batch, "upload_jsonl_file", side_effect=fake_upload
        ), patch.object(gemini_batch, "_http_post_json", return_value={"name": "b"}):
            gemini_batch.create_batch(
                "gemini-2.5-flash-lite",
                [("batch-000007", {"contents": []})],
                display_name="t",
            )

        lines = [l for l in str(captured["jsonl_text"]).splitlines() if l.strip()]
        self.assertEqual(len(lines), 1)
        line_obj = json.loads(lines[0])
        self.assertEqual(set(line_obj.keys()), {"key", "request"})
        self.assertEqual(line_obj["key"], "batch-000007")
        self.assertEqual(gemini_batch._response_key(line_obj), "batch-000007")


class ParseJsonFromTextTests(unittest.TestCase):
    def test_fenced_json_block_is_parsed(self) -> None:
        self.assertEqual(
            gemini_batch.parse_json_from_text('```json\n{"a":1}\n```'),
            {"a": 1},
        )

    def test_non_json_returns_none(self) -> None:
        self.assertIsNone(gemini_batch.parse_json_from_text("not json"))


class HttpErrorBodyTests(unittest.TestCase):
    def test_http_error_body_is_surfaced_in_runtime_error(self) -> None:
        body = b'{"error":{"message":"Invalid JSON payload received. Unknown name \\"key\\""}}'
        err = HTTPError("http://x", 400, "Bad Request", {}, io.BytesIO(body))

        with patch.object(gemini_batch, "urlopen", side_effect=err):
            with self.assertRaises(RuntimeError) as ctx:
                gemini_batch._http_get_json("http://x")

        message = str(ctx.exception)
        self.assertIn("HTTP 400", message)
        self.assertIn("Unknown name", message)


if __name__ == "__main__":
    unittest.main()
