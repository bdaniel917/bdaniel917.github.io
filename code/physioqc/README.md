# PhysioQC

PhysioQC is a Python prototype for explainable quality checks on physiological recordings. It emits per-channel, per-window metrics and candidate flags, retains source/configuration provenance, and keeps human review in a separate append-only stream. It now supports batch files, live streams, watched-folder processing, a terminal review interface, BIDS JSON metadata, and a small offline evaluation harness.

It is an engineering tool, not a clinical device or a clinically validated quality classifier. Modality profiles are exploratory starting points; thresholds and metrics must be evaluated for the signal units, acquisition chain, task, and population before study use.

## Install and batch run

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python demo.py --output demo.csv
python physioqc.py run demo.csv --sample-rate 250 --window-seconds 10 \
  --clip-abs 2.5 --signal-band 0.5,40 --output qc.jsonl
```

CSV input has one row per sample and one column per channel. An optional `timestamp`, `time`, or `timestamp_s` column is retained as window context. CSV is read window by window, so working memory scales with the configured window and channel count rather than the full file.

## Modality profiles and BIDS JSON

Profiles `eeg`, `ecg`, `emg`, and `psg` supply explicit starting values for sample rate, line frequency, spectral band, and candidate thresholds. They are marked `exploratory_unvalidated` in every record. `generic` preserves conservative, modality-neutral defaults.

```bash
python physioqc.py run sub-01_task-rest_eeg.csv --profile auto --output qc.jsonl
python physioqc.py run recording.csv --profile ecg --sample-rate 500 --output qc.jsonl
```

PhysioQC automatically reads a sibling `recording.json` for a `recording.csv`, or accepts `--bids-sidecar path.json`. It uses BIDS `SamplingFrequency`, `PowerLineFrequency`, channel-count fields, and `Columns` where available. Explicit CLI settings take precedence over sidecar and profile values. BIDS inheritance resolution, direct BIDS TSV ingestion, and validation of channel units are not included.

Profiles are not clinically validated. For example, clipping thresholds depend on acquisition units and rails, while flatline tolerances depend on quantization and the measurement scale. Set thresholds explicitly when those facts are known.

## Live socket and IPC input

`listen` accepts one newline-delimited JSON frame per sample. Each frame has a timestamp and a `channels` object:

```json
{"timestamp":"2026-10-14T12:00:00.000Z","channels":{"ECG":0.12,"ACC_X":0.03}}
```

TCP listens on loopback by default. A local client can send frames to it:

```bash
python physioqc.py listen --transport tcp --address 127.0.0.1:8765 \
  --profile ecg --sample-rate 250 --window-seconds 10 --output live.jsonl
```

For local IPC, use a Unix-domain socket:

```bash
python physioqc.py listen --transport unix --address /tmp/physioqc.sock \
  --sample-rate 250 --window-seconds 10 --output live.jsonl
```

`listen-shm` reads a single-writer shared-memory ring. To create a compatible ring from a producer process:

```python
import time
from physioqc import SharedMemoryRing

ring = SharedMemoryRing.create("physioqc_ecg", ["ECG"], capacity=4096)
try:
    ring.write(time.time(), [0.12])
finally:
    ring.block.close()
    ring.block.unlink()  # The producer owns the ring lifetime.
```

The reader uses the same name and channel order:

```bash
python physioqc.py listen-shm --name physioqc_ecg --channels ECG \
  --sample-rate 250 --window-seconds 10 --output live.jsonl
```

The ring is a local, single-writer protocol with sequence markers. Overruns are reported and skipped; the reader does not silently pretend the data were continuous. TCP has no authentication or encryption, so keep it on loopback or protect it at the network layer.

## Watched-folder daemon

The foreground daemon waits until an incoming CSV's size and modification time are stable across polls, then processes it once per content hash. Changed files are processed again to a new output name. A state file tracks completed hashes.

```bash
python physioqc.py daemon --input-dir ./inbox --output-dir ./qc-output \
  --state-file ./qc-state.json --profile auto --window-seconds 10
```

Stop it with Ctrl-C. Run it under a process supervisor for unattended use; this prototype does not install a system service.

## Review and annotations

The `tui` command browses window records and appends an annotation without changing QC output. Use `u` for usable, `a` for artifact, `r` for review, `n`/`p` to move, and `q` to quit. Each label can include a short note.

```bash
python physioqc.py tui qc.jsonl --reviewer reviewer-01 --output annotations.jsonl
```

For scripts, annotate one window directly:

```bash
python physioqc.py annotate --window-id WINDOW_ID --label review \
  --reviewer reviewer-01 --note "inspect transition" --output annotations.jsonl
```

## Offline evaluation

`evaluate` compares the QC candidate flag (`event_candidate`) with an independent window-level labels CSV. Required columns are `window_id,label`; labels are `artifact`, `usable`, or `review` (excluded). An optional `fault_type` column produces per-fault summaries.

```csv
window_id,label,fault_type
abc123-000000-ECG,artifact,clipping
abc123-000001-ECG,usable,clean
```

```bash
python physioqc.py evaluate qc.jsonl truth.csv --output evaluation.json
```

The report includes confusion counts, precision, recall, specificity, and F1. This is evaluation infrastructure, not clinical validation; credible performance claims require representative recordings, defensible adjudication, prespecified thresholds, and study-specific analysis.

## Measures and output

RMS, peak-to-peak range, flatline fraction, linear drift slope, line-noise power ratio around 50/60 Hz, robust burst count/rate, missing-value fraction, kurtosis, optional clipping fraction, and an optional in-band/out-of-band power ratio. The band-power ratio is a proxy, not ground-truth SNR. Output records carry channel, sample bounds, timestamps, configuration and hashes, profile status, source provenance, metrics, and explicit candidate reasons. No opaque composite score is produced.

```bash
python -m unittest discover -s tests -v
```
