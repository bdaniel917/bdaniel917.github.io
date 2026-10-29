import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

from physioqc import (Config, LiveWindowSink, SharedMemoryRing, append_annotation,
                      compute_metrics, consume_live_lines, evaluate_records,
                      main, run_qc)


def config(**overrides):
    values = dict(sample_rate_hz=250.0, window_seconds=1.0, mains_hz=60.0,
                  line_bandwidth_hz=1.0, flatline_epsilon=0.0, clip_abs=1.0,
                  signal_band_low_hz=None, signal_band_high_hz=None,
                  max_flatline_fraction=0.1, max_line_noise_ratio=0.2,
                  max_burst_rate_per_min=12.0, max_clip_fraction=0.0)
    values.update(overrides)
    return Config(**values)


class MetricTests(unittest.TestCase):
    def test_flatline_is_detected_and_missing_values_are_counted(self):
        metrics = compute_metrics([0, 0, 0, float("nan"), 0], config())
        self.assertEqual(metrics["flatline_fraction"], 1.0)
        self.assertAlmostEqual(metrics["missing_fraction"], 0.2)

    def test_clipping_fraction_uses_explicit_threshold(self):
        metrics = compute_metrics([0.1, 0.2, 1.0, -1.2], config(clip_abs=1.0))
        self.assertEqual(metrics["clip_fraction"], 0.5)

    def test_line_frequency_energy_is_separated(self):
        t = np.arange(2500) / 250
        x = np.sin(2 * np.pi * 10 * t) + 0.5 * np.sin(2 * np.pi * 60 * t)
        metrics = compute_metrics(x.tolist(), config(window_seconds=10.0))
        self.assertGreater(metrics["line_noise_ratio"], 0.15)


