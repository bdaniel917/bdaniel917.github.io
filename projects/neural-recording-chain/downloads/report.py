#!/usr/bin/env python3
"""Build an evidence-linked Markdown engineering report from current results.

No PDF or website is produced. Claims are drawn from measured outputs, and
source hashes prevent silently presenting stale validation as current.
"""
from __future__ import annotations
import argparse
import csv
import hashlib
import json
from pathlib import Path
from sweep import ROOT,write_rows


def rows(path):
    with path.open(newline='') as f:return list(csv.DictReader(f))


def table(headers,data):
    return '\n'.join(['| '+' | '.join(headers)+' |','| '+' | '.join(['---']*len(headers))+' |']+
                     ['| '+' | '.join(map(str,row))+' |' for row in data])


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--outdir',default='results_v2');args=ap.parse_args()
    out=Path(args.outdir).resolve()
    validation=json.loads((out/'validation.json').read_text());manifest=json.loads((out/'manifest.json').read_text())
    streaming=json.loads((out/'streaming_manifest.json').read_text())
    if not validation['passed']:raise RuntimeError('Validation has failures; report refused')
    for record in [validation,manifest,streaming]:
        for name,expected in record['source_sha256'].items():
            if hashlib.sha256((ROOT/name).read_bytes()).hexdigest()!=expected:raise RuntimeError(f'Stale evidence for {name}; rerun corresponding experiment')
    if hashlib.sha256((ROOT/'chain_sim').read_bytes()).hexdigest()!=manifest['binary_sha256']:
        raise RuntimeError('Binary changed since experiments; regenerate sweep')
    baseline=rows(out/'baseline_metrics.csv')[0];bits=rows(out/'bit_comparison.csv')
    evidence=validation['evidence'];multi=rows(out/'multichannel_summary.csv')
    spec=json.loads((ROOT/'experiment_spec.json').read_text());screen=[]
    for row in rows(out/'numerical_sweep.csv'):
        reasons=[]
        if float(row['fixed_output_snr_db'])<spec['minimum_fixed_output_snr_db']:reasons.append('fixed SNR')
        if float(row['output_snr_db'])-float(row['fixed_output_snr_db'])>spec['maximum_snr_degradation_vs_float_db']:reasons.append('SNR degradation')
        if float(row['float_fixed_rmse'])>spec['maximum_float_fixed_rmse_fs']:reasons.append('implementation RMS')
        if abs(float(row['fixed_bandpower_bias_pct']))>spec['maximum_absolute_fixed_bandpower_bias_pct']:reasons.append('feature bias')
        if spec['require_stable_quantized_poles'] and not int(row['quantized_poles_stable']):reasons.append('pole stability')
        if spec['require_no_fixed_input_state_or_accumulator_events'] and any(int(row[k]) for k in ['fixed_input_clip_count','fixed_state_clip_count','accumulator_clip_count']):reasons.append('overflow events')
        # This bit-count proxy is transparent; it is NOT time, area, or power.
        proxy=(2 if row['realization']=='df2t' else 4)*int(row['state_bits'])+5*(int(row['coeff_frac'])+2)+int(row['acc_bits'])
        screen.append({**row,'passes_illustrative_spec':int(not reasons),'failure_reasons':'; '.join(reasons),'format_bit_count_proxy':proxy})
    write_rows(out/'numerical_screening.csv',screen)
    passing=sorted([r for r in screen if r['passes_illustrative_spec']],key=lambda r:r['format_bit_count_proxy'])
    adc=[r for r in bits if r['bit_path']=='ADC']
    adc_table=table(['ADC bits','Rounding RMS (FS)','Float output SNR (dB)','Fixed output SNR (dB)'],
                    [[r['bits'],f"{float(r['adc_quantization_rms']):.6g}",f"{float(r['output_snr_db']):.4f}",f"{float(r['fixed_output_snr_db']):.4f}"] for r in adc])
    f=lambda key:float(baseline[key])
    validation_table=table(['Validation','Measured evidence','Interpretation'],[
        ['Float reference',f"Max error {evidence['float_reference_max_error']:.3g} FS",'C agrees with SciPy for tested random streams'],
        ['Fixed reference','24 format/mode combinations, exact equality','Independent Python arbitrary-integer oracle'],
        ['Block state','1, 17, 64, 1024 samples; irregular final block','Same sample output regardless of block size'],
        ['Analog convergence',f"Max tone gain error {evidence['analog_max_gain_error']:.3g}",'40/80/160 kHz versus continuous two-pole transfer'],
        ['Noise calibration',f"Theory RMS {evidence['analog_noise_rms_theory']:.6g} FS",'Measured noise agrees within explicit finite-record tolerance'],
        ['Error closure',f"Max residual {evidence['component_reconstruction_max_fs']:.3g} FS",'Waveforms reconstruct; powers retain cross terms'],
        ['PSD normalization','Even/odd Parseval and tone power checks pass','FS²/Hz PSD; FS² integrated power'],
        ['ADC boundaries','Code transitions, ties, overload, error decomposition','Rounding and clipping have separate contracts'],
        ['Saturation recovery',f"Float {evidence['float_notch_artifact_recovery_to_1mFS_s']:.3f} s; fixed {evidence['fixed_notch_artifact_recovery_to_1mFS_s']:.3f} s",'Observed last deviation >0.001 FS from unclipped-control output'],
    ])
    limit_table=table(['State bits','Zero-input tail RMS (FS)'],[[n,f"{evidence[f'zero_input_tail_rms_{n}_fs']:.6g}"] for n in [8,16,17]])
    multi_table=table(['Reference','Matched float SNR (dB)','Clean feature bias (%)','Original-signal error RMS (FS)'],
                      [[r['mode'],f"{float(r['float_matched_reference_snr_db']):.2f}",f"{float(r['clean_reference_feature_bias_pct']):.2f}",f"{float(r['original_signal_error_rms_fs']):.5f}"] for r in multi])
    candidate_table=table(['Coefficient fractional bits','Accumulator bits','Rounding','Overflow','Realization','Bit-count proxy'],
        [[r[k] for k in ['coeff_frac','acc_bits','rounding','overflow','realization','format_bit_count_proxy']] for r in passing[:8]]) if passing else 'No configuration passes the illustrative baseline specification.'
    limitations=table(['Simplification','Effect on conclusion','Evidence needed next'],[
        ['Two stationary desired tones','Cannot infer neural feature robustness or decoder accuracy','Recorded neural data and labeled task evaluation'],
        ['Generic two-RC analog response','Optimal bandwidth/gain values are model-specific','Measured transfer functions, gain limits, input range'],
        ['White input noise only','No electrode or low-frequency colored-noise behavior','Noise PSD, electrode impedance, amplifier characterization'],
        ['Ideal ADC except quantization/clipping','Bit depth is not measured ENOB or hardware fidelity','Measured SINAD, INL/DNL, jitter, transfer curves'],
        ['Fixed 60 Hz notch','Drift/harmonics can leak; high Q extends transients','Application requirements and comparison with tracked/adaptive methods'],
        ['Finite simulation grid and hypothetical priors','No global optimum, yield, or rare-event guarantee','Requirements-driven grids and justified parameter distributions'],
        ['8 synthetic channels and simple quality gates','Referencing can remove desired shared activity','Recorded spatial covariance and validated channel rejection'],
        ['Host kernel timing','Cannot establish embedded real-time, power, or safety margins','Target profiling, worst-case timing, full pipeline and transport'],
    ])
    text=f'''# Neural acquisition-chain engineering report

Independent educational model • schema 2 • generated from checked experiment outputs.
This report does not characterize Precision Neuroscience hardware.

## Question and method

Where is information lost, and when does additional numerical precision help?
The C model separates clean signal, AFE noise, interference, ideal ADC rounding,
ADC overload, and fixed-point implementation effects. Python controls experiments,
computes explicit-reference metrics, and independently validates the DSP blocks.

Baseline: {manifest['baseline']['fs']:g} Hz ADC, {manifest['baseline']['internal_fs']:g} Hz internal
clock, {manifest['baseline']['duration']:g} s record, {manifest['baseline']['analog_fc']:g} Hz per RC pole,
noise density {manifest['baseline']['noise_density']:g} FS/√Hz.
Amplitude remains normalized; there is no device voltage calibration.

## Principal observations

1. Baseline floating output SNR is **{f('output_snr_db'):.3f} dB**, fixed output SNR is
   **{f('fixed_output_snr_db'):.3f} dB**. The shared clean-filtered reference excludes
   clean-path attenuation; signed raw-input feature bias is **{f('bandpower_bias_pct'):.3f}%**.
   Clean-path feature bias alone is **{f('clean_path_bandpower_bias_pct'):.3f}%**.
2. Additional ADC precision reduces rounding error but need not improve system SNR.
   The current ADC comparison is below; conclusions apply to this input/noise/gain case.
3. Recursive fixed-point filters can retain limit cycles even when their quantized
   linear poles are stable. Higher precision reduces some errors but is not a
   universal cure for every input, realization, or rounding mode.
4. Referencing suppresses shared interference but also changes desired spatial signals.
   A favorable matched-reference SNR is not proof of preserving the original signal.

{adc_table}

![Acquisition sweeps](acquisition_sweep.png)

## Noise-model correction

Old noise had constant per-internal-sample RMS while the internal rate varied
with ADC rate. This confounded noise density and sampling rate. The new model
holds a common internal rate and one-sided density, with input sigma = density ×
sqrt(Fint/2). Common seeds share actual analog sample endpoints across ADC rates.
Measured noise RMS at 500/1000/2000/4000 Hz is
{', '.join(f'{v:.6g}' for v in evidence['noise_rms_by_fs_500_1000_2000_4000'])} FS;
continuous-RC theory predicts {evidence['analog_noise_rms_theory']:.6g} FS.

Normalized Hann PSD and integrated power replace the old unnormalized FFT sum.
Full-band SNR, in-band SNR, signed feature bias, and clean-path gain/phase are
reported separately. These metric definitions prevent one good number from
concealing a different distortion. Details: [model and metrics](../docs/MODEL_AND_METRICS.md).

## Validation performed

**{validation['tests_run']} test groups passed**, with no reported errors or failures.
Test results establish the stated numerical contracts for selected vectors and
parameter ranges; they do not validate a physiological model or a product.
Machine-readable evidence: [validation.json](validation.json).

{validation_table}

Impulse-followed-by-zero behavior, measured over the last 2 s of an 8 s vector:

{limit_table}

These are observations for one excitation and format. The 8-bit result being zero
is not evidence that 8 bits is more accurate; coarse arithmetic can erase the
small response while causing larger errors during an active signal. The validation
suite records these outcomes rather than forcing a misleading zero-tail assertion.

## Experiment coverage and uncertainty

{', '.join(f"{k}: {v}" for k,v in manifest['experiment_rows'].items())} rows.
Interaction runs pair common seeds, with {manifest['arguments']['repeats']} repeats.
Summary files contain sample SD, SEM, and empirical 5th/95th percentiles.
These intervals are descriptive, not manufacturing yields or confidence bounds.
Plots show means ± one sample SD for repeated interactions.

![Interaction experiments](interactions.png)

Monte Carlo separates parameter-only, noise-only, and combined variation.
The independent log-normal priors are unbounded, hypothetical, and explicitly
saved. Coefficient precision, accumulator width, rounding, overflow, realization,
line offset/drift/harmonics/steps, and artifact stress are covered in separate tables.
Fixed-filter pole radius is saved, including unstable/marginal choices; numerical
failures are evidence, not silently discarded configurations.

## Illustrative numerical requirements

[experiment_spec.json](../experiment_spec.json) defines an editable demonstration
screen, not an employer requirement: fixed SNR ≥ {spec['minimum_fixed_output_snr_db']:g} dB,
SNR loss versus float ≤ {spec['maximum_snr_degradation_vs_float_db']:g} dB,
implementation RMS ≤ {spec['maximum_float_fixed_rmse_fs']:g} FS,
absolute fixed feature bias ≤ {spec['maximum_absolute_fixed_bandpower_bias_pct']:g}%,
stable quantized poles, and no analyzed fixed-point overflow events.
**{len(passing)}/{len(screen)}** tested numerical configurations pass this baseline-only screen.

{candidate_table}

The bit-count proxy is S×N + 5(C+2) + A, where S=4 for DF1 and S=2 for
DF2T. It counts conceptual active state, coefficient, and accumulator bits,
providing only a transparent comparison proxy. It does not measure silicon area, host
memory, time, power, or identify the best embedded implementation. Neither this
baseline screen nor its ranking guarantees performance over the stress suite.
Full decisions and failure reasons: [numerical_screening.csv](numerical_screening.csv).

## Multichannel and streaming demonstration

Eight channels include a flat channel and a channel with an overload artifact.
Known clean signals include a shared desired component, making reference-related
signal removal visible. The simple quality gate excludes the two injected bad
channels. Statistics below average the six good channels; rejected channels remain
in the detailed table. The C block implementation preserves separate state and
agrees with SciPy on 32 channel/mode streams within 5e-13 FS.

{multi_table}

![Multichannel reference tradeoffs](multichannel.png)

At 64-sample blocks and 2 kHz, block collection can contribute up to
**{streaming['block_collection_max_ms']:g} ms**. Notch group delay at 93/183 Hz is
{streaming['notch_group_delay_ms_at_93_183_hz'][0]:.4f}/{streaming['notch_group_delay_ms_at_93_183_hz'][1]:.4f} ms.
These are different delays, not an end-to-end latency estimate.
The reference C state structure occupies {streaming['c_filter_state_bytes_per_channel']} bytes per channel.
Median host kernel throughput across five repeats is
{streaming['median_c_channel_samples_per_second']:.4g} channel-samples/s on {streaming['machine']}.
It excludes I/O, referencing, transport, acquisition, and scheduling and does not
certify a worst-case embedded deadline. Input generation and file I/O are outside
the timed kernel; results include a checksum to keep processing observable.

## What remains simplified and how it affects conclusions

{limitations}

## Reproduce and inspect

Use the environment and commands in [README](../README.md). Manifests record
source hashes, configuration, binary hash, dependencies, and compiler/SDK settings.
Report generation refuses failed or source-stale validation. Original `results/`
is legacy data and is not overwritten. The two schema versions are not a paired
before/after experiment: noise settings, duration, and metric definitions changed.

Useful tables: [baseline](baseline_metrics.csv), [ADC/state comparisons](bit_comparison.csv),
[paired differences](paired_delta_summary.csv), [numerical sweep](numerical_sweep.csv),
[notch stress](notch_stress.csv), [Monte Carlo](monte_carlo_summary.csv),
[multichannel](multichannel_metrics.csv), [provenance](manifest.json).
'''
    (out/'ENGINEERING_REPORT.md').write_text(text)
    print('Wrote',out/'ENGINEERING_REPORT.md')
    return 0
if __name__=='__main__':raise SystemExit(main())
