#!/usr/bin/env python3
"""Windowed, explainable quality checks for physiological CSV recordings."""
from __future__ import annotations

import argparse
import csv
import curses
import hashlib
import json
import math
import os
import socket
import struct
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from multiprocessing import shared_memory
from typing import Iterator, TextIO

import numpy as np

VERSION = "0.3.0"


@dataclass(frozen=True)
class Config:
    sample_rate_hz: float
    window_seconds: float
    mains_hz: float
    line_bandwidth_hz: float
    flatline_epsilon: float
    clip_abs: float | None
    signal_band_low_hz: float | None
    signal_band_high_hz: float | None
    max_flatline_fraction: float
    max_line_noise_ratio: float
    max_burst_rate_per_min: float
    max_clip_fraction: float
    profile_name: str = "generic"
    profile_validation_status: str = "exploratory_unvalidated"
    bids_sidecar_file: str | None = None
    bids_sidecar_sha256: str | None = None


PROFILES = {
    "generic": dict(sample_rate_hz=250.0, mains_hz=60.0, line_bandwidth_hz=1.0,
                    flatline_epsilon=0.0, signal_band_low_hz=None, signal_band_high_hz=None,
                    max_flatline_fraction=0.1, max_line_noise_ratio=0.2,
                    max_burst_rate_per_min=12.0, max_clip_fraction=0.0),
    "eeg": dict(sample_rate_hz=250.0, mains_hz=60.0, line_bandwidth_hz=1.0,
                flatline_epsilon=1e-6, signal_band_low_hz=0.5, signal_band_high_hz=40.0,
                max_flatline_fraction=0.1, max_line_noise_ratio=0.2,
                max_burst_rate_per_min=12.0, max_clip_fraction=0.0),
    "ecg": dict(sample_rate_hz=250.0, mains_hz=60.0, line_bandwidth_hz=1.0,
                flatline_epsilon=1e-6, signal_band_low_hz=0.5, signal_band_high_hz=40.0,
                max_flatline_fraction=0.1, max_line_noise_ratio=0.2,
                max_burst_rate_per_min=12.0, max_clip_fraction=0.0),
    "emg": dict(sample_rate_hz=1000.0, mains_hz=60.0, line_bandwidth_hz=2.0,
                flatline_epsilon=1e-6, signal_band_low_hz=20.0, signal_band_high_hz=450.0,
                max_flatline_fraction=0.1, max_line_noise_ratio=0.25,
                max_burst_rate_per_min=30.0, max_clip_fraction=0.0),
    "psg": dict(sample_rate_hz=200.0, mains_hz=60.0, line_bandwidth_hz=1.0,
                flatline_epsilon=1e-6, signal_band_low_hz=0.3, signal_band_high_hz=35.0,
                max_flatline_fraction=0.1, max_line_noise_ratio=0.2,
                max_burst_rate_per_min=20.0, max_clip_fraction=0.0),
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _finite(values: np.ndarray) -> np.ndarray:
    return values[np.isfinite(values)]


def compute_metrics(values: list[float], config: Config) -> dict[str, float | int | None]:
    x = np.asarray(values, dtype=float)
    finite = _finite(x)
    missing_fraction = 1.0 - (finite.size / max(x.size, 1))
    if finite.size == 0:
        return {
            "sample_count": int(x.size), "missing_fraction": 1.0, "rms": None,
            "peak_to_peak": None, "flatline_fraction": None, "drift_per_second": None,
            "line_noise_ratio": None, "burst_rate_per_min": None,
            "burst_count": None, "clip_fraction": None, "kurtosis": None,
            "band_power_ratio_db": None,
        }

    center = float(np.median(finite))
    residual = finite - center
    rms = float(np.sqrt(np.mean(np.square(finite))))
    peak_to_peak = float(np.max(finite) - np.min(finite))

    diffs = np.diff(finite)
    flatline_fraction = float(np.mean(np.abs(diffs) <= config.flatline_epsilon)) if diffs.size else 0.0

    # Least-squares slope, expressed in signal units per second.
    t = np.arange(finite.size, dtype=float) / config.sample_rate_hz
    centered_t = t - float(np.mean(t))
    denom = float(np.dot(centered_t, centered_t))
    drift = float(np.dot(centered_t, finite - np.mean(finite)) / denom) if denom > 0 else 0.0

    # Robust outlier candidates: count threshold crossings, not just high samples.
    mad = float(np.median(np.abs(residual)))
    threshold = max(6.0 * 1.4826 * mad, config.flatline_epsilon, np.finfo(float).eps)
    above = np.abs(residual) > threshold
    burst_count = int(np.count_nonzero(above & ~np.r_[False, above[:-1]]))
    duration_min = max(x.size / config.sample_rate_hz / 60.0, 1e-12)
    burst_rate = float(burst_count / duration_min)

    clip_fraction = None
    if config.clip_abs is not None:
        clip_fraction = float(np.mean(np.abs(finite) >= config.clip_abs))

    variance = float(np.mean(np.square(residual)))
    kurtosis = float(np.mean(np.power(residual, 4)) / (variance * variance)) if variance > 0 else 0.0

    line_noise_ratio = None
    band_power_ratio_db = None
    if finite.size >= 8:
        centered = finite - float(np.mean(finite))
        taper = np.hanning(finite.size)
        spectrum = np.abs(np.fft.rfft(centered * taper)) ** 2
        freqs = np.fft.rfftfreq(finite.size, d=1.0 / config.sample_rate_hz)
        total_power = float(np.sum(spectrum[1:]))
        if total_power > 0:
            line_mask = np.abs(freqs - config.mains_hz) <= config.line_bandwidth_hz
            line_mask[0] = False
            line_noise_ratio = float(np.sum(spectrum[line_mask]) / total_power)
            if config.signal_band_low_hz is not None and config.signal_band_high_hz is not None:
                signal_mask = (freqs >= config.signal_band_low_hz) & (freqs <= config.signal_band_high_hz)
                signal_mask[0] = False
                in_band = float(np.sum(spectrum[signal_mask]))
                out_band = max(total_power - in_band, np.finfo(float).tiny)
                band_power_ratio_db = float(10.0 * math.log10(max(in_band, np.finfo(float).tiny) / out_band))

    return {
        "sample_count": int(x.size), "missing_fraction": float(missing_fraction),
        "rms": rms, "peak_to_peak": peak_to_peak,
        "flatline_fraction": flatline_fraction, "drift_per_second": drift,
        "line_noise_ratio": line_noise_ratio, "burst_rate_per_min": burst_rate,
        "burst_count": burst_count, "clip_fraction": clip_fraction,
        "kurtosis": kurtosis, "band_power_ratio_db": band_power_ratio_db,
    }


def _windows(path: Path, window_samples: int) -> tuple[list[str], Iterator[tuple[int, list[str | None], dict[str, list[float]]]]]:
    """Yield bounded windows, keeping memory proportional to one window."""
    handle = path.open("r", newline="", encoding="utf-8-sig")
    reader = csv.DictReader(handle)
    if not reader.fieldnames:
        handle.close()
        raise ValueError("CSV needs a header row")
    time_column = next((name for name in ("timestamp", "time", "timestamp_s") if name in reader.fieldnames), None)
    channels = [name for name in reader.fieldnames if name != time_column]
    if not channels:
        handle.close()
        raise ValueError("CSV needs at least one numeric channel column")

    def generate() -> Iterator[tuple[int, list[str | None], dict[str, list[float]]]]:
        start = 0
        timestamps: list[str | None] = []
        samples = {name: [] for name in channels}
        try:
            for row in reader:
                timestamps.append(row.get(time_column) if time_column else None)
                for name in channels:
                    try:
                        samples[name].append(float(row[name]))
                    except (TypeError, ValueError):
                        samples[name].append(float("nan"))
                if len(timestamps) >= window_samples:
                    yield start, timestamps, samples
                    start += len(timestamps)
                    timestamps, samples = [], {name: [] for name in channels}
            if timestamps:
                yield start, timestamps, samples
        finally:
            handle.close()

    return channels, generate()


def _flag_reasons(metrics: dict[str, float | int | None], config: Config) -> list[str]:
    flags: list[str] = []
    value = metrics.get("flatline_fraction")
    if value is not None and value > config.max_flatline_fraction:
        flags.append("flatline_fraction_high")
    value = metrics.get("line_noise_ratio")
    if value is not None and value > config.max_line_noise_ratio:
        flags.append("line_noise_ratio_high")
    value = metrics.get("burst_rate_per_min")
    if value is not None and value > config.max_burst_rate_per_min:
        flags.append("burst_rate_high")
    value = metrics.get("clip_fraction")
    if value is not None and value > config.max_clip_fraction:
        flags.append("clipping_detected")
    value = metrics.get("missing_fraction")
    if value is not None and value > 0:
        flags.append("missing_or_non_numeric_samples")
    return flags


def run_qc(input_path: Path, output: TextIO, config: Config) -> int:
    window_samples = max(1, int(round(config.sample_rate_hz * config.window_seconds)))
    file_hash = sha256_file(input_path)
    config_record = asdict(config)
    config_hash = hashlib.sha256(json.dumps(config_record, sort_keys=True).encode()).hexdigest()
    channels, windows = _windows(input_path, window_samples)
    count = 0
    for window_index, (sample_start, timestamps, samples) in enumerate(windows):
        sample_end = sample_start + len(timestamps)
        for channel in channels:
            metrics = compute_metrics(samples[channel], config)
            flags = _flag_reasons(metrics, config)
            record = {
                "schema_version": "1.1", "record_type": "window_qc",
                "window_id": f"{file_hash[:12]}-{window_index:06d}-{channel}",
                "source_file": input_path.name, "source_sha256": file_hash,
                "channel": channel, "sample_rate_hz": config.sample_rate_hz,
                "sample_start": sample_start, "sample_end_exclusive": sample_end,
                "window_start": timestamps[0] if timestamps else None,
                "window_end": timestamps[-1] if timestamps else None,
                "window_seconds_nominal": len(timestamps) / config.sample_rate_hz,
                "config": config_record, "config_sha256": config_hash,
                "physioqc_version": VERSION, "computed_at_utc": datetime.now(timezone.utc).isoformat(),
                "metrics": metrics, "quality_flags": flags,
                "event_candidate": bool(flags),
            }
            output.write(json.dumps(record, allow_nan=False) + "\n")
            count += 1
    return count


def export_parquet(input_path: Path, output_path: Path, batch_size: int = 512) -> None:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise RuntimeError("Parquet export requires optional dependency pyarrow") from exc
    # Read JSONL in bounded batches to avoid materializing a study in memory.
    schema = pa.schema([
        ("schema_version", pa.string()), ("record_type", pa.string()), ("window_id", pa.string()),
        ("source_file", pa.string()), ("source_sha256", pa.string()), ("channel", pa.string()),
        ("sample_rate_hz", pa.float64()), ("sample_start", pa.int64()), ("sample_end_exclusive", pa.int64()),
        ("window_start", pa.string()), ("window_end", pa.string()), ("window_seconds_nominal", pa.float64()),
        ("config_json", pa.string()), ("config_sha256", pa.string()), ("physioqc_version", pa.string()),
        ("computed_at_utc", pa.string()), ("quality_flags", pa.list_(pa.string())), ("event_candidate", pa.bool_()),
        ("sample_count", pa.int64()), ("missing_fraction", pa.float64()), ("rms", pa.float64()),
        ("peak_to_peak", pa.float64()), ("flatline_fraction", pa.float64()), ("drift_per_second", pa.float64()),
        ("line_noise_ratio", pa.float64()), ("burst_rate_per_min", pa.float64()), ("burst_count", pa.int64()),
        ("clip_fraction", pa.float64()), ("kurtosis", pa.float64()), ("band_power_ratio_db", pa.float64()),
    ])
    writer = None
    batch: list[dict] = []
    try:
        with input_path.open(encoding="utf-8") as handle:
            for line in handle:
                source = json.loads(line)
                row = {key: value for key, value in source.items() if key not in {"config", "metrics"}}
                row["config_json"] = json.dumps(source.get("config", {}), sort_keys=True)
                row.update(source.get("metrics", {}))
                batch.append(row)
                if len(batch) >= batch_size:
                    table = pa.Table.from_pylist(batch, schema=schema)
                    if writer is None:
                        writer = pq.ParquetWriter(output_path, schema, compression="zstd")
                    writer.write_table(table)
                    batch.clear()
        if batch:
            table = pa.Table.from_pylist(batch, schema=schema)
            if writer is None:
                writer = pq.ParquetWriter(output_path, schema, compression="zstd")
            writer.write_table(table)
    finally:
        if writer is not None:
            writer.close()
    if writer is None:
        raise ValueError("Input JSONL contained no QC records")


def add_run_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("input", type=Path, help="CSV with one row per sample; optional timestamp column")
    parser.add_argument("--output", type=Path, required=True, help="JSONL output path")
    parser.add_argument("--sample-rate", type=float, default=None, help="Sampling rate in Hz; defaults to BIDS metadata or the selected profile")
    parser.add_argument("--window-seconds", type=float, default=None)
    parser.add_argument("--profile", choices=("auto", *PROFILES), default="auto", help="Exploratory modality starting point; thresholds are not clinically validated")
    parser.add_argument("--bids-sidecar", type=Path, default=None, help="BIDS JSON sidecar; matching input.json is discovered automatically")
    parser.add_argument("--no-bids-sidecar", action="store_true", help="Disable automatic sidecar discovery")
    parser.add_argument("--mains-hz", type=float, choices=(50.0, 60.0), default=None)
    parser.add_argument("--line-bandwidth-hz", type=float, default=None)
    parser.add_argument("--flatline-epsilon", type=float, default=None)
    parser.add_argument("--clip-abs", type=float, default=None, help="Absolute amplitude at or above which samples count as clipped")
    parser.add_argument("--signal-band", type=str, default=None, help="Optional low,high Hz band for a band-power ratio proxy")
    parser.add_argument("--no-signal-band", action="store_true", help="Disable the profile's optional band-power ratio")
    parser.add_argument("--max-flatline-fraction", type=float, default=None)
    parser.add_argument("--max-line-noise-ratio", type=float, default=None)
    parser.add_argument("--max-burst-rate-per-min", type=float, default=None)
    parser.add_argument("--max-clip-fraction", type=float, default=None)


def _load_bids_sidecar(input_path: Path, explicit: Path | None, disabled: bool) -> tuple[Path | None, dict]:
    if disabled:
        return None, {}
    candidate = explicit if explicit is not None else input_path.with_suffix(".json")
    if not candidate.exists():
        if explicit is not None:
            raise ValueError(f"BIDS sidecar not found: {candidate}")
        return None, {}
    try:
        metadata = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read BIDS sidecar {candidate}: {exc}") from exc
    if not isinstance(metadata, dict):
        raise ValueError("BIDS sidecar must contain a JSON object")
    return candidate, metadata


def _infer_profile(input_path: Path, metadata: dict) -> str:
    name = input_path.stem.lower()
    for profile in ("eeg", "ecg", "emg", "psg"):
        if name.endswith("_" + profile):
            return profile
        key = profile.upper() + "ChannelCount"
        if metadata.get(key, 0):
            return profile
    columns = [str(c).lower() for c in metadata.get("Columns", [])]
    joined = " ".join(columns)
    for profile, markers in (("eeg", ("eeg",)), ("ecg", ("ecg", "ekg")),
                             ("emg", ("emg",)), ("psg", ("resp", "spo2", "eog", "chin"))):
        if any(marker in joined for marker in markers):
            return profile
    return "generic"


def make_config(args: argparse.Namespace) -> Config:
    sidecar_path, metadata = _load_bids_sidecar(args.input, args.bids_sidecar, args.no_bids_sidecar)
    profile = _infer_profile(args.input, metadata) if args.profile == "auto" else args.profile
    defaults = dict(PROFILES[profile])
    sidecar_rate = metadata.get("SamplingFrequency")
    if sidecar_rate is not None:
        try:
            defaults["sample_rate_hz"] = float(sidecar_rate)
        except (TypeError, ValueError) as exc:
            raise ValueError("BIDS SamplingFrequency must be numeric") from exc
    sidecar_mains = metadata.get("PowerLineFrequency")
    if sidecar_mains not in (None, "n/a", "n/a "):
        try:
            defaults["mains_hz"] = float(sidecar_mains)
        except (TypeError, ValueError) as exc:
            raise ValueError("BIDS PowerLineFrequency must be numeric or 'n/a'") from exc

    sample_rate = args.sample_rate if args.sample_rate is not None else defaults["sample_rate_hz"]
    low, high = defaults["signal_band_low_hz"], defaults["signal_band_high_hz"]
    if args.no_signal_band:
        low = high = None
    if args.signal_band:
        parts = args.signal_band.split(",")
        if len(parts) != 2:
            raise ValueError("--signal-band must be LOW,HIGH")
        low, high = map(float, parts)
    if args.profile == "auto":
        profile_name = profile
    else:
        profile_name = args.profile
    if args.no_signal_band and args.signal_band:
        raise ValueError("use either --no-signal-band or --signal-band, not both")
    if low is not None and (low < 0 or high <= low or high >= sample_rate / 2):
        raise ValueError(f"signal band must satisfy 0 <= low < high < Nyquist ({sample_rate / 2:g} Hz)")
    window_seconds = args.window_seconds if args.window_seconds is not None else 10.0
    if sample_rate <= 0 or window_seconds <= 0:
        raise ValueError("sample rate and window duration must be positive")
    if args.mains_hz is not None and args.mains_hz >= sample_rate / 2:
        raise ValueError("mains frequency must be below Nyquist")
    sidecar_hash = sha256_file(sidecar_path) if sidecar_path else None
    config = Config(
        sample_rate_hz=sample_rate, window_seconds=window_seconds,
        mains_hz=args.mains_hz if args.mains_hz is not None else defaults["mains_hz"],
        line_bandwidth_hz=args.line_bandwidth_hz if args.line_bandwidth_hz is not None else defaults["line_bandwidth_hz"],
        flatline_epsilon=args.flatline_epsilon if args.flatline_epsilon is not None else defaults["flatline_epsilon"],
        clip_abs=args.clip_abs,
        signal_band_low_hz=low, signal_band_high_hz=high,
        max_flatline_fraction=args.max_flatline_fraction if args.max_flatline_fraction is not None else defaults["max_flatline_fraction"],
        max_line_noise_ratio=args.max_line_noise_ratio if args.max_line_noise_ratio is not None else defaults["max_line_noise_ratio"],
        max_burst_rate_per_min=args.max_burst_rate_per_min if args.max_burst_rate_per_min is not None else defaults["max_burst_rate_per_min"],
        max_clip_fraction=args.max_clip_fraction if args.max_clip_fraction is not None else defaults["max_clip_fraction"],
        profile_name=profile_name, profile_validation_status="exploratory_unvalidated",
        bids_sidecar_file=sidecar_path.name if sidecar_path else None,
        bids_sidecar_sha256=sidecar_hash,
    )
    numeric = [config.sample_rate_hz, config.window_seconds, config.mains_hz,
               config.line_bandwidth_hz, config.flatline_epsilon,
               config.max_flatline_fraction, config.max_line_noise_ratio,
               config.max_burst_rate_per_min, config.max_clip_fraction]
    if not all(math.isfinite(value) for value in numeric):
        raise ValueError("configuration values must be finite")
    if config.line_bandwidth_hz <= 0 or config.flatline_epsilon < 0 or config.max_burst_rate_per_min < 0:
        raise ValueError("line bandwidth must be positive; flatline epsilon and burst threshold must be nonnegative")
    if not 0 <= config.max_flatline_fraction <= 1 or not 0 <= config.max_line_noise_ratio <= 1 or not 0 <= config.max_clip_fraction <= 1:
        raise ValueError("fraction thresholds must lie between 0 and 1")
    if config.clip_abs is not None and (not math.isfinite(config.clip_abs) or config.clip_abs <= 0):
        raise ValueError("clip threshold must be a positive finite value")
    if config.mains_hz <= 0 or config.mains_hz >= config.sample_rate_hz / 2:
        raise ValueError("mains frequency must be positive and below Nyquist")
    return config


def append_annotation(output_path: Path, window_id: str, label: str, reviewer: str, note: str = "") -> None:
    if label not in {"usable", "artifact", "review"}:
        raise ValueError("label must be usable, artifact, or review")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    record = {"schema_version": "1.1", "record_type": "human_annotation",
              "window_id": window_id, "label": label, "reviewer": reviewer,
              "note": note, "annotated_at_utc": datetime.now(timezone.utc).isoformat()}
    with output_path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(record) + "\n")


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def evaluate_records(qc_path: Path, truth_path: Path) -> dict:
    """Compare candidate event flags with a CSV of independent window labels."""
    qc_by_id = {}
    with qc_path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                record = json.loads(line)
                if record.get("record_type") == "window_qc":
                    qc_by_id[record["window_id"]] = bool(record.get("event_candidate"))
    groups: dict[str, dict[str, int]] = {}
    with truth_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        required = {"window_id", "label"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError("truth CSV requires window_id,label columns")
        for row in reader:
            label = (row.get("label") or "").strip().lower()
            if label == "review":
                continue
            if label not in {"artifact", "usable"}:
                raise ValueError(f"unsupported truth label {label!r}; use artifact, usable, or review")
            window_id = row["window_id"]
            if window_id not in qc_by_id:
                raise ValueError(f"truth window_id not found in QC output: {window_id}")
            group = (row.get("fault_type") or "all").strip() or "unspecified"
            counts = groups.setdefault(group, {"tp": 0, "fp": 0, "tn": 0, "fn": 0})
            predicted = qc_by_id[window_id]
            actual = label == "artifact"
            key = "tp" if actual and predicted else "fn" if actual else "fp" if predicted else "tn"
            counts[key] += 1
    if not groups:
        raise ValueError("truth CSV has no evaluable rows")

    def summarize(counts: dict[str, int]) -> dict:
        tp, fp, tn, fn = (counts[k] for k in ("tp", "fp", "tn", "fn"))
        precision = _ratio(tp, tp + fp)
        recall = _ratio(tp, tp + fn)
        specificity = _ratio(tn, tn + fp)
        f1 = (2 * precision * recall / (precision + recall)) if precision is not None and recall is not None and precision + recall else None
        return {"n": tp + fp + tn + fn, "true_positive": tp, "false_positive": fp,
                "true_negative": tn, "false_negative": fn, "precision": precision,
                "recall": recall, "specificity": specificity, "f1": f1}

    overall_counts = {key: sum(group[key] for group in groups.values()) for key in ("tp", "fp", "tn", "fn")}
    return {"evaluation": "window_level_candidate_flags", "clinical_validation": False,
            "qc_file": qc_path.name, "truth_file": truth_path.name,
            "overall": summarize(overall_counts),
            "by_fault_type": {name: summarize(counts) for name, counts in sorted(groups.items())}}


class SharedMemoryRing:
    """Single-writer ring: each slot is published with an odd/even sequence marker."""
    MAGIC = b"PHYQCR01"
    HEADER = struct.Struct("<8sIIQQ")

    def __init__(self, block: shared_memory.SharedMemory, channels: list[str], capacity: int):
        self.block, self.channels, self.capacity = block, channels, capacity
        self.slot_size = 16 + 8 * len(channels)

    @classmethod
    def create(cls, name: str, channels: list[str], capacity: int) -> "SharedMemoryRing":
        if not channels or capacity < 2:
            raise ValueError("shared-memory ring needs channels and capacity >= 2")
        size = cls.HEADER.size + capacity * (16 + 8 * len(channels))
        block = shared_memory.SharedMemory(name=name, create=True, size=size)
        block.buf[:] = b"\0" * size
        cls.HEADER.pack_into(block.buf, 0, cls.MAGIC, 1, len(channels), capacity, 0)
        return cls(block, channels, capacity)

    @classmethod
    def attach(cls, name: str, channels: list[str]) -> "SharedMemoryRing":
        block = shared_memory.SharedMemory(name=name, create=False)
        magic, version, n_channels, capacity, _ = cls.HEADER.unpack_from(block.buf, 0)
        if magic != cls.MAGIC or version != 1 or n_channels < 1 or capacity < 2 or len(channels) != n_channels:
            block.close()
            raise ValueError("shared-memory ring header is invalid or --channels does not match its channel count")
        return cls(block, channels, capacity)

    def write(self, timestamp: float, values: list[float]) -> int:
        if len(values) != len(self.channels):
            raise ValueError("shared-memory frame must have one value per declared channel")
        _, _, _, _, seq = self.HEADER.unpack_from(self.block.buf, 0)
        slot = seq % self.capacity
        offset = self.HEADER.size + slot * self.slot_size
        struct.pack_into("<Q", self.block.buf, offset, 2 * seq + 1)
        struct.pack_into("<d", self.block.buf, offset + 8, float(timestamp))
        for i, value in enumerate(values):
            struct.pack_into("<d", self.block.buf, offset + 16 + 8 * i, float(value))
        struct.pack_into("<Q", self.block.buf, offset, 2 * seq + 2)
        struct.pack_into("<Q", self.block.buf, 24, seq + 1)
        return seq

    def read_from(self, cursor: int) -> tuple[list[tuple[float, dict[str, float]]], int, int]:
        _, _, _, _, published = self.HEADER.unpack_from(self.block.buf, 0)
        skipped = max(0, published - cursor - self.capacity)
        cursor = max(cursor, published - self.capacity)
        frames = []
        while cursor < published:
            offset = self.HEADER.size + (cursor % self.capacity) * self.slot_size
            expected = 2 * cursor + 2
            before = struct.unpack_from("<Q", self.block.buf, offset)[0]
            if before != expected:
                break
            timestamp = struct.unpack_from("<d", self.block.buf, offset + 8)[0]
            values = [struct.unpack_from("<d", self.block.buf, offset + 16 + 8 * i)[0]
                      for i in range(len(self.channels))]
            after = struct.unpack_from("<Q", self.block.buf, offset)[0]
            if before != after:
                break
            frames.append((timestamp, dict(zip(self.channels, values))))
            cursor += 1
        return frames, cursor, skipped

    def close(self) -> None:
        self.block.close()


class LiveWindowSink:
    def __init__(self, output: TextIO, config: Config, source_name: str, channels: list[str] | None = None):
        self.output, self.config, self.source_name = output, config, source_name
        self.channels = channels or []
        self.window_samples = max(1, int(round(config.sample_rate_hz * config.window_seconds)))
        self.samples = {name: [] for name in self.channels}
        self.times: list[str] = []
        self.window_index = 0
        self.config_hash = hashlib.sha256(json.dumps(asdict(config), sort_keys=True).encode()).hexdigest()

    def push(self, timestamp: str | float | None, values: dict[str, float]) -> None:
        if not self.channels:
            self.channels = list(values)
            if not self.channels:
                raise ValueError("live frame has no channels")
            self.samples = {name: [] for name in self.channels}
        if set(values) - set(self.channels):
            raise ValueError("live channel set changed within a stream")
        self.times.append(None if timestamp is None else str(timestamp))
        for channel in self.channels:
            try:
                self.samples[channel].append(float(values.get(channel, float("nan"))))
            except (TypeError, ValueError):
                self.samples[channel].append(float("nan"))
        if len(self.times) >= self.window_samples:
            self._emit()

    def _emit(self) -> None:
        if not self.times:
            return
        start = self.window_index * self.window_samples
        end = start + len(self.times)
        for channel in self.channels:
            metrics = compute_metrics(self.samples[channel], self.config)
            flags = _flag_reasons(metrics, self.config)
            key = f"{self.source_name}:{self.window_index}:{channel}"
            record = {"schema_version": "1.1", "record_type": "window_qc",
                      "window_id": hashlib.sha256(key.encode()).hexdigest()[:24],
                      "source_file": self.source_name, "source_sha256": None,
                      "channel": channel, "sample_rate_hz": self.config.sample_rate_hz,
                      "sample_start": start, "sample_end_exclusive": end,
                      "window_start": self.times[0], "window_end": self.times[-1],
                      "window_seconds_nominal": len(self.times) / self.config.sample_rate_hz,
                      "config": asdict(self.config), "config_sha256": self.config_hash,
                      "physioqc_version": VERSION, "computed_at_utc": datetime.now(timezone.utc).isoformat(),
                      "metrics": metrics, "quality_flags": flags, "event_candidate": bool(flags)}
            self.output.write(json.dumps(record, allow_nan=False) + "\n")
        self.output.flush()
        self.window_index += 1
        self.times = []
        self.samples = {name: [] for name in self.channels}

    def finish(self) -> None:
        self._emit()


def consume_live_lines(reader, sink: LiveWindowSink) -> int:
    frames = 0
    for line in reader:
        if not line.strip():
            continue
        try:
            frame = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid NDJSON frame: {exc}") from exc
        values = frame.get("channels")
        if not isinstance(values, dict):
            raise ValueError("each live frame needs a channels object")
        timestamp = frame.get("timestamp", frame.get("timestamp_s"))
        sink.push(timestamp, values)
        frames += 1
    return frames


def _listen_socket(transport: str, address: str, output_path: Path, config: Config) -> None:
    if transport == "tcp":
        host, separator, port_text = address.rpartition(":")
        if not separator or not host:
            raise ValueError("TCP address must be HOST:PORT")
        endpoint = (host, int(port_text))
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    else:
        endpoint = address
        if os.path.exists(address):
            os.unlink(address)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    server.bind(endpoint)
    server.listen(4)
    print(f"listening on {transport} {address}; send one JSON frame per line", flush=True)
    try:
        while True:
            connection, _ = server.accept()
            with connection, connection.makefile("r", encoding="utf-8") as reader, output_path.open("a", encoding="utf-8") as output:
                sink = LiveWindowSink(output, config, f"live://{transport}/{address}/{uuid.uuid4().hex}")
                count = consume_live_lines(reader, sink)
                sink.finish()
                print(f"processed {count} frames", flush=True)
    finally:
        server.close()
        if transport == "unix" and os.path.exists(address):
            os.unlink(address)


def listen_shared_memory(name: str, channels: list[str], output_path: Path, config: Config, poll_seconds: float) -> None:
    ring = SharedMemoryRing.attach(name, channels)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cursor = 0
    try:
        with output_path.open("a", encoding="utf-8") as output:
            sink = LiveWindowSink(output, config, f"live://shm/{name}/{uuid.uuid4().hex}", ring.channels)
            print(f"reading shared-memory ring {name}; Ctrl-C to stop", flush=True)
            while True:
                frames, cursor, skipped = ring.read_from(cursor)
                if skipped:
                    print(f"warning: shared-memory ring overrun; skipped {skipped} frames", file=sys.stderr, flush=True)
                for timestamp, values in frames:
                    sink.push(timestamp, values)
                time.sleep(poll_seconds)
    except KeyboardInterrupt:
        sink.finish()
    finally:
        ring.close()


def _daemon_config(path: Path, sample_rate: float | None, window_seconds: float,
                   profile: str, mains_hz: float | None) -> Config:
    args = argparse.Namespace(input=path, bids_sidecar=None, no_bids_sidecar=False,
        profile=profile, sample_rate=sample_rate, window_seconds=window_seconds,
        mains_hz=mains_hz, line_bandwidth_hz=None, flatline_epsilon=None,
        clip_abs=None, signal_band=None, no_signal_band=False,
        max_flatline_fraction=None, max_line_noise_ratio=None,
        max_burst_rate_per_min=None, max_clip_fraction=None)
    return make_config(args)


def run_daemon(input_dir: Path, output_dir: Path, state_path: Path,
               sample_rate: float | None, window_seconds: float, profile: str,
               mains_hz: float | None, poll_seconds: float) -> None:
    if poll_seconds <= 0:
        raise ValueError("poll interval must be positive")
    output_dir.mkdir(parents=True, exist_ok=True)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state: dict[str, str] = {}
    stable: dict[str, tuple[int, int, int]] = {}
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
    print(f"watching {input_dir}; completed CSVs are processed once per content hash", flush=True)
    try:
        while True:
            for path in sorted(input_dir.glob("*.csv")):
                try:
                    stat = path.stat()
                except FileNotFoundError:
                    continue
                signature = (stat.st_size, stat.st_mtime_ns)
                previous = stable.get(str(path))
                stable[str(path)] = (*signature, (previous[2] + 1 if previous and previous[:2] == signature else 1))
                if stable[str(path)][2] < 2:
                    continue
                digest = sha256_file(path)
                if state.get(str(path)) == digest:
                    continue
                target = output_dir / f"{path.stem}.{digest[:12]}.jsonl"
                temporary = target.with_suffix(target.suffix + ".tmp")
                try:
                    with temporary.open("w", encoding="utf-8") as output:
                        run_qc(path, output, _daemon_config(path, sample_rate, window_seconds, profile, mains_hz))
                    temporary.replace(target)
                    state[str(path)] = digest
                    tmp_state = state_path.with_suffix(state_path.suffix + ".tmp")
                    tmp_state.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                    tmp_state.replace(state_path)
                    print(f"processed {path} -> {target}", flush=True)
                except Exception as exc:
                    temporary.unlink(missing_ok=True)
                    print(f"physioqc daemon: {path}: {exc}", file=sys.stderr, flush=True)
            time.sleep(poll_seconds)
    except KeyboardInterrupt:
        print("stopped", flush=True)


def _tui(records_path: Path, annotations_path: Path, reviewer: str) -> None:
    records = [json.loads(line) for line in records_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    records = [record for record in records if record.get("record_type") == "window_qc"]
    if not records:
        raise ValueError("QC file contains no window_qc records")
    annotations_path.parent.mkdir(parents=True, exist_ok=True)
    index = 0

    def app(screen):
        nonlocal index
        curses.curs_set(0)
        while True:
            screen.erase()
            record = records[index]
            metrics = record.get("metrics", {})
            screen.addstr(0, 0, f"PhysioQC review  {index + 1}/{len(records)}")
            screen.addstr(2, 0, f"Window: {record.get('window_id', '')}")
            screen.addstr(3, 0, f"Channel: {record.get('channel', '')}   Samples: {record.get('sample_start')}–{record.get('sample_end_exclusive')}")
            screen.addstr(4, 0, f"Flags: {', '.join(record.get('quality_flags', [])) or 'none'}")
            screen.addstr(6, 0, "Metrics:")
            for row, (key, value) in enumerate(metrics.items(), start=7):
                if row >= curses.LINES - 3:
                    break
                screen.addnstr(row, 2, f"{key}: {value}", max(1, curses.COLS - 4))
            screen.addstr(min(18, curses.LINES - 2), 0, "u usable · a artifact · r review · n next · p previous · q quit")
            screen.refresh()
            key = screen.getkey().lower()
            if key == "q":
                return
            if key == "n":
                index = min(index + 1, len(records) - 1)
            elif key == "p":
                index = max(index - 1, 0)
            elif key in {"u", "a", "r"}:
                label = {"u": "usable", "a": "artifact", "r": "review"}[key]
                screen.addstr(curses.LINES - 1, 0, "Note (optional): ")
                curses.echo()
                note = screen.getstr(curses.LINES - 1, 16, 160).decode("utf-8", errors="replace")
                curses.noecho()
                append_annotation(annotations_path, record["window_id"], label, reviewer, note)
                index = min(index + 1, len(records) - 1)

    curses.wrapper(app)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser("run", help="compute windowed, per-channel QC records")
    add_run_arguments(run_parser)
    parquet_parser = sub.add_parser("to-parquet", help="convert QC JSONL to Parquet in bounded batches")
    parquet_parser.add_argument("input", type=Path)
    parquet_parser.add_argument("output", type=Path)
    parquet_parser.add_argument("--batch-size", type=int, default=512)
    annotation_parser = sub.add_parser("annotate", help="append a human review record without altering QC output")
    annotation_parser.add_argument("--window-id", required=True)
    annotation_parser.add_argument("--label", choices=("usable", "artifact", "review"), required=True)
    annotation_parser.add_argument("--reviewer", required=True)
    annotation_parser.add_argument("--note", default="")
    annotation_parser.add_argument("--output", type=Path, required=True)
    listen_parser = sub.add_parser("listen", help="ingest live NDJSON frames over TCP or a Unix-domain socket")
    listen_parser.add_argument("--transport", choices=("tcp", "unix"), default="tcp")
    listen_parser.add_argument("--address", default="127.0.0.1:8765", help="HOST:PORT for TCP, socket path for unix")
    listen_parser.add_argument("--output", type=Path, required=True)
    listen_parser.add_argument("--sample-rate", type=float, default=None)
    listen_parser.add_argument("--window-seconds", type=float, default=10.0)
    listen_parser.add_argument("--profile", choices=("auto", *PROFILES), default="generic")
    listen_parser.add_argument("--mains-hz", type=float, choices=(50.0, 60.0), default=None)
    shm_parser = sub.add_parser("listen-shm", help="read a live single-writer shared-memory ring")
    shm_parser.add_argument("--name", required=True)
    shm_parser.add_argument("--channels", required=True, help="comma-separated channel names in ring order")
    shm_parser.add_argument("--output", type=Path, required=True)
    shm_parser.add_argument("--sample-rate", type=float, default=None)
    shm_parser.add_argument("--window-seconds", type=float, default=10.0)
    shm_parser.add_argument("--profile", choices=("auto", *PROFILES), default="generic")
    shm_parser.add_argument("--mains-hz", type=float, choices=(50.0, 60.0), default=None)
    shm_parser.add_argument("--poll-seconds", type=float, default=0.02)
    daemon_parser = sub.add_parser("daemon", help="watch an inbox and process stable CSV files once per content hash")
    daemon_parser.add_argument("--input-dir", type=Path, required=True)
    daemon_parser.add_argument("--output-dir", type=Path, required=True)
    daemon_parser.add_argument("--state-file", type=Path, default=Path(".physioqc-state.json"))
    daemon_parser.add_argument("--sample-rate", type=float, default=None)
    daemon_parser.add_argument("--window-seconds", type=float, default=10.0)
    daemon_parser.add_argument("--profile", choices=("auto", *PROFILES), default="auto")
    daemon_parser.add_argument("--mains-hz", type=float, choices=(50.0, 60.0), default=None)
    daemon_parser.add_argument("--poll-seconds", type=float, default=1.0)
    tui_parser = sub.add_parser("tui", help="review QC windows and append human annotations")
    tui_parser.add_argument("input", type=Path, help="QC JSONL file")
    tui_parser.add_argument("--output", type=Path, required=True, help="annotation JSONL output")
    tui_parser.add_argument("--reviewer", required=True)
    evaluate_parser = sub.add_parser("evaluate", help="compare candidate flags with an independently labeled truth CSV")
    evaluate_parser.add_argument("qc_jsonl", type=Path)
    evaluate_parser.add_argument("truth_csv", type=Path)
    evaluate_parser.add_argument("--output", type=Path, default=None, help="JSON report path; defaults to stdout")
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            config = make_config(args)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("w", encoding="utf-8") as output:
                count = run_qc(args.input, output, config)
            print(f"wrote {count} window-channel records to {args.output}")
        elif args.command == "to-parquet":
            if args.batch_size < 1:
                raise ValueError("batch size must be positive")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            export_parquet(args.input, args.output, args.batch_size)
            print(f"wrote Parquet records to {args.output}")
        elif args.command == "annotate":
            append_annotation(args.output, args.window_id, args.label, args.reviewer, args.note)
            print(f"appended annotation to {args.output}")
        elif args.command in {"listen", "listen-shm"}:
            config = _daemon_config(Path("live.csv"), args.sample_rate, args.window_seconds,
                                    args.profile, args.mains_hz)
            if args.command == "listen":
                _listen_socket(args.transport, args.address, args.output, config)
            else:
                channels = [name.strip() for name in args.channels.split(",") if name.strip()]
                listen_shared_memory(args.name, channels, args.output, config, args.poll_seconds)
        elif args.command == "daemon":
            if not args.input_dir.is_dir():
                raise ValueError(f"input directory does not exist: {args.input_dir}")
            run_daemon(args.input_dir, args.output_dir, args.state_file,
                       args.sample_rate, args.window_seconds, args.profile,
                       args.mains_hz, args.poll_seconds)
        elif args.command == "tui":
            if not sys.stdin.isatty() or not sys.stdout.isatty():
                raise ValueError("tui requires an interactive terminal")
            _tui(args.input, args.output, args.reviewer)
        elif args.command == "evaluate":
            report = evaluate_records(args.qc_jsonl, args.truth_csv)
            rendered = json.dumps(report, indent=2, allow_nan=False) + "\n"
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(rendered, encoding="utf-8")
                print(f"wrote evaluation report to {args.output}")
            else:
                print(rendered, end="")
    except (OSError, ValueError, RuntimeError, csv.Error) as exc:
        print(f"physioqc: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
