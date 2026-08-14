"""S3-only run store: object presence is the state, and writes must be strict."""
import io
import json
import os
import unittest
from unittest import mock

from app.services.serpwow import s3_run_store as store


class FakeS3:
    """Minimal in-memory stand-in for the boto3 S3 client surface we use."""

    def __init__(self, fail_keys=()):
        self.objects: dict[str, bytes] = {}
        self.fail_keys = set(fail_keys)
        self.put_calls = 0

    def put_object(self, Bucket, Key, Body, ContentType="application/json"):
        self.put_calls += 1
        if Key in self.fail_keys:
            raise RuntimeError("S3 is down")
        self.objects[Key] = Body if isinstance(Body, bytes) else Body.encode()

    def upload_fileobj(self, Fileobj, Bucket, Key, ExtraArgs=None):
        self.put_calls += 1
        if Key in self.fail_keys:
            raise RuntimeError("S3 is down")
        Fileobj.seek(0)
        self.objects[Key] = Fileobj.read()

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise self.NoSuchKey(Key)
        return {"Body": io.BytesIO(self.objects[Key])}

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)

    def delete_objects(self, Bucket, Delete):
        deleted = []
        for entry in Delete["Objects"]:
            if entry["Key"] in self.fail_keys:
                continue        # unconfirmed, exactly like a real per-key failure
            self.objects.pop(entry["Key"], None)
            deleted.append({"Key": entry["Key"]})
        return {"Deleted": deleted}

    def get_paginator(self, _name):
        objects = self.objects

        class _P:
            def paginate(self, Bucket, Prefix):
                keys = sorted(k for k in objects if k.startswith(Prefix))
                for i in range(0, max(1, len(keys)), 2):  # force multi-page
                    yield {"Contents": [{"Key": k} for k in keys[i:i + 2]]}

        return _P()

    class NoSuchKey(Exception):
        pass


def _patched(fake):
    return mock.patch.multiple(
        store,
        _client=mock.Mock(return_value=fake),
        _bucket=mock.Mock(return_value="test-bucket"),
    )


class KeyLayoutTests(unittest.TestCase):
    def test_filenames_count_from_one_and_shard_by_thousands(self) -> None:
        # The index is 0-based in code; the FILENAME is 1-based, so the first data row
        # of input.csv is row_000001 and matches what a spreadsheet shows.
        p = "acme/relationship/run1"
        self.assertEqual(store.raw_key(p, 0), f"{p}/raw/0/row_000001.json")
        self.assertEqual(store.raw_key(p, 999), f"{p}/raw/0/row_001000.json")
        self.assertEqual(store.raw_key(p, 1000), f"{p}/raw/1/row_001001.json")
        self.assertEqual(store.raw_key(p, 499999), f"{p}/raw/499/row_500000.json")

    def test_the_key_round_trips_back_to_the_index(self) -> None:
        # list_done_rows resumes off these names: an off-by-one here re-scrapes (or
        # skips) every row of a resumed run.
        p = "acme/relationship/run1"
        for idx in (0, 1, 999, 1000, 499999):
            self.assertEqual(store._idx_from_key(store.raw_key(p, idx)), idx)
            self.assertEqual(store._idx_from_key(store.error_key(p, idx)), idx)
            self.assertEqual(store._idx_from_key(store.cleaned_key(p, idx)), idx)

    def test_a_scraped_row_is_ONE_object_holding_only_the_provider_body(self) -> None:
        """raw/ is read by hand to judge the search prompt, so nothing of ours goes in it
        — and nothing of ours needs to: the query comes back inside search_parameters, the
        index is the filename, the CSV fields are in input.csv, and credits are a constant
        per billed call."""
        fake = FakeS3()
        wire = '{"search_parameters":{"q":"prompt"},"text_blocks":[{"snippet":"hi"}]}'
        env = {"query": "prompt", "credits": 10, "request_count": 4, "error": None,
               "fields": {"y_name": "Y"}, "x_domain": "acme.com",
               "response": json.loads(wire), "response_text": wire}
        with _patched(fake):
            store.write_row("p", 0, env)
            written = sorted(fake.objects)
            raw = fake.objects["p/raw/0/row_000001.json"].decode()
            rebuilt = store.read_row("p", 0)
        # Exactly one object, and byte-for-byte what came off the wire.
        self.assertEqual(written, ["p/raw/0/row_000001.json"])
        self.assertEqual(raw, wire)
        self.assertEqual(sorted(json.loads(raw)), ["search_parameters", "text_blocks"])
        # read_row supplies the counts a billed 200 implies.
        self.assertEqual(rebuilt["response"], env["response"])
        self.assertEqual(rebuilt["credits"], store.CREDITS_PER_CALL)
        self.assertEqual(rebuilt["successful_requests"], 1)
        self.assertIsNone(rebuilt["error"])

    def test_a_dead_row_writes_our_record_and_no_raw_object(self) -> None:
        fake = FakeS3()
        with _patched(fake):
            store.write_row("p", 4, {"error": "HTTP 529", "error_category": "rate_limit",
                                     "request_count": 4, "fields": {"y_name": "Y"},
                                     "response": None, "response_text": None})
            self.assertEqual(sorted(fake.objects), ["p/errors/0/row_000005.json"])
            self.assertEqual(store.list_done_rows("p"), {4})
            self.assertEqual(store.read_row("p", 4)["error"], "HTTP 529")

    def test_error_and_cleaned_keys_share_the_shard_scheme(self) -> None:
        p = "acme/relationship/run1"
        self.assertEqual(store.error_key(p, 5), f"{p}/errors/0/row_000006.json")
        self.assertEqual(store.cleaned_key(p, 5), f"{p}/cleaned/0/row_000006.json")

    def test_run_prefix_uses_the_shared_company_slug(self) -> None:
        self.assertEqual(store.run_prefix("ISI Market Test", "abc"),
                         "isi-market-test/relationship/abc")


