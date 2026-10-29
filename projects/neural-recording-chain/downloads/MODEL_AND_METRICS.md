# Model and metric contract

This is an independent educational reference model. No transfer function, noise
specification, signal amplitude, or numerical requirement is claimed to describe
Precision Neuroscience hardware. Original `results/` is retained unchanged;
updated data uses schema 2 in `results_v2/`.

## Units and generated input

Amplitudes are normalized ADC full scale (FS): signed ADC range is
`[-1, 1 - 2^(1-N)]`. Power is FS²; PSD is FS²/Hz. To interpret the numbers in
volts, a measured or assumed full-scale voltage must be supplied externally;
this project does not silently assign a microvolt calibration.

The desired input is `amp * (.62 sin(2π93t) + .38 sin(2π183t))`.
Interference includes a configurable line tone, linear frequency drift,
second-half amplitude multiplier, independent 120 Hz harmonic, out-of-band
tone, and rectangular artifact. These are controlled probes, not realistic
neural activity or a characterized artifact population.

## Noise and internal simulation clock

`noise_density` is a one-sided amplitude spectral density in FS/√Hz, before gain.
For independent samples at the fixed internal rate `Fint`, generate Gaussian
noise with `sigma = noise_density * sqrt(Fint / 2)`.

The baseline Fint is 80 kHz, shared by all ADC rates (500, 1000, 2000, 4000 Hz).
Fint/fs must be an integer. The same seed at the same Fint produces the same
analog realization and common sample endpoints at different ADC rates. Thus
sampling-rate experiments do not silently change the physical noise density.
Changing Fint requires changing sigma as above. Different-Fint noise runs share
statistics, not identical continuous-time noise paths; convergence is assessed
statistically. Seed zero maps to one in the C generator.

For two continuous-time identical RC poles with per-pole frequency fc,
`H(f) = (1 + j f/fc)^(-2)` and the one-sided equivalent noise bandwidth is
`π fc / 4`. Expected noise RMS after gain is `G * density * sqrt(π fc / 4)`.
The implemented exponential-update RC cascade is a discrete approximation;
finite-Fint response and integration errors remain. Analytic tone gain and
noise RMS are checked, including 40/80/160 kHz convergence.

The **overall** -3 dB frequency is `fc * sqrt(sqrt(2)-1) ≈ .644 fc`.
At the per-pole fc, the cascade is -6.02 dB. It is not a Butterworth response.
The analog model includes no high-pass, electrode impedance, 1/f noise,
common-mode rejection, or amplifier saturation. Only the ADC and numerical
filter states saturate/wrap.

## Sampling, quantization, and overload

Every trace row is labeled with its actual final internal-step time. Baseline
first timestamp is 39/80000 s, followed by increments of 1/2000 s. This offset
is intentional; the old implementation mislabeled these samples.

ADC conversion rounds half away from zero. It separates:

- Rounding error: ideal unbounded rounded-code value minus analog input.
- Clipping error: bounded ADC value minus unbounded rounded-code value.

Their sum is exactly the ADC error (up to floating-point roundoff). The
`Δ/√12` estimate assumes suitably distributed, decorrelated quantization error;
it is not promised for small coherent tones, zero input, or clipping distortion.

An out-of-band frequency folds as `abs((f + fs/2) mod fs - fs/2)`.
At DC or Nyquist the sampled sinusoid's phase and continuous-time peak cannot
be identified separately. The report flags these cases and uses sampled RMS;
it does not invent a sine/cosine quadrature amplitude. For ordinary aliases,
least-squares sine/cosine fitting estimates amplitude independent of phase.
An isolated component estimate describes this synthetic tone, not a blind
alias detector usable on unknown recordings.

## Filters and integer arithmetic

The reference is a causal double-precision biquad. Fixed point supports direct
form I and transposed direct form II with independently configured:

- Signed input/state width N, 2..31; fractional bits F, 0..N-1.
- Coefficient fractional bits C, 2..28. Coefficients are integer multiples of
  2^-C, with storage wide enough for coefficients near ±2; conceptual signed
  coefficient width is C+2. Coefficient rounding is always nearest, ties away.
- Accumulator width A, 8..62. State rounding is nearest/ties-away or truncation
  toward zero. Overflow is explicit saturation or modulo two's-complement wrap.

Resolution is 2^-F; range is `[-2^(N-1-F), 2^(N-1-F)-2^-F]`.
DF1 limits the accumulator after each left-to-right MAC. DF2T limits each state
update expression, then rounds each state to the selected Q format. These are
specified software arithmetic contracts, not claims of matching any particular
DSP instruction set. Wider host int64 intermediates safely contain the supported
products and sums. Counters distinguish input conversion, state, and accumulator
limit events. A sample can have multiple events; clipping percentage is the
percentage of samples with at least one event.

