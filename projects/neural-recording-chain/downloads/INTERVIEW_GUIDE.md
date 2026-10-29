# Project walkthrough

## Project summary

The acquisition-chain simulator separates analog noise, aliasing, ADC rounding,
clipping, and recursive fixed-point error. Numerical checks compare the DSP
blocks against analytical and independent software references. Conclusions are
limited by the synthetic inputs and generic acquisition assumptions.

## Five-minute demonstration

1. Draw signal → gain/two RC poles → ADC → causal notch → features. Explain
   why analog alias rejection must precede sampling and clipping is irreversible.
2. Show `quantization_comparison.png` and the ADC rows of `bit_comparison.csv`.
   More resolution reduces ADC rounding, but total error can be dominated by
   noise/interference. Explain the assumptions behind Δ/√12.
3. Show signed clean and noisy feature bias beside matched-reference SNR.
   The clean reference deliberately removes deterministic filter response from
   the noise metric; that response must be assessed separately.
4. Show fixed fractional precision versus fixed range. Write resolution 2^-F
   and range ±2^(N-1-F). Discuss recursive quantization, coefficient precision,
   pole radius, accumulator limits, and measured zero-input limit cycles.
5. Show the multichannel comparison. Excluding a contaminated reference channel
   helps, but shared desired neural activity can also be removed. End by saying
   what data and target measurements you would require before a product decision.

## Three deeper discussions

### Acquisition and experimental design

- Why did fixed per-sample noise RMS confound the old sampling-rate sweep?
- Derive variance = one-sided PSD × Fint/2 and ENBW = πfc/4 for two equal RC poles.
- Why does the gain sweep eventually degrade rather than keep improving SNR?
- Why can aliased interference increase bandpower while attenuation decreases it?
- Why are paired differences preferable to independent noise runs for small effects?
- What distinguishes sensitivity analysis from a manufacturing-yield prediction?

### DSP implementation

- Derive the notch difference equation and its zeros/poles.
- Explain Q versus rejection width, drift sensitivity, phase, and settling.
- Explain why coefficient quantization can shift notch depth and pole stability.
- Walk through one signed MAC, rounding, saturation, and history update.
- Explain why linear stability does not preclude zero-input limit cycles.
- Explain why DF1 and DF2T agree in double precision but differ in finite precision.
- Discuss how a target instruction's rounding/MAC behavior could differ from this contract.

### Array processing and deployment

- What desired spatial information does common-average reference remove?
- Why does matched-reference SNR conceal that removal unless raw-signal error is also shown?
- What happens when a bad channel contaminates the reference?
- Why must each channel retain independent state across block boundaries?
- Distinguish phase delay, group delay, block buffering, and end-to-end latency.
- What does the host benchmark omit? What would a worst-case target test include?

## Current function map

### `dsp.h`

| Function | Responsibility |
|---|---|
| `notch_coeff` | Normalized second-order notch coefficients |
| `float_step`, `float_block` | Persistent double-precision DF1/DF2T sample/block processing |
| `limit_int` | Explicit signed saturation or two's-complement modulo behavior and event count |
| `shift_round` | Nearest/ties-away or truncation toward zero after scaling |
| `fixed_init` | Quantize coefficients, set numerical contract, zero history |
| `fixed_input` | Round real input into the selected state Q format and handle range limits |
| `acc_limit` | Apply configured accumulator width and overflow behavior |
| `fixed_step`, `fixed_block` | Fixed-point recurrence and state-preserving block interface |
| `adc_step` | Ideal rounded signed ADC, separate rounding and overload error |

### `chain_sim.c`

| Function | Responsibility |
|---|---|
| `usage`, `fail` | Command documentation and explicit configuration errors |
| `number`, `integer` | Finite, range-checked argument conversion |
| `parse` | Defaults, overrides, derived fractional width, model/work bounds |
| `uniform01`, `gaussian` | Seeded xorshift uniform and Box–Muller normal samples |
| `rc_step` | Two cascaded exponential-update RC poles |
| `main` | Fixed internal clock, separate component paths, ADC/filter processing, actual-time traces |

### `analysis.py`

| Function | Responsibility |
|---|---|
| `rms`, `db_ratio` | Mean-square amplitude and logarithmic power ratio |
| `spectrum`, `bandpower` | Normalized one-sided Hann PSD and band integration |
| `folded_frequency` | Map a continuous tone into [0,fs/2] |
| `tone_amplitude` | Known-tone least-squares fit; explicit DC/Nyquist treatment |
| `notch_coeff`, `transfer` | Independent Python coefficients and frequency response |
| `settling_seconds` | Pole-envelope-based startup exclusion, not arbitrary recovery guarantee |
| `pole_metrics` | Quantized pole radius/stability and center-frequency gain |
| `measure` | Shared analysis mask; float/fixed SNR, bias, components, clipping, gain/phase |

### `sweep.py`

| Function | Responsibility |
|---|---|
| `compile_sim` | Build the C simulator through the shared compiler configuration |
| `read_trace` | Numeric CSV columns into arrays |
| `simulate` | Execute C with every parameter explicitly supplied |
| `run_case` | Manage trace lifetime, return complete parameters and metrics |
| `write_rows` | Write a table with the union of row fields |
| `summarize` | Group replicates; mean, sample SD, SEM, empirical quantiles |
| `make_plots` | Headless figures, normalized spectra, separate metrics, replicate SD bars |
| `main` | Validate CLI, baseline, bits, acquisition, interactions, numerical/stress/MC runs, provenance |

### Validation, multichannel, and reporting

`validate.py` builds a C vector driver and runs 14 groups of analytical and
independent-reference tests. `reference_fixed` uses Python arbitrary-width integer
arithmetic; `probe` sends arbitrary vectors through the actual C block kernels.
`tests/probe.c` also contains boundary assertions and a timed eight-state kernel.

`multichannel.py` constructs a synthetic array, detects injected bad-channel
examples, compares reference choices, passes 32 streams through C blocks, verifies
against SciPy, and reports delay/memory/host-throughput context.

`build.py` smoke-tests the compiler/SDK and compiles sources. `report.py` checks
source hashes and validation, screens numerical configurations against transparent
illustrative thresholds, and generates a report from measured tables. Editing a
source requires regenerating the corresponding evidence before reporting it.

## What not to overclaim

- “This shows an optimal ADC width for Precision.” Instead: under these explicit
  assumptions, rounding falls below other errors by a particular resolution.
- “Stable poles prove the fixed-point filter is fine.” Limit cycles and overload
  are separate nonlinear behaviors; evaluate signal and task requirements.
- “Common-average reference improves the signal.” It improves some error metrics
  while changing the desired spatial signal. Show both effects.
- “All tests pass, so the model is accurate.” Tests establish numerical contracts
  and selected analytic behavior, not electrode/neural/device realism.
- “The benchmark proves real-time performance.” It measures host kernel throughput;
  a target deadline requires full-pipeline worst-case measurement.