class StrictWriteTests(unittest.TestCase):
    def test_put_object_round_trips(self) -> None:
        fake = FakeS3()
        with _patched(fake):
            store.put_object("k", {"a": 1})
            self.assertEqual(store.get_object("k"), {"a": 1})

    def test_put_object_RAISES_on_failure(self) -> None:
        """The whole point of not using mirror_file_to_s3: with S3 as the only copy,
        a swallowed PUT failure silently loses the row's work."""
        fake = FakeS3(fail_keys={"k"})
        with _patched(fake):
            with self.assertRaises(RuntimeError):
                store.put_object("k", {"a": 1})

    def test_get_object_returns_none_for_a_missing_key(self) -> None:
        fake = FakeS3()
        with _patched(fake):
            self.assertIsNone(store.get_object("nope"))


class ResumeTests(unittest.TestCase):
    def test_list_done_rows_finds_raw_and_error_objects_across_pages(self) -> None:
        fake = FakeS3()
        p = "acme/relationship/run1"
        with _patched(fake):
            store.put_object(store.raw_key(p, 0), {})
            store.put_object(store.raw_key(p, 1500), {})
            store.put_object(store.error_key(p, 7), {})
            done = store.list_done_rows(p)
        self.assertEqual(done, {0, 1500, 7})

    def test_list_done_rows_is_empty_for_a_fresh_run(self) -> None:
        fake = FakeS3()
        with _patched(fake):
            self.assertEqual(store.list_done_rows("acme/relationship/new"), set())


class CountersTests(unittest.TestCase):
    def test_flush_is_throttled_but_force_always_writes(self) -> None:
        fake = FakeS3()
        with _patched(fake), mock.patch.dict(
                os.environ, {"RELATIONSHIP_STATUS_FLUSH_SEC": "999"}, clear=False):
            c = store.Counters("acme/relationship/run1", rows_total=10)
            c.flush(force=True)
            first = fake.put_calls
            c.bump(rows_scraped=1)
            c.flush()                      # throttled — no write
            self.assertEqual(fake.put_calls, first)
            c.flush(force=True)            # terminal snapshot always writes
            self.assertEqual(fake.put_calls, first + 1)

    def test_bump_rejects_an_unknown_counter(self) -> None:
        """It used to drop them silently, which cost gmaps a whole counter."""
        with self.assertRaises(KeyError):
            store.Counters("acme/relationship/run1").bump(rows_no_listings=1)

    def test_status_json_holds_counters_only_never_rows(self) -> None:
        fake = FakeS3()
        with _patched(fake):
            c = store.Counters("acme/relationship/run1", rows_total=500000)
            c.bump(rows_scraped=3, credits=30, requests=3)
            c.set_phase("scraping")
            c.flush(force=True)
            status = store.read_status("acme/relationship/run1")
        self.assertEqual(status["rows_total"], 500000)
        self.assertEqual(status["rows_scraped"], 3)
        self.assertEqual(status["credits"], 30)
        self.assertEqual(status["phase"], "scraping")
        self.assertNotIn("rows", status)
        # ~2KB regardless of run size is the whole point.
        self.assertLess(len(str(status)), 2048)


