#!/usr/bin/env python3
"""Small study-aware wearable ingestion prototype with immutable raw objects."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

VERSION = "0.1.0"
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
REQUIRED = ("tenant_id", "study_id", "participant_id", "session_id", "device_id", "stream", "provider", "event_id")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_id(name: str, value: object) -> str:
    text = str(value or "")
    if not ID_PATTERN.fullmatch(text) or text in {".", ".."}:
        raise ValueError(f"{name} must be 1–80 safe identifier characters")
    return text


def connect(root: Path) -> sqlite3.Connection:
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / "catalog.sqlite3")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""CREATE TABLE IF NOT EXISTS jobs (
      job_id INTEGER PRIMARY KEY,
      provider TEXT NOT NULL, event_id TEXT NOT NULL,
      tenant_id TEXT NOT NULL, study_id TEXT NOT NULL, participant_id TEXT NOT NULL,
      session_id TEXT NOT NULL, device_id TEXT NOT NULL, stream TEXT NOT NULL,
      raw_object TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
      received_at_utc TEXT NOT NULL, status TEXT NOT NULL,
      attempts INTEGER NOT NULL DEFAULT 0, summary_json TEXT,
      UNIQUE(tenant_id, provider, event_id)
    )""")
    db.commit()
    return db


def ingest(root: Path, event_path: Path, data_path: Path) -> tuple[int, bool]:
    event = json.loads(event_path.read_text(encoding="utf-8"))
    values = {name: validate_id(name, event.get(name)) for name in REQUIRED}
    if not data_path.is_file():
        raise ValueError("payload file does not exist")
    digest = sha256_file(data_path)
    object_rel = Path("raw") / f"tenant={values['tenant_id']}" / f"study={values['study_id']}" / f"participant={values['participant_id']}" / f"session={values['session_id']}" / f"stream={values['stream']}" / f"sha256={digest}.csv"
    object_path = root / object_rel
    object_path.parent.mkdir(parents=True, exist_ok=True)
    if not object_path.exists():
        with tempfile.NamedTemporaryFile(dir=object_path.parent, delete=False) as tmp:
            temp_path = Path(tmp.name)
        try:
            shutil.copyfile(data_path, temp_path)
            temp_path.replace(object_path)
        finally:
            temp_path.unlink(missing_ok=True)

    db = connect(root)
    try:
        existing = db.execute("SELECT job_id,payload_sha256 FROM jobs WHERE tenant_id=? AND provider=? AND event_id=?", (values["tenant_id"], values["provider"], values["event_id"])).fetchone()
        if existing:
            if existing["payload_sha256"] != digest:
                raise ValueError("event id was already received with a different payload hash")
            return int(existing["job_id"]), False
        cursor = db.execute(
            """INSERT INTO jobs(provider,event_id,tenant_id,study_id,participant_id,session_id,device_id,stream,raw_object,payload_sha256,received_at_utc,status)
               VALUES(?,?,?,?,?,?,?,?,?,?,?, 'queued')""",
            (values["provider"], values["event_id"], values["tenant_id"], values["study_id"], values["participant_id"], values["session_id"], values["device_id"], values["stream"], str(object_rel), digest, datetime.now(timezone.utc).isoformat()),
        )
        db.commit()
        return int(cursor.lastrowid), True
    finally:
        db.close()


def _time_number(value: str) -> float:
    try:
        return float(value)
    except ValueError:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("ISO timestamps must include a timezone")
        return parsed.astimezone(timezone.utc).timestamp()