class PipelineTests(unittest.TestCase):
    def test_run_writes_contextual_records_per_window_and_channel(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "samples.csv"
            source.write_text("timestamp_s,a,b\n0,0,1\n0.01,0,2\n0.02,1,3\n0.03,1,4\n", encoding="utf-8")
            output = root / "qc.jsonl"
            with output.open("w", encoding="utf-8") as handle:
                count = run_qc(source, handle, config(sample_rate_hz=100, window_seconds=0.02))
            records = [json.loads(line) for line in output.read_text().splitlines()]
            self.assertEqual(count, 4)
            self.assertEqual([r["channel"] for r in records], ["a", "b", "a", "b"])
            self.assertTrue(all(r["source_sha256"] for r in records))
            self.assertTrue(all("quality_flags" in r and "metrics" in r for r in records))


class ExtensionTests(unittest.TestCase):
    def test_bids_sidecar_supplies_profile_sample_rate_and_mains(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "sub-01_task-rest_eeg.csv"
            source.write_text("Cz\n0\n1\n0\n1\n", encoding="utf-8")
            source.with_suffix(".json").write_text(json.dumps({
                "SamplingFrequency": 250,
                "PowerLineFrequency": 50,
                "EEGChannelCount": 1,
            }), encoding="utf-8")
            output = root / "qc.jsonl"
            self.assertEqual(main(["run", str(source), "--output", str(output), "--window-seconds", ".02"]), 0)
            record = json.loads(output.read_text().splitlines()[0])
            self.assertEqual(record["sample_rate_hz"], 250)
            self.assertEqual(record["config"]["mains_hz"], 50)
            self.assertEqual(record["config"]["profile_name"], "eeg")
            self.assertEqual(record["config"]["profile_validation_status"], "exploratory_unvalidated")
            self.assertTrue(record["config"]["bids_sidecar_sha256"])

    def test_live_ndjson_emits_window_records(self):
        sink_output = io.StringIO()
        sink = LiveWindowSink(sink_output, config(sample_rate_hz=2, window_seconds=1), "test")
        count = consume_live_lines(io.StringIO(
            '{"timestamp_s":0,"channels":{"ECG":0.0}}\n'
            '{"timestamp_s":0.5,"channels":{"ECG":0.1}}\n'
            '{"timestamp_s":1,"channels":{"ECG":0.2}}\n'), sink)
        sink.finish()
        records = [json.loads(line) for line in sink_output.getvalue().splitlines()]
        self.assertEqual(count, 3)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["sample_end_exclusive"], 2)
        self.assertEqual(records[1]["sample_end_exclusive"], 3)

    def test_tcp_listener_accepts_a_stream_and_flushes_partial_windows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            probe = socket.socket()
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
            probe.close()
            output = root / "live.jsonl"
            proc = subprocess.Popen([sys.executable, "physioqc.py", "listen",
                "--transport", "tcp", "--address", f"127.0.0.1:{port}",
                "--output", str(output), "--sample-rate", "250", "--window-seconds", ".008"],
                cwd=Path(__file__).resolve().parents[1], stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
            client = None
            try:
                deadline = time.time() + 3
                while time.time() < deadline:
                    try:
                        client = socket.create_connection(("127.0.0.1", port), timeout=.2)
                        break
                    except OSError:
                        if proc.poll() is not None:
                            self.fail("TCP listener exited before accepting a client")
                        time.sleep(.03)
                self.assertIsNotNone(client)
                client.sendall((
                    '{"timestamp_s":0,"channels":{"ECG":0.1}}\n'
                    '{"timestamp_s":0.5,"channels":{"ECG":0.2}}\n'
                    '{"timestamp_s":1,"channels":{"ECG":0.3}}\n'
                ).encode())
                client.shutdown(socket.SHUT_WR)
                client.close()
                deadline = time.time() + 3
                while time.time() < deadline and (not output.exists() or len(output.read_text().splitlines()) < 2):
                    time.sleep(.03)
                records = [json.loads(line) for line in output.read_text().splitlines()]
                self.assertEqual(len(records), 2)
                self.assertEqual(records[1]["sample_end_exclusive"], 3)
            finally:
                if client is not None:
                    client.close()
                proc.terminate()
                proc.wait(timeout=2)

    def test_named_shared_memory_ring_publishes_and_reports_overrun(self):
        name = f"physioqc_test_{os.getpid()}_{time.time_ns()}"
        producer = SharedMemoryRing.create(name, ["ECG"], 2)
        consumer = SharedMemoryRing.attach(name, ["ECG"])
        try:
            producer.write(0.0, [1.0])
            producer.write(0.5, [2.0])
            frames, cursor, skipped = consumer.read_from(0)
            self.assertEqual(skipped, 0)
            self.assertEqual(cursor, 2)
            self.assertEqual(frames[1][1]["ECG"], 2.0)
            producer.write(1.0, [3.0])
            producer.write(1.5, [4.0])
            frames, cursor, skipped = consumer.read_from(0)
            self.assertEqual(skipped, 2)
            self.assertEqual([frame[1]["ECG"] for frame in frames], [3.0, 4.0])
        finally:
            consumer.close()
            producer.block.close()
            producer.block.unlink()

    def test_evaluation_reports_window_level_confusion_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            qc = root / "qc.jsonl"
            qc.write_text("\n".join(json.dumps({"record_type": "window_qc", "window_id": wid,
                "event_candidate": flagged}) for wid, flagged in (("a", True), ("b", False), ("c", True))) + "\n")
            truth = root / "truth.csv"
            truth.write_text("window_id,label,fault_type\na,artifact,flatline\nb,usable,clean\nc,usable,clean\n", encoding="utf-8")
            report = evaluate_records(qc, truth)
            self.assertEqual(report["overall"]["true_positive"], 1)
            self.assertEqual(report["overall"]["false_positive"], 1)
            self.assertEqual(report["overall"]["true_negative"], 1)
            self.assertFalse(report["clinical_validation"])

    def test_annotation_writer_appends_separate_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "nested" / "annotations.jsonl"
            append_annotation(output, "window-1", "review", "reviewer-a", "inspect edge")
            record = json.loads(output.read_text())
            self.assertEqual(record["window_id"], "window-1")
            self.assertEqual(record["label"], "review")
            self.assertEqual(record["note"], "inspect edge")

    def test_daemon_processes_a_stable_csv_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inbox, out = root / "inbox", root / "out"
            inbox.mkdir()
            source = inbox / "study.csv"
            source.write_text("ecg\n0.1\n0.2\n0.1\n0.2\n", encoding="utf-8")
            proc = subprocess.Popen([sys.executable, "physioqc.py", "daemon",
                "--input-dir", str(inbox), "--output-dir", str(out),
                "--state-file", str(root / "state.json"), "--sample-rate", "250",
                "--window-seconds", ".02", "--poll-seconds", ".05"],
                cwd=Path(__file__).resolve().parents[1], stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
            try:
                deadline = time.time() + 4
                while time.time() < deadline and not list(out.glob("*.jsonl")):
                    time.sleep(.05)
                outputs = list(out.glob("*.jsonl"))
                self.assertEqual(len(outputs), 1)
                time.sleep(.15)
                self.assertEqual(len(list(out.glob("*.jsonl"))), 1)
            finally:
                proc.terminate()
                proc.wait(timeout=2)


if __name__ == "__main__":
    unittest.main()