class StopMarkerTests(unittest.TestCase):
    def test_stop_marker_round_trips(self) -> None:
        fake = FakeS3()
        p = "acme/relationship/run1"
        with _patched(fake):
            self.assertFalse(store.stop_requested(p))
            store.request_stop(p)
            self.assertTrue(store.stop_requested(p))


class PointerTests(unittest.TestCase):
    def test_pointer_resolves_a_run_id_to_its_prefix(self) -> None:
        fake = FakeS3()
        with _patched(fake):
            store.write_run_pointer("run1", "acme/relationship/run1", "Acme")
            ptr = store.read_run_pointer("run1")
        self.assertEqual(ptr["prefix"], "acme/relationship/run1")
        self.assertEqual(ptr["company_name"], "Acme")

    def test_pointer_carries_the_supabase_run_db_id(self) -> None:
        """A relationship run has no state dict, so the pointer is where phase 3 finds
        the Supabase row to update."""
        fake = FakeS3()
        with _patched(fake):
            store.write_run_pointer("run1", "acme/relationship/run1", "Acme",
                                    run_db_id="db-42")
            self.assertEqual(store.read_run_pointer("run1")["run_db_id"], "db-42")

    def test_missing_pointer_is_none_not_an_error(self) -> None:
        fake = FakeS3()
        with _patched(fake):
            self.assertIsNone(store.read_run_pointer("nope"))


class HermeticityTests(unittest.TestCase):
    """The relationship pipeline's AI Mode client and S3 store read these credentials
    directly (see scrapedo_ai_client.py / s3_run_store.py) — tests/__init__.py
    must blank them for the whole process or a row-path test could hit live services."""

    def test_cloud_credentials_are_blanked_for_the_test_process(self) -> None:
        for key in ("SCRAPEDO_TOKEN", "S3_BUCKET", "GEMINI_API_KEY",
                    "AWS_SECRET_ACCESS_KEY"):
            self.assertEqual(os.environ.get(key, ""), "",
                             f"{key} leaked into the test process")


class BulkDeleteTests(unittest.TestCase):
    """"Rerun failed" deletes one error marker per dead row. One HTTPS call per key took
    20+ minutes on 50k rows and timed out the web request; delete_objects does 1000 a
    call. The COUNT has to be what S3 confirmed, because reporting a row as retried when
    its marker survived means the row is never rescraped."""

    def test_it_deletes_in_chunks_of_a_thousand(self) -> None:
        fake = FakeS3()
        keys = [f"p/raw/0/row_{i:06d}.error.json" for i in range(2500)]
        with _patched(fake):
            for key in keys:
                store.put_bytes(key, b"{}")
            calls = []
            original = fake.delete_objects
            fake.delete_objects = lambda Bucket, Delete: (
                calls.append(len(Delete["Objects"])) or original(Bucket=Bucket, Delete=Delete))
            deleted = store.delete_objects(keys)
        self.assertEqual(deleted, 2500)
        self.assertEqual(calls, [1000, 1000, 500])
        self.assertEqual(fake.objects, {})

    def test_an_unconfirmed_delete_is_not_counted(self) -> None:
        fake = FakeS3(fail_keys={"p/b.json"})
        with _patched(fake):
            store.put_bytes("p/a.json", b"{}")
            deleted = store.delete_objects(["p/a.json", "p/b.json"])
        # 1, not 2: b was never confirmed gone, so it must not be reported as retried.
        self.assertEqual(deleted, 1)

    def test_no_keys_makes_no_calls(self) -> None:
        fake = FakeS3()
        fake.delete_objects = lambda **_kw: self.fail("called with nothing to delete")
        with _patched(fake):
            self.assertEqual(store.delete_objects([]), 0)


if __name__ == "__main__":
    unittest.main()