The quantized denominator's maximum pole radius and stability flag are saved.
Linear pole stability is necessary but does not rule out nonlinear limit cycles,
clipping, wrap distortion, or coefficient-related notch leakage. Unit tests
compare C output exactly with an arbitrary-integer Python oracle. This validates
implementation equivalence, not scientific suitability of a chosen word length.

## Analysis interval and reference definitions

Warm-up is the larger of 0.25 s, a 60 dB unquantized-notch pole-envelope decay,
and a conservative repeated-RC-pole interval. At least 0.5 s and 128 samples
must remain. The warm-up is not a universal bound on arbitrary artifact recovery
or fixed-point limit cycles. High-Q trials automatically use longer records.
Clipping counts and percentages both use this same analysis interval; separate
full-record counts remain available.

Let r = desired neural signal after gain, c = clean analog signal, h = notch,
y = floating notch output from ADC, and q = fixed output:

| Metric | Definition | What it does not establish |
|---|---|---|
| Input SNR | 10 log10(mean(c²) / mean((ADC-c)²)) | Does not penalize analog distortion shared by c |
| Float output SNR | 10 log10(mean(h(c)²) / mean((y-h(c))²)) | Does not penalize clean-path attenuation or phase |
| Fixed output SNR | Same reference h(c), output q | Not just float/fixed implementation error |
| In-band SNR | Corresponding 70–250 Hz reference/error powers | Excludes error outside the selected band |
| Feature bias | 100 * (BP(output)-BP(r)) / BP(r) | Can conceal cancellation between attenuation and added interference |
| Clean feature bias | 100 * (BP(h(c))-BP(r)) / BP(r) | Separates deterministic clean-path distortion |
| Float/fixed RMSE | sqrt(mean((q-y)²)) | Does not identify spectral or task relevance |
| Clean gain/phase | Joint fit at 93 and 183 Hz on h(c) | Phase delay at two tones is not broadband group delay |

The line-residual amplitude is a fit at the nominal configured line frequency.
For drift or steps it is not the total residual interference power; use the
separately filtered line-component RMS and full error spectra as well.

The raw input reference keeps bandwidth attenuation visible in feature bias.
Both signed bias and absolute error are retained. Full-band and in-band SNR
answer different questions. A zero-power floor prevents infinities; an SNR
computed for a zero desired signal is not physically meaningful.

## Normalized spectral power

After mean removal, apply a Hann window and compute a one-sided periodogram:
`PSD = abs(rFFT(x*w))² / (fs * sum(w²))`. Double positive-frequency bins except
DC and, for even records, Nyquist. Integrate using `df=fs/N`.

This yields FS²/Hz and FS² with verified Parseval normalization. The integral is
window-weighted power; it is a finite-record estimate, not an exact ensemble PSD.
The 70–250 Hz band ends at Nyquist when fs=500 Hz; bin-edge inclusion and finite
frequency resolution remain limitations. No Welch averaging or spectral
confidence interval is claimed. DC is removed for PSD/bandpower, but retained
in full-band time-domain RMS and error.

## Error contribution budget

AFE noise, line, out-of-band, artifact, rounding error, and clipping error are
individually passed through the identical floating notch. Their waveform sum
must reconstruct `y - h(c)`. Save the maximum closure residual, component powers,
and `cross_power = total_error_power - sum(component_powers)`.

The waveform decomposition remains valid through ADC clipping because the
clipping error is explicitly defined. The *powers* need not add independently:
clipping and rounding are signal dependent and finite records have cross terms.

## Experimental uncertainty and reproducibility

Interaction grids repeat common seeds across configurations. Export raw paired
differences, means, sample SD, SEM, and empirical 5th/95th percentiles. Three
replicates demonstrate variability but are insufficient for precise tail claims.
Percentiles are not confidence intervals or pass/fail probabilities.

Monte Carlo separates parameter-only (fixed noise realization), noise-only
(fixed parameters), and combined variation. Gain/fc/density have independent
unbounded log-normal priors with log SD .08/.12/.25. They are hypothetical;
there is no manufacturing distribution or yield claim.

Manifests record parameters, source/binary hashes, dependencies, and environment.
Timing is host-only, with five repeats. Throughput excludes acquisition,
reference computation, file I/O, transport, and scheduling; it does not certify
worst-case deadlines. Block collection delay and filter group delay are separate.