def process_csv(raw_path: Path, output_path: Path) -> dict:
    """Validate time-series CSV and write canonical long-form rows."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = output_path.with_suffix(output_path.suffix + ".tmp")
    records = 0
    bad_rows = 0
    duplicate_times = 0
    out_of_order = 0
    first_time = last_time = None
    previous = None
    with raw_path.open("r", newline="", encoding="utf-8-sig") as source, tmp.open("w", newline="", encoding="utf-8") as target:
        reader = csv.DictReader(source)
        if not reader.fieldnames or "timestamp" not in reader.fieldnames:
            raise ValueError("time-series CSV requires a 'timestamp' column")
        channels = [name for name in reader.fieldnames if name != "timestamp"]
        if not channels:
            raise ValueError("time-series CSV requires at least one value column")
        writer = csv.DictWriter(target, fieldnames=("sample_index", "timestamp_raw", "timestamp_epoch_s", "channel", "value"))
        writer.writeheader()
        for index, row in enumerate(reader):
            try:
                timestamp = _time_number(str(row.get("timestamp", "")))
                row_values = [(channel, float(row[channel])) for channel in channels]
                if not all(math.isfinite(value) for _, value in row_values):
                    raise ValueError("non-finite sensor value")
            except (KeyError, TypeError, ValueError):
                bad_rows += 1
                continue
            if previous is not None:
                if timestamp == previous:
                    duplicate_times += 1
                elif timestamp < previous:
                    out_of_order += 1
            previous = timestamp
            first_time = timestamp if first_time is None else first_time
            last_time = timestamp
            for channel, value in row_values:
                writer.writerow({"sample_index": index, "timestamp_raw": row["timestamp"], "timestamp_epoch_s": timestamp, "channel": channel, "value": value})
            records += 1
    tmp.replace(output_path)
    return {"input_sample_rows": records + bad_rows, "valid_sample_rows": records,
            "bad_sample_rows": bad_rows, "channel_count": len(channels),
            "duplicate_timestamps": duplicate_times, "out_of_order_timestamps": out_of_order,
            "first_timestamp_epoch_s": first_time, "last_timestamp_epoch_s": last_time,
            "normalized_path": str(output_path)}


def work(root: Path, limit: int) -> list[dict]:
    db = connect(root)
    completed = []
    try:
        jobs = db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY job_id LIMIT ?", (limit,)).fetchall()
        for job in jobs:
            db.execute("UPDATE jobs SET status='processing', attempts=attempts+1 WHERE job_id=?", (job["job_id"],))
            db.commit()
            try:
                raw = root / job["raw_object"]
                if sha256_file(raw) != job["payload_sha256"]:
                    raise ValueError("immutable raw object hash mismatch")
                out = root / "normalized" / f"tenant={job['tenant_id']}" / f"study={job['study_id']}" / f"participant={job['participant_id']}" / f"session={job['session_id']}" / f"stream={job['stream']}" / f"event={job['event_id']}.csv"
                summary = process_csv(raw, out)
                summary.update({"job_id": job["job_id"], "payload_sha256": job["payload_sha256"], "pipeline_version": VERSION})
                db.execute("UPDATE jobs SET status='complete',summary_json=? WHERE job_id=?", (json.dumps(summary, sort_keys=True), job["job_id"]))
                completed.append(summary)
            except Exception as exc:
                summary = {"job_id": job["job_id"], "error": str(exc), "pipeline_version": VERSION}
                db.execute("UPDATE jobs SET status='failed',summary_json=? WHERE job_id=?", (json.dumps(summary, sort_keys=True), job["job_id"]))
                completed.append(summary)
            db.commit()
        return completed
    finally:
        db.close()


def status(root: Path) -> list[dict]:
    db = connect(root)
    try:
        return [dict(row) for row in db.execute("SELECT job_id,provider,event_id,study_id,participant_id,session_id,stream,status,attempts,payload_sha256 FROM jobs ORDER BY job_id")]
    finally:
        db.close()


def retry_failed(root: Path, job_id: int | None = None) -> int:
    db = connect(root)
    try:
        if job_id is None:
            cursor = db.execute("UPDATE jobs SET status='queued',summary_json=NULL WHERE status='failed'")
        else:
            cursor = db.execute("UPDATE jobs SET status='queued',summary_json=NULL WHERE status='failed' AND job_id=?", (job_id,))
        db.commit()
        return int(cursor.rowcount)
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p_ingest = sub.add_parser("ingest", help="store an event payload immutably and enqueue it")
    p_ingest.add_argument("--root", type=Path, required=True)
    p_ingest.add_argument("--event", type=Path, required=True)
    p_ingest.add_argument("--data", type=Path, required=True)
    p_work = sub.add_parser("work", help="process queued payloads with the local demo worker")
    p_work.add_argument("--root", type=Path, required=True)
    p_work.add_argument("--limit", type=int, default=100)
    p_status = sub.add_parser("status", help="list ingestion jobs")
    p_status.add_argument("--root", type=Path, required=True)
    p_retry = sub.add_parser("retry", help="requeue failed jobs after correcting the cause")
    p_retry.add_argument("--root", type=Path, required=True)
    p_retry.add_argument("--job-id", type=int, default=None)
    args = parser.parse_args(argv)
    try:
        if args.command == "ingest":
            job_id, created = ingest(args.root, args.event, args.data)
            print(json.dumps({"job_id": job_id, "created": created}))
        elif args.command == "work":
            print(json.dumps(work(args.root, args.limit), indent=2))
        elif args.command == "status":
            print(json.dumps(status(args.root), indent=2))
        else:
            print(json.dumps({"requeued": retry_failed(args.root, args.job_id)}))
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"wearable-ingestion: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
