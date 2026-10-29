# Neural recording chain simulator

A small, reproducible engineering study of how a signal changes as it moves through an illustrative neural acquisition chain. It models source activity, electrode/interface noise, analog bandwidth and gain, ADC sampling and quantization, and an integer-coefficient FIR filter. A Monte Carlo sweep varies selected assumptions to show which parameters most affect output SNR and clipping.

This is a teaching and portfolio project. It is **not** a model of Precision Neuroscience hardware, a medical device, or a clinical recording. Values are illustrative and should be replaced with measured specifications before drawing device-level conclusions.

## Run

Requires Python 3.10+ and NumPy.

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python simulate.py --trials 200 --seed 7 --out results
```

The command writes `summary.json` and `sweep.csv` to the output directory. No patient or external data is used.

## Model

1. Create a deterministic synthetic source containing a low-frequency component and transient events.
2. Apply a first-order source-resistance/input-capacitance divider, then add approximate resistor thermal noise, amplifier voltage noise, and amplifier current noise.
3. Apply a simple first-order analog low-pass model and gain.
4. Sample, clip to the ADC full-scale range, and quantize to a configurable number of bits.
5. Apply a symmetric integer FIR low-pass filter, with explicit accumulator scaling and rounding.
6. Randomize gain, input noise, ADC range, and cutoff across trials; report output SNR, saturation fraction, and sensitivity correlations.

The model is intentionally transparent. The electrode interface is reduced to a first-order R-C divider; the thermal-noise RMS uses an approximate bandwidth calculation. This is not a complete electrode-electrolyte impedance model or a validated noise-power budget. The project also does not model a complete anti-alias filter, device-specific ADC behavior, or a full fixed-point overflow analysis. Those are natural next refinements when real component data is available.

## Example output

`summary.json` gives the seed, model configuration, SNR and clipping summaries, and a ranked list of Spearman rank correlations between swept parameters and output SNR. Treat the values as relative behavior within this model, not as expected performance of a real recording system.

## Tests

```bash
python -m unittest discover -s tests -v
```
