#!/usr/bin/env python3
"""Transparent, illustrative neural recording chain simulation."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import numpy as np

NINE_TAPS = (1, 2, 3, 4, 5, 4, 3, 2, 1)


def source_signal(fs: int = 2000, duration: float = 2.0) -> tuple[np.ndarray, np.ndarray]:
    """Return time and a deterministic synthetic source in arbitrary microvolts."""
    n = int(fs * duration)
    t = np.arange(n) / fs
    # A low-frequency field component plus brief, biphasic events.
    x = 18 * np.sin(2 * np.pi * 11 * t) + 7 * np.sin(2 * np.pi * 37 * t)
    for center in (0.31, 0.73, 1.18, 1.61):
        x += 85 * np.exp(-0.5 * ((t - center) / 0.0015) ** 2)
        x -= 34 * np.exp(-0.5 * ((t - center - 0.003) / 0.002) ** 2)
    return t, x


def lowpass(x: np.ndarray, fs: float, cutoff_hz: float) -> np.ndarray:
    """First-order RC low-pass, implemented as a stable one-pole recurrence."""
    if not 0 < cutoff_hz < fs / 2:
        raise ValueError("cutoff_hz must be between 0 and Nyquist")
    alpha = 1.0 - np.exp(-2.0 * np.pi * cutoff_hz / fs)
    y = np.empty_like(x, dtype=float)
    y[0] = alpha * x[0]
    for i in range(1, x.size):
        y[i] = y[i - 1] + alpha * (x[i] - y[i - 1])
    return y


def electrode_input_filter(x: np.ndarray, fs: float, resistance_ohm: float,
                           input_capacitance_pf: float) -> np.ndarray:
    """Simplified source-resistance/input-capacitance divider in the frequency domain."""
    if resistance_ohm <= 0 or input_capacitance_pf < 0:
        raise ValueError("resistance must be positive and capacitance nonnegative")
    freq = np.fft.rfftfreq(x.size, d=1.0 / fs)
    tau = resistance_ohm * input_capacitance_pf * 1e-12
    response = 1.0 / (1.0 + 1j * 2.0 * np.pi * freq * tau)
    return np.fft.irfft(np.fft.rfft(x) * response, n=x.size)


def fixed_point_fir(x: np.ndarray, taps: tuple[int, ...] = NINE_TAPS, q_bits: int = 12) -> np.ndarray:
    """Apply a normalized symmetric integer FIR and return scaled float samples."""
    coeff = np.array(taps, dtype=np.int64)
    scale = 1 << q_bits
    if coeff.ndim != 1 or coeff.size % 2 != 1 or coeff.sum() == 0:
        raise ValueError("FIR taps must be a nonempty odd-length vector")
    coeff = np.rint(coeff * scale / coeff.sum()).astype(np.int64)
    # Force exact unity DC gain after coefficient quantization.
    coeff[coeff.size // 2] += scale - int(coeff.sum())
    # Centered convolution: integer accumulation followed by explicit rounding.
    padded = np.pad(np.asarray(x, dtype=np.int64), (coeff.size // 2, coeff.size // 2), mode="edge")
    acc = np.convolve(padded, coeff, mode="valid").astype(np.int64)
    rounded = np.where(acc >= 0, acc + scale // 2, acc - scale // 2)
    return (rounded // scale).astype(float)


def run_trial(rng: np.random.Generator, config: dict[str, float], fs: int = 2000) -> dict[str, float]:
    """Simulate one trial and return metrics plus the processed waveform."""
    _, source = source_signal(fs=fs)
    # Simplified R-C source/input divider. This is a first-order model, not an
    # electrochemical electrode model or a claim about any specific amplifier.
    resistance = config["electrode_resistance_ohm"]
    electrode_signal = electrode_input_filter(source, fs, resistance,
                                               config["input_capacitance_pf"])
    bandwidth_hz = min(config["analog_cutoff_hz"], fs / 2)
    boltzmann = 1.380649e-23
    temperature_k = 300.0
    thermal_noise_uv = np.sqrt(4 * boltzmann * temperature_k * resistance * bandwidth_hz) * 1e6
    current_noise_uv = (config["amplifier_current_noise_pa_sqrt_hz"] * 1e-12
                        * resistance * np.sqrt(bandwidth_hz) * 1e6)
    electrode_noise = rng.normal(0.0, thermal_noise_uv, source.size)
    amplifier_noise = rng.normal(0.0, config["amplifier_noise_uv"], source.size)
    current_noise = rng.normal(0.0, current_noise_uv, source.size)
    analog = lowpass(electrode_signal + electrode_noise + amplifier_noise + current_noise,
                     fs, config["analog_cutoff_hz"])
    amplified = analog * config["gain"]
    full_scale = config["adc_full_scale_v"] * 1e6  # convert volts to microvolts
    max_code = (1 << int(config["adc_bits"])) / 2 - 1
    clipped = np.clip(amplified, -full_scale, full_scale)
    saturation_fraction = float(np.mean(np.abs(amplified) > full_scale))
    lsb = 2 * full_scale / (1 << int(config["adc_bits"]))
    codes = np.rint(clipped / lsb).clip(-max_code - 1, max_code).astype(np.int64)
    recovered = codes.astype(float) * lsb / config["gain"]
    filtered = fixed_point_fir(np.rint(recovered * 1000).astype(np.int64)) / 1000.0
    # Compare against the clean source after matching the same analog and digital bandwidth.
    clean_path = lowpass(electrode_signal, fs, config["analog_cutoff_hz"])
    reference = fixed_point_fir(np.rint(clean_path * 1000).astype(np.int64)) / 1000.0
    err = filtered - reference
    snr_db = float(10 * np.log10(np.mean(reference**2) / max(np.mean(err**2), 1e-18)))
    return {"output_snr_db": snr_db, "saturation_fraction": saturation_fraction,
            "peak_input_uv": float(np.max(np.abs(amplified))), "lsb_uv": float(lsb / config["gain"]),
            "filtered": filtered}


def rank_correlations(rows: list[dict[str, float]], keys: list[str]) -> list[dict[str, float | str]]:
    """Rank monotonic parameter/metric relationships with Spearman correlation."""
    y = np.array([r["output_snr_db"] for r in rows])
    yrank = np.argsort(np.argsort(y)).astype(float)
    result = []
    for key in keys:
        values = np.array([r[key] for r in rows])
        vrank = np.argsort(np.argsort(values)).astype(float)
        corr = float(np.corrcoef(vrank, yrank)[0, 1]) if np.std(vrank) and np.std(yrank) else 0.0
        result.append({"parameter": key, "spearman_rho": corr})
    return sorted(result, key=lambda item: abs(float(item["spearman_rho"])), reverse=True)


def simulate(trials: int = 200, seed: int = 7) -> tuple[dict, list[dict[str, float]]]:
    if trials < 2:
        raise ValueError("trials must be at least 2")
    rng = np.random.default_rng(seed)
    rows: list[dict[str, float]] = []
    for _ in range(trials):
        config = {
            "gain": float(rng.uniform(500, 6000)),
            "electrode_resistance_ohm": float(rng.uniform(1e5, 2e6)),
            "input_capacitance_pf": float(rng.uniform(20, 150)),
            "amplifier_noise_uv": float(rng.uniform(0.8, 5.0)),
            "amplifier_current_noise_pa_sqrt_hz": float(rng.uniform(0.01, 0.2)),
            "analog_cutoff_hz": float(rng.uniform(180, 450)),
            "adc_full_scale_v": float(rng.uniform(0.03, 0.3)),
            "adc_bits": float(rng.choice([10, 12, 14, 16])),
        }
        result = run_trial(rng, config)
        rows.append({**config, "output_snr_db": result["output_snr_db"],
                     "saturation_fraction": result["saturation_fraction"],
                     "peak_input_uv": result["peak_input_uv"], "lsb_uv": result["lsb_uv"]})
    metrics = [r["output_snr_db"] for r in rows]
    clips = [r["saturation_fraction"] for r in rows]
    parameter_keys = ["gain", "electrode_resistance_ohm", "input_capacitance_pf",
                      "amplifier_noise_uv", "amplifier_current_noise_pa_sqrt_hz",
                      "analog_cutoff_hz", "adc_full_scale_v", "adc_bits"]
    summary = {
        "project": "neural-recording-chain-simulator",
        "model_status": "illustrative; not device-specific",
        "seed": seed,
        "trials": trials,
        "units_note": "Synthetic source and noise are in arbitrary microvolts; ADC full scale is an illustrative volts value.",
        "output_snr_db": {"median": float(np.median(metrics)), "p05": float(np.percentile(metrics, 5)), "p95": float(np.percentile(metrics, 95))},
        "saturation_fraction": {"median": float(np.median(clips)), "p95": float(np.percentile(clips, 95))},
        "parameter_sensitivity_spearman": rank_correlations(rows, parameter_keys),
    }
    return summary, rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=int, default=200)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--out", type=Path, default=Path("results"))
    args = parser.parse_args()
    summary, rows = simulate(args.trials, args.seed)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    with (args.out / "sweep.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
