# Neural acquisition-chain engineering laboratory

A C11 acquisition/DSP model with Python experiments, independent validation,
and an evidence-linked engineering report. Built for explaining acquisition
tradeoffs, numerical implementation, and the limits of simulation.

This is an independent educational project. It is **not** a model of Precision
Neuroscience hardware and does not establish clinical or decoder performance.
Amplitudes are normalized ADC full scale, not calibrated neural voltages.

## Start here

- [Model, equations, and metric definitions](docs/MODEL_AND_METRICS.md)
- [Interview walkthrough and function map](docs/INTERVIEW_GUIDE.md)
- [Generated engineering report](results_v2/ENGINEERING_REPORT.md)
- [Machine-readable validation evidence](results_v2/validation.json)

Original `results/` is preserved as legacy data. New outputs are in `results_v2/`.
Old and new results use different noise/metric definitions and are not a
controlled before/after comparison. The interactive website source is in `sites/neural-signal-lab/`. Its browser
print view can be saved as a PDF handout; the technical report remains Markdown.

## Install and run

Requires Python 3.10+, a C11 compiler, NumPy, SciPy, and Matplotlib.
A project-local `.venv` has been prepared in this workspace. For a fresh checkout:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

`requirements.lock.txt` records the versions used for this run (Python 3.12).
Use it to reproduce this environment on a compatible platform/interpreter.

```sh
make all-results
```

The Makefile uses `.venv/bin/python` when available, otherwise `python3`.
Equivalent individual steps:

```sh
.venv/bin/python validate.py
.venv/bin/python sweep.py
.venv/bin/python multichannel.py
.venv/bin/python report.py
```

For a smaller exploratory experiment set (all experiment families still run):

```sh
.venv/bin/python sweep.py --mc-trials 5 --repeats 2 --outdir quick_run
```

The default sweep uses 40 trials per Monte Carlo variation category and three
paired noise repeats per interaction cell. Its narrow-notch cases automatically
get longer records when required. `--duration` controls other records; insufficient
post-warm-up records are rejected. The general model may be configured directly:

```sh
.venv/bin/python build.py
./chain_sim --help
./chain_sim --out /tmp/example.csv --noise-density .0002 --internal-fs 80000 --fs 1000
```

`--noise` / `--afe-noise-rms` were intentionally replaced by `--noise-density`
in FS/√Hz. `--analog-fc` and Python `--afe-bandwidth` refer to EACH RC pole;
overall cascade -3 dB bandwidth is approximately 0.644 times that value.

`build.py` respects `CC`, `CFLAGS`, and an explicit `SDKROOT`. On macOS it
checks compiler/SDK compatibility and can select a compatible installed SDK
when the system default SDK does not link. The effective command is recorded.
For headless/restricted systems, set `MPLCONFIGDIR=/tmp/neural-chain-mpl` to give
Matplotlib a writable cache. `make clean` removes binaries only, preserving data.

## What changed

- **Physical noise density:** one-sided white ASD, common 80 kHz internal clock,
  correct `sigma = density * sqrt(Fint/2)`, shared analog realizations across ADC rates.
- **Explicit quality metrics:** float and fixed full-band/in-band SNR, signed
  feature bias, clean-path gain/phase, implementation RMSE/max error, line residual.
- **Normalized power:** one-sided Hann PSD in FS²/Hz and integrated power in FS²,
  validated for even/odd records and multiple sample rates/durations.
- **Separated errors:** rounding and overload, input/state/accumulator events,
  waveform-level error reconstruction with cross-power reported.
- **Numerical design:** configurable state/fraction width, coefficient precision,
  accumulator width, rounding, saturation/wrap, DF1/DF2T, quantized pole checks.
- **Robustness experiments:** line offset/drift/harmonic/step, overload artifacts,
  impulse and zero-input limit cycles, recovery against a control run.
- **Experimental design:** paired interaction grids, replicate statistics and
  differences, separated parameter/noise/combined Monte Carlo variation.
- **Streaming/multichannel:** persistent C block states, eight synthetic channels,
  bad-channel examples, common/local referencing, host timing and delay accounting.
- **Evidence and provenance:** independent SciPy/analytical/Python-integer checks,
  source/binary hashes, build/dependency metadata, stale-evidence detection.

## Code layout

| File | Role |
|---|---|
| `dsp.h` | C float/fixed kernels, ADC, numerical contracts, persistent block APIs |
| `chain_sim.c` | Sources, analog approximation, fixed-clock noise, ADC, trace output |
| `analysis.py` | Normalized spectra, references, SNR/features, poles, error decomposition |
| `sweep.py` | Acquisition, bit-width, interaction, numerical, stress, and Monte Carlo experiments |
| `validate.py` | Independent analytical/numerical checks, JSON evidence, nonzero exit on failure |
| `tests/probe.c` | C vector driver, boundary assertions, block processing, timed kernel |
| `multichannel.py` | Synthetic array/reference study and streaming benchmark |
| `build.py` | Compiler configuration and SDK compatibility check |
| `report.py` | Evidence-checked Markdown report and illustrative numerical screening |
| `experiment_spec.json` | Editable demonstration thresholds, explicitly not hardware requirements |

## Outputs

| Output | Meaning |
|---|---|
| `baseline_trace.csv`, `baseline_metrics.csv` | Actual-time sample traces and baseline metrics |
| `acquisition_sweep.csv` | One-factor acquisition studies |
| `bit_comparison.csv`, `fixed_point_width_sweep.csv` | ADC precision and state range/resolution comparisons |
| `noise_budget.csv` | Post-notch contributions, cross-power, reconstruction closure |
| `interactions.csv`, `interaction_summary.csv` | Repeated gain/ADC, bandwidth/rate, state/fraction, Q/coefficient grids |
| `paired_deltas.csv`, `paired_delta_summary.csv` | Same-seed differences versus each grid's first x setting |
| `numerical_sweep.csv`, `numerical_screening.csv` | Numerical contracts and pass/fail against illustrative baseline thresholds |
| `notch_stress.csv` | Line mismatch, drift, harmonics, steps, artifact conditions |
| `monte_carlo.csv`, `monte_carlo_summary.csv` | Parameter-only, noise-only, combined distributions |
| `multichannel_*.csv`, `channel_quality.csv` | Referencing tradeoffs, injected fault gates |
| `multichannel_inputs.npz` | Reproducible channel inputs and clean ground truth |
| `benchmark.csv`, `streaming_manifest.json` | Five host timing trials, state memory and latency context |
| `validation.json`, `manifest.json` | Pass/fail evidence, complete settings, hashes and environment |
| `ENGINEERING_REPORT.md` | Generated findings, evidence, numerical screening, limitations |

Plots show acquisition, interaction, quantization, baseline spectra, Monte Carlo,
and referencing results. Interaction error bars show ±1 sample SD, not confidence
intervals. An unstable or poor numerical choice is retained as an experimental
result rather than hidden.

## Interpretation limits

The desired signal is two known tones. Analog filtering is two generic RC poles.
Noise is white; there is no electrode impedance, colored noise, measured amplifier
response, clock jitter, ADC nonlinearity, or target-specific instruction emulation.
The eight-channel extension starts at the ADC/reference stage and does not simulate
a complete multichannel analog circuit. No neural recordings or decoder are used.

A high matched-reference SNR can coexist with clean-signal attenuation, phase
shift, or unwanted spatial reference changes. Finite-word filters can have stable
linear poles and still exhibit limit cycles. Priors and screening thresholds are
illustrative; host throughput is not a worst-case embedded real-time guarantee.
See the model contract and generated report before interpreting a plot as a design
recommendation.
