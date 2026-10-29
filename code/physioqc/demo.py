#!/usr/bin/env python3
"""Generate deterministic synthetic channels for PhysioQC exploration."""
import argparse
import csv
import math
from pathlib import Path

import numpy as np


def generate(path: Path, sample_rate: int = 250, seconds: int = 20) -> None:
    n = sample_rate * seconds
    t = np.arange(n) / sample_rate
    rng = np.random.default_rng(17)
    base = 0.35 * np.sin(2 * math.pi * 10 * t) + 0.03 * rng.normal(size=n)
    line = base + 0.15 * np.sin(2 * math.pi * 60 * t)
    flatline = base.copy(); flatline[1250:1750] = 0.12
    drift = base + np.linspace(-0.8, 0.8, n)
    burst = base.copy(); burst[[700, 701, 1400, 3000]] += [2.3, -2.6, 2.7, -2.4]
    clipped = np.clip(1.7 * base, -0.45, 0.45)
    missing = base.copy(); missing[800:805] = np.nan
    path.parent.mkdir(parents=True, exist_ok=True)
    names = ["clean", "line_noise", "flatline", "drift", "burst", "clipped", "missing"]
    arrays = [base, line, flatline, drift, burst, clipped, missing]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["timestamp_s", *names])
        for i in range(n):
            writer.writerow([f"{t[i]:.6f}", *("" if not np.isfinite(x[i]) else f"{x[i]:.8f}" for x in arrays)])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("demo.csv"))
    parser.add_argument("--sample-rate", type=int, default=250)
    parser.add_argument("--seconds", type=int, default=20)
    args = parser.parse_args()
    generate(args.output, args.sample_rate, args.seconds)
    print(f"wrote synthetic signal to {args.output}")


if __name__ == "__main__":
    main()
