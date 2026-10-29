import json
import tempfile
import unittest
from pathlib import Path

from pipeline import ingest, retry_failed, status, work


class IngestionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "lake"
        self.event = Path(self.tmp.name) / "event.json"
        self.data = Path(self.tmp.name) / "stream.csv"
        self.event.write_text(json.dumps({"tenant_id":"t1","study_id":"s1","participant_id":"p1","session_id":"visit1","device_id":"d1","stream":"ecg","provider":"adapter","event_id":"evt1"}))
        self.data.write_text("timestamp,ecg\n2026-01-01T00:00:00Z,0.2\n2026-01-01T00:00:01Z,0.3\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_ingest_is_idempotent_and_worker_preserves_audit_context(self):
        first, created = ingest(self.root, self.event, self.data)
        second, created_again = ingest(self.root, self.event, self.data)
        self.assertEqual(first, second)
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(status(self.root)[0]["status"], "queued")
        result = work(self.root, 10)
        self.assertEqual(result[0]["valid_sample_rows"], 2)
        self.assertEqual(status(self.root)[0]["status"], "complete")
        self.assertTrue(Path(result[0]["normalized_path"]).exists())

    def test_invalid_timestamp_is_quarantined_as_a_bad_row_not_dropped_silently(self):
        self.data.write_text("timestamp,ecg\nnot-a-time,0.2\n2026-01-01T00:00:01Z,0.3\n")
        ingest(self.root, self.event, self.data)
        result = work(self.root, 10)[0]
        self.assertEqual(result["bad_sample_rows"], 1)
        self.assertEqual(result["valid_sample_rows"], 1)

    def test_path_identifiers_reject_traversal(self):
        event = json.loads(self.event.read_text())
        event["study_id"] = "../outside"
        self.event.write_text(json.dumps(event))
        with self.assertRaises(ValueError):
            ingest(self.root, self.event, self.data)

    def test_failed_job_can_be_requeued_after_transient_storage_recovery(self):
        ingest(self.root, self.event, self.data)
        raw = next((self.root / "raw").rglob("*.csv"))
        original = self.data.read_bytes()
        raw.unlink()
        failed = work(self.root, 10)
        self.assertIn("error", failed[0])
        self.assertEqual(status(self.root)[0]["status"], "failed")
        raw.write_bytes(original)
        self.assertEqual(retry_failed(self.root), 1)
        self.assertEqual(work(self.root, 10)[0]["valid_sample_rows"], 2)
        self.assertEqual(status(self.root)[0]["status"], "complete")


if __name__ == "__main__":
    unittest.main()
