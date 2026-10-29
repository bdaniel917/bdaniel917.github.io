'use strict';
const $ = (s, p = document) => p.querySelector(s);
const $$ = (s, p = document) => [...p.querySelectorAll(s)];
const fmt = (x, digits = 2) => Number(x).toLocaleString('en-US', { maximumFractionDigits: digits, minimumFractionDigits: digits });
const signed = (x, digits = 2) => `${x > 0 ? '+' : ''}${fmt(x, digits)}`;
const compact = x => Math.abs(x) < .001 && x !== 0 ? Number(x).toExponential(2) : Number(x).toLocaleString('en-US', { maximumFractionDigits: 5 });
const color = { teal: '#315e85', muted: '#a4b3bf', orange: '#a7683f', ink: '#202c38' };
const NS = 'http://www.w3.org/2000/svg';
let data, factor = 'adc_bits', setting = 16, acqView = 'spectrum';
let fixedMode = 'same_full_scale_range', fixedWidth = 16, referenceMode = 'none', tourStep = 0;
const tooltip = $('#tooltip');
const factors = {
  adc_bits: { name: 'ADC bits', label: 'ADC RESOLUTION', unit: 'bits', help: 'More ADC bits reduce the rounding step. They cannot recover clipped samples or remove interference.', baseline: 16 },
  fs: { name: 'Sample rate', label: 'SAMPLING RATE', unit: 'Hz', help: 'The analog source and noise density stay unchanged. Only the ADC sampling rate changes.', baseline: 2000 },
  gain: { name: 'Gain', label: 'ANALOG GAIN', unit: '×', help: 'Gain lifts the signal relative to ADC rounding—but eventually drives the converter into overload.', baseline: 1 },
  analog_fc: { name: 'Bandwidth', label: 'EACH RC POLE', unit: 'Hz', help: 'Two identical RC poles trade interference rejection against desired-signal attenuation. Overall −3 dB bandwidth is 0.644 × this setting.', baseline: 650 },
  noise_density: { name: 'Noise', label: 'INPUT NOISE DENSITY', unit: 'FS/√Hz', help: 'One-sided white noise density, before analog gain. A fixed 80 kHz internal clock avoids confounding noise density with ADC rate.', baseline: .0002 }
};

function node(tag, attrs = {}, text = '') {
  const el = document.createElementNS(NS, tag);
  for (const [key, value] of Object.entries(attrs)) el.setAttribute(key, value);
  if (text) el.textContent = text;
  return el;
}
function metric(label, value, sub = '') {
  return `<div><span class="metric-label">${label}</span><b class="metric-value">${value}</b><span class="metric-sub">${sub}</span></div>`;
}
function buttonState(buttons, selected, attr) {
  buttons.forEach(b => { const active = b.getAttribute(attr) === String(selected); b.classList.toggle('selected', active); b.setAttribute('aria-pressed', String(active)); });
}

// Every plotted point comes from exported C traces; visual PSD bins are averaged.
function chart(container, series, options = {}) {
  const el = typeof container === 'string' ? $(container) : container;
  const W = Math.max(360, el.clientWidth || 600), H = Math.max(200, el.clientHeight || 270);
  const pad = { l: 46, r: 17, t: 24, b: 38 }, w = W - pad.l - pad.r, h = H - pad.t - pad.b;
  const points = series.flatMap(s => s.points);
  const xmin = options.xmin ?? Math.min(...points.map(p => p[0]));
  const xmax = options.xmax ?? Math.max(...points.map(p => p[0]));
  const ymin = options.ymin ?? Math.min(...points.map(p => p[1]));
  const ymax = options.ymax ?? Math.max(...points.map(p => p[1]));
  const dy = Math.max(1e-15, ymax - ymin);
  const lo = options.ymin ?? ymin - dy * .12, hi = options.ymax ?? ymax + dy * .12;
  const xx = x => pad.l + (x - xmin) / (xmax - xmin || 1) * w;
  const yy = y => pad.t + (hi - y) / (hi - lo || 1) * h;
  const svg = node('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': options.label || 'Scientific chart' });
  for (let i = 0; i <= 4; i++) {
    const value = lo + (hi - lo) * i / 4, y = yy(value);
    svg.append(node('line', { x1: pad.l, x2: W - pad.r, y1: y, y2: y, class: 'chart-grid' }));
    const label = options.yformat ? options.yformat(value) : Math.abs(value) < .01 && value !== 0 ? value.toExponential(0) : fmt(value, options.decimals ?? 0);
    svg.append(node('text', { x: pad.l - 8, y: y + 3, 'text-anchor': 'end' }, label));
  }
  for (let i = 0; i <= 4; i++) {
    const value = xmin + (xmax - xmin) * i / 4;
    svg.append(node('text', { x: xx(value), y: H - 17, 'text-anchor': 'middle' }, options.xformat ? options.xformat(value) : fmt(value, 0)));
  }
  svg.append(node('text', { x: pad.l, y: 11, class: 'axis-label' }, options.ylabel || 'PSD · dB FS²/Hz'));
  svg.append(node('text', { x: W - pad.r, y: H - 2, 'text-anchor': 'end', class: 'axis-label' }, options.xlabel || 'FREQUENCY · Hz'));
  const clipId = `clip-${el.id}`;
  const defs = node('defs'), clip = node('clipPath', { id: clipId });
  clip.append(node('rect', { x: pad.l, y: pad.t, width: w, height: h })); defs.append(clip); svg.append(defs);
  for (const s of series) {
    const path = s.points.map((p, i) => `${i ? 'L' : 'M'}${xx(p[0]).toFixed(2)},${yy(p[1]).toFixed(2)}`).join(' ');
    svg.append(node('path', { d: path, stroke: s.color, class: 'chart-line', 'stroke-dasharray': s.dashed ? '4 4' : '', 'clip-path': `url(#${clipId})`, opacity: s.opacity ?? 1 }));
  }
  for (const a of options.annotations || []) {
    svg.append(node('line', { x1: xx(a.x), x2: xx(a.x), y1: pad.t + 12, y2: pad.t + 34, stroke: color.teal, 'stroke-width': .7 }));
    svg.append(node('text', { x: xx(a.x), y: pad.t + 7, 'text-anchor': 'middle', class: 'annotation' }, a.text));
  }
  const cross = node('line', { x1: 0, x2: 0, y1: pad.t, y2: pad.t + h, class: 'chart-cross', visibility: 'hidden' }); svg.append(cross);
  const overlay = node('rect', { x: pad.l, y: pad.t, width: w, height: h, fill: 'transparent', tabindex: 0, role: 'application', 'aria-label': 'Explore chart values with left and right arrow keys' });
  let cursor = .5;
  const hide = () => { tooltip.hidden = true; cross.setAttribute('visibility', 'hidden'); };
  const show = (fraction, clientX, clientY) => {
    cursor = Math.min(1, Math.max(0, fraction));
    const value = xmin + cursor * (xmax - xmin), text = [`${fmt(value, options.tooltipXDigits ?? 1)} ${options.xunit || 'Hz'}`];
    for (const s of series) {
      const p = s.points.reduce((a, b) => Math.abs(a[0] - value) < Math.abs(b[0] - value) ? a : b);
      text.push(`${s.name}: ${options.tooltipY ? options.tooltipY(p[1]) : fmt(p[1], 2) + ' dB'}`);
    }
    cross.setAttribute('x1', xx(value)); cross.setAttribute('x2', xx(value)); cross.setAttribute('visibility', 'visible');
    tooltip.replaceChildren(...text.map(line => { const div = document.createElement('div'); div.textContent = line; return div; }));
    tooltip.hidden = false;
    tooltip.style.left = `${Math.min(innerWidth - 270, Math.max(8, clientX + 12))}px`;
    tooltip.style.top = `${Math.min(innerHeight - 115, Math.max(8, clientY - 80))}px`;
    overlay.setAttribute('aria-label', text.join('; ') + '. Use left and right arrow keys.');
  };
  overlay.addEventListener('pointermove', e => {
    const rect = svg.getBoundingClientRect(); show(((e.clientX - rect.left) / rect.width * W - pad.l) / w, e.clientX, e.clientY);
  });
  overlay.addEventListener('pointerleave', hide); overlay.addEventListener('blur', hide);
  overlay.addEventListener('keydown', e => {
    if (!['ArrowLeft', 'ArrowRight'].includes(e.key)) return;
    e.preventDefault(); const rect = svg.getBoundingClientRect(); show(cursor + (e.key === 'ArrowRight' ? .02 : -.02), rect.left + rect.width * cursor, rect.top + 100);
  });
  svg.append(overlay); el.replaceChildren(svg);
}

function renderHero() {
  const r = data.acquisition.find(r => r.swept_factor === 'adc_bits' && r.swept_value === 16);
  const s = r.visual.spectra;
  chart('#hero-spectrum', [{ name: 'Clean reference', color: color.muted, points: s.clean_notch }, { name: 'Float output', color: color.teal, points: s.float_notch }], {
    xmin: 0, xmax: 750, ymin: -115, ymax: -15, annotations: [{ x: 93, text: '93' }, { x: 183, text: '183 Hz' }, { x: 650, text: 'aliased tone' }], label: 'Measured baseline spectrum with neural tones at 93 and 183 Hz and out-of-band alias at 650 Hz'
  });
  $('#hero-snr').innerHTML = `${fmt(data.baseline.output_snr_db, 2)} <small>dB</small>`;
}
const stages = [
  ['Neural input', '93 + 183 Hz', 'Known ground truth', 'Two controlled sine components provide an exact reference for error analysis. They are test probes, not a model of real neural activity.'],
  ['Analog front end', 'Gain + 2 RC poles', 'Noise and bandwidth', 'Input-referred white noise has a defined spectral density. Gain and two generic RC poles affect signal, interference, and noise before sampling.'],
  ['ADC', 'Sampling + quantization', 'Information can be lost', 'An ideal signed ADC rounds into a finite code space. Out-of-band content can alias; overload clips. These losses cannot be repaired by adding downstream bits.'],
  ['Digital notch', 'Causal 60 Hz rejection', 'The numerical implementation', 'Double-precision and integer filters process the same ADC samples. Finite state, coefficient, and accumulator precision can introduce different errors.'],
  ['Features', 'SNR + bandpower', 'More than one metric', 'Matched-reference SNR measures added error. Signed 70–250 Hz feature bias and clean gain/phase separately expose signal distortion.']
];
function selectStage(index) {
  buttonState($$('#pipeline button'), index, 'data-stage');
  $('#stage-detail').innerHTML = `<b>${stages[index][2]}</b><span>${stages[index][3]}</span>`;
}
function acquisitionRows() { return data.acquisition.filter(r => r.swept_factor === factor).sort((a, b) => a.swept_value - b.swept_value); }
function selectFactor(name, value) { factor = name; setting = value ?? factors[name].baseline; renderAcquisition(); }
function renderAcquisition() {
  const meta = factors[factor], rows = acquisitionRows();
  let index = rows.findIndex(r => r.swept_value === setting); if (index < 0) index = 0;
  const r = rows[index]; setting = r.swept_value;
  buttonState($$('#factor-tabs button'), factor, 'data-factor');
  $('#factor-label').textContent = meta.label;
  $('#setting-value').innerHTML = `${factor === 'noise_density' ? setting.toFixed(5) : fmt(setting, 0)} <span>${meta.unit}</span>`;
  const slider = $('#setting-slider'); slider.max = rows.length - 1; slider.value = index; slider.setAttribute('aria-valuetext', `${setting} ${meta.unit}`);
  $('#range-labels').innerHTML = rows.map((r, i) => `<button data-setting="${i}" class="${i === index ? 'selected' : ''}" aria-label="Select ${r.swept_value} ${meta.unit}" aria-pressed="${i === index}">${factor === 'noise_density' ? r.swept_value.toFixed(5) : r.swept_value.toLocaleString()}</button>`).join('');
  $$('#range-labels button').forEach(b => b.addEventListener('click', () => { setting = rows[+b.dataset.setting].swept_value; renderAcquisition(); }));
  $('#factor-help').textContent = meta.help;
  $('#constant-settings').textContent = [factor !== 'fs' && '2 kHz sampling', factor !== 'adc_bits' && '16-bit ADC', factor !== 'gain' && 'Gain 1×', factor !== 'analog_fc' && '650 Hz per RC pole', factor !== 'noise_density' && '0.0002 FS/√Hz noise', '80 kHz internal clock'].filter(Boolean).join(' · ');
  if (acqView === 'spectrum') {
    const s = r.visual.spectra;
    chart('#acq-chart', [{ name: 'ADC', color: color.orange, opacity: .6, points: s.adc }, { name: 'Clean reference', color: color.muted, points: s.clean_notch }, { name: 'Float output', color: color.teal, points: s.float_notch }], { xmin: 0, xmax: r.fs / 2, ymin: -130, ymax: -10, label: `Measured spectrum with ${meta.name} set to ${setting} ${meta.unit}` });
  } else {
    const w = r.visual.wave, t0 = w.t[0];
    chart('#acq-chart', [{ name: 'ADC', color: color.orange, opacity: .6, points: w.t.map((t, i) => [(t - t0) * 1000, w.adc[i]]) }, { name: 'Clean reference', color: color.muted, points: w.t.map((t, i) => [(t - t0) * 1000, w.clean_notch[i]]) }, { name: 'Float output', color: color.teal, points: w.t.map((t, i) => [(t - t0) * 1000, w.float_notch[i]]) }], { xlabel: 'TIME AFTER DISPLAY START · ms', ylabel: 'AMPLITUDE · FS', decimals: 2, xunit: 'ms', tooltipY: v => fmt(v, 5) + ' FS', label: 'Measured waveform segment after warm-up' });
  }
  $('#acq-metrics').innerHTML = metric('Float output SNR', `${fmt(r.output_snr_db)} <small>dB</small>`, 'Clean filtered reference') + metric('Fixed output SNR', `${fmt(r.fixed_output_snr_db)} <small>dB</small>`, 'Same clean reference') + metric('Signed feature bias', `${signed(r.bandpower_bias_pct)}<small>%</small>`, `Clean path: ${signed(r.clean_path_bandpower_bias_pct)}%`) + metric('ADC clipping', `${fmt(r.adc_clipping_pct)}<small>%</small>`, `${r.adc_clip_count.toLocaleString()} clipped / ${r.analysis_samples.toLocaleString()} analyzed`);
  let finding;
  if (factor === 'adc_bits') finding = `Rounding RMS is ${Number(r.adc_quantization_rms).toExponential(2)} FS. At 16–17 bits it sits below other baseline errors, so extra ADC precision barely changes total SNR. More bits do not remove the remaining interference.`;
  if (factor === 'fs') finding = `The 1,350 Hz component appears at ${fmt(r.alias_frequency_hz, 0)} Hz${r.alias_in_70_250_hz ? ', inside the 70–250 Hz feature band' : ', outside the feature band'}. The physical noise source is held constant. Full-band SNR and in-band feature error answer different questions.`;
  if (factor === 'gain') finding = r.adc_clipping_pct > 0 ? `Gain ${setting}× causes ADC overload in ${fmt(r.adc_clipping_pct)}% of analyzed samples. Saturation adds distortion that downstream numerical precision cannot undo.` : `At gain ${setting}×, the ADC has headroom. Amplifying both signal and input-referred noise does not improve their ratio; gain mainly helps against rounding until overload appears.`;
  if (factor === 'analog_fc') finding = `The clean path alone changes feature power by ${signed(r.clean_path_bandpower_bias_pct)}%. Narrower bandwidth can improve error-relative SNR while attenuating desired signal. An SNR maximum is not automatically the best feature-preserving setting.`;
  if (factor === 'noise_density') finding = `Filtered AFE noise RMS is ${Number(r.afe_noise_rms).toExponential(2)} FS. Increasing noise can make a negative net feature bias look smaller by adding power; that does not restore the attenuated neural signal.`;
  $('#acq-finding').innerHTML = `<span class="mono">Interpretation</span><span>${finding}</span>`;
}
function renderAlias() {
  const hz = +$('#alias-freq').value, fs = +$('#alias-fs').value;
  const alias = Math.abs((hz + fs / 2) % fs - fs / 2), inside = alias >= 70 && alias <= Math.min(250, fs / 2);
  $('#alias-freq-label').textContent = `${hz.toLocaleString()} Hz`;
  $('#alias-result').innerHTML = `${alias.toLocaleString()} <small>Hz</small>`;
  $('#alias-band').textContent = inside ? 'Inside 70–250 Hz' : 'Outside 70–250 Hz'; $('#alias-band').classList.toggle('in-band', inside);
  $('#alias-edge').textContent = alias === 0 || alias === fs / 2 ? 'At DC or Nyquist, continuous-time amplitude and phase cannot be separately identified from samples.' : 'falias = |((f + fs/2) mod fs) − fs/2|';
  const W = 500, left = 15, right = 485, x = v => left + v / (fs / 2) * (right - left);
  const svg = node('svg', { viewBox: `0 0 ${W} 75`, width: '100%', height: '100%', role: 'img', 'aria-label': `${hz} Hz folds to ${alias} Hz at ${fs} Hz sampling` });
  svg.append(node('rect', { x: x(70), y: 19, width: x(Math.min(250, fs / 2)) - x(70), height: 23, fill: '#dce9d6' }));
  svg.append(node('line', { x1: left, x2: right, y1: 42, y2: 42, stroke: '#a6b49f' }));
  svg.append(node('circle', { cx: x(alias), cy: 30, r: 5, fill: inside ? color.orange : color.teal }));
  svg.append(node('line', { x1: x(alias), x2: x(alias), y1: 34, y2: 47, stroke: inside ? color.orange : color.teal }));
  [[left, '0 Hz', 'start'], [x(160), 'FEATURE BAND', 'middle'], [right, `${(fs / 2).toLocaleString()} Hz · Nyquist`, 'end']].forEach(([px, text, anchor]) => svg.append(node('text', { x: px, y: 64, 'font-family': 'monospace', 'font-size': 9, fill: color.ink, 'text-anchor': anchor }, text)));
  $('#alias-diagram').replaceChildren(svg);
}
function renderFixed() {
  const rows = data.fixed.filter(r => r.comparison === fixedMode).sort((a, b) => a.bits - b.bits);
  const r = rows.find(r => r.bits === fixedWidth) || rows[0]; fixedWidth = r.bits;
  buttonState($$('[data-fixed-mode]'), fixedMode, 'data-fixed-mode');
  $('#width-select').innerHTML = rows.map(row => `<button data-width="${row.bits}" class="${row.bits === fixedWidth ? 'selected' : ''}" aria-pressed="${row.bits === fixedWidth}">${row.bits}-bit state</button>`).join('');
  $$('#width-select button').forEach(b => b.addEventListener('click', () => { fixedWidth = +b.dataset.width; renderFixed(); }));
  const w = r.visual.wave, t0 = w.t[0];
  chart('#fixed-chart', [{ name: 'Fixed − float', color: color.teal, points: w.t.map((t, i) => [(t - t0) * 1000, w.fixed_notch[i] - w.float_notch[i]]) }], {
    xlabel: 'TIME AFTER DISPLAY START · ms', ylabel: 'IMPLEMENTATION ERROR · FS', xunit: 'ms', decimals: 3, tooltipY: v => Number(v).toExponential(4) + ' FS', yformat: v => Math.abs(v) < .01 ? v.toExponential(1) : fmt(v, 2), label: 'Measured fixed-point output minus floating-point output, with a rescaled vertical axis for each setting'
  });
  $('#fixed-metrics').innerHTML = metric('Float / fixed error RMS', `${Number(r.float_fixed_rmse).toExponential(2)}`, 'FS · plot axis rescales') + metric('Fixed output SNR', `${fmt(r.fixed_output_snr_db)} <small>dB</small>`, 'Clean filtered reference') + metric('Fractional width F', `${r.frac_bits} <small>bits</small>`, `Resolution: ${Number(2 ** -r.frac_bits).toExponential(2)}`);
  $('#fixed-explanation').textContent = fixedMode === 'same_full_scale_range' ? `N=${r.state_bits}, F=${r.frac_bits}: the normalized range stays near [−1,1). Extra bits reduce the state step and generally reduce implementation error in this comparison. This is not a universal bound on limit cycles or task-level error.` : `F is held at 7 bits for all selected widths. The step remains 1/128. Extra total bits expand range, but they do not improve fractional resolution; measured error is unchanged when no additional range is needed.`;
}
function renderArithmetic() {
  const n = +$('#word-bits').value, input = $('#fraction-bits'); input.max = n - 1; input.value = Math.min(+input.value, n - 1);
  const f = +input.value, integer = n - f - 1, step = 2 ** -f, range = 2 ** integer;
  $('#word-bits-label').textContent = `${n} bits`; $('#fraction-bits-label').textContent = `${f} bits`;
  $('#bit-boxes').innerHTML = Array.from({ length: n }, (_, i) => `<span class="${i === 0 ? 'sign' : i <= integer ? 'integer' : 'fraction'}" title="${i === 0 ? 'Sign' : i <= integer ? 'Integer' : 'Fractional'} bit"></span>`).join('');
  $('#resolution').textContent = compact(step) + ' FS';
  $('#representable').textContent = `[−${compact(range)}, ${compact(range - step)}] FS`;
}
const references = {
  none: ['No spatial reference', 'The digital notch rejects the matched 60 Hz tone, but shared 120 Hz interference and channel noise remain.'],
  common_average_all: ['Average all channels', 'The flat and overloaded channels contaminate the reference. Shared desired activity is also removed. A plausible reference can introduce new errors.'],
  common_average_good: ['Average good channels', 'Excluding injected bad channels improves interference rejection here. The reference still removes shared desired activity, so the clean feature changes.'],
  local_good: ['Local good neighbors', 'A local reference reduces shared interference, but mixes neighboring desired signals into the channel. In this example it increases clean bandpower substantially.']
};
function renderReference() {
  const r = data.multichannel.find(r => r.mode === referenceMode);
  buttonState($$('#reference-tabs button'), referenceMode, 'data-reference');
  $('#reference-title').textContent = references[referenceMode][0]; $('#reference-copy').textContent = references[referenceMode][1];
  $('#reference-snr').innerHTML = `${fmt(r.float_matched_reference_snr_db)} <small>dB</small>`;
  $('#reference-bias').innerHTML = `${signed(r.clean_reference_feature_bias_pct)}<small>%</small>`;
  const el = $('#reference-chart'), W = Math.max(350, el.clientWidth), H = 260;
  const svg = node('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': 'Matched-reference SNR for four spatial reference choices; values are averages of good channels' });
  const l = 35, right = 16, top = 28, bottom = 48, h = H - top - bottom, w = W - l - right;
  [0, 10, 20, 30].forEach(v => { const y = top + h * (1 - v / 30); svg.append(node('line', { x1: l, x2: W - right, y1: y, y2: y, class: 'chart-grid' })); svg.append(node('text', { x: l - 10, y: y + 3, 'text-anchor': 'end' }, v.toString())); });
  svg.append(node('text', { x: l, y: 12, class: 'axis-label' }, 'MATCHED-REFERENCE FLOAT SNR · dB'));
  const labels = ['None', 'All-channel', 'Good-channel', 'Local'];
  data.multichannel.forEach((r, i) => {
    const width = Math.min(60, w / 6), x = l + w * (i + .5) / 4 - width / 2, y = top + h * (1 - r.float_matched_reference_snr_db / 30);
    const selected = r.mode === referenceMode;
    svg.append(node('rect', { x, y, width, height: top + h - y, fill: selected ? color.teal : '#d5dfd0', rx: 2 }));
    svg.append(node('text', { x: x + width / 2, y: y - 8, 'text-anchor': 'middle', style: `fill:${selected ? color.teal : color.ink}` }, fmt(r.float_matched_reference_snr_db, 1)));
    svg.append(node('text', { x: x + width / 2, y: H - 23, 'text-anchor': 'middle' }, labels[i]));
  });
  el.replaceChildren(svg);
}
const evidenceRows = {
  validated: () => [
    ['Double-precision DSP', `C output agrees with an independent SciPy reference on tested streams. Block sizes 1, 17, 64, and 1,024 preserve exactly the same output within each realization.`, `Maximum recorded reference error: ${Number(data.validation.evidence.float_reference_max_error).toExponential(2)} FS`],
    ['Bit-accurate arithmetic', '24 numerical-format and mode combinations match a Python arbitrary-integer oracle exactly. ADC code boundaries, signed rounding, saturation, and wrap behavior have explicit checks.', 'Equivalence validates the implementation contract—not the suitability of a word length.'],
    ['Analog & noise behavior', 'Tone response and white-noise RMS are checked against continuous-time two-RC expectations. Internal-rate convergence is checked at 40, 80, and 160 kHz.', `Expected filtered noise RMS: ${Number(data.validation.evidence.analog_noise_rms_theory).toExponential(3)} FS`],
    ['Power & error accounting', 'Even/odd FFT normalization and known-tone power tests pass. Filtered error components reconstruct the output error; cross terms remain explicit.', `Maximum reconstruction residual: ${Number(data.validation.evidence.component_reconstruction_max_fs).toExponential(2)} FS`],
    ['Robustness observations', 'Impulse/zero-input tests expose limit cycles. Saturation recovery is measured against a matched control. Invalid numeric settings and insufficient analysis intervals are rejected.', 'A passed test can document an undesirable behavior. It need not mean the behavior is acceptable.']
  ],
  simplified: () => [
    ['Synthetic desired signal', 'Two stationary sine components give exact ground truth. They do not establish neural feature robustness, physiological realism, or decoder accuracy.', 'Conclusion: demonstrate mechanisms, not clinical performance.'],
    ['Generic analog hardware', 'Two RC poles, white noise, and an ideal rounded/clipped ADC omit electrode impedance, colored noise, amplifier limits, nonlinearity, jitter, and measured ENOB.', 'Conclusion: optimal gain, bandwidth, and bit width remain model-specific.'],
    ['A small synthetic array', 'Eight channels and simple fault gates illustrate spatial referencing. The array extension starts at the ADC/reference stage and is not a complete multichannel analog circuit.', 'Conclusion: reference benefits depend on real spatial covariance.'],
    ['Hypothetical variability', 'The Monte Carlo priors are independent, unbounded log-normal distributions. Replicate percentiles describe the chosen simulations.', 'Conclusion: these distributions are not manufacturing yields or confidence guarantees.'],
    ['Host execution', 'A C kernel benchmark measures this host and excludes acquisition, referencing, transport, scheduling, and file I/O. The numerical contract is not a specific DSP instruction set.', 'Conclusion: throughput does not establish embedded worst-case timing or power.']
  ],
  next: () => [
    ['Measure the acquisition chain', 'Use measured input range, gain, transfer function, electrode impedance, noise spectrum, and ADC characterization to replace generic assumptions.', 'Then repeat the noise and alias budgets in calibrated voltage units.'],
    ['Use recorded neural activity', 'Evaluate representative recordings, transients, bad-channel patterns, and task labels. Compare feature preservation and downstream decoding with appropriate data splits.', 'Then connect DSP error to an actual task requirement.'],
    ['Define acceptance criteria', 'Agree on useful bands, latency, overload recovery, rejection depth, and permissible feature distortion before selecting a numerical format.', 'Then evaluate candidates across the complete operating envelope.'],
    ['Compare line-noise strategies', 'Test fixed notches, tracking or adaptive methods under real frequency drift and harmonics. Include clean-signal distortion and adaptation transients.', 'Then choose a rejection strategy against measured interference.'],
    ['Profile the target system', 'Match coefficient formats, MAC order, overflow, and rounding to the target. Measure sustained and worst-case timing with the full channel count and transport.', 'Then establish real resource and deadline margins.']
  ]
};
function evidenceHTML(mode) {
  return evidenceRows[mode]().map(r => `<div class="evidence-row"><h4>${r[0]}</h4><p>${r[1]}<span class="evidence-stat">${r[2]}</span></p></div>`).join('');
}
function renderEvidence() {
  const groups = [['validated', 'Validation checks'], ['simplified', 'Model limitations'], ['next', 'Next measurements']];
  $('#evidence-content').innerHTML = groups.map(([mode, title]) => `<section class="evidence-group" aria-labelledby="evidence-${mode}"><h3 id="evidence-${mode}">${title}</h3>${evidenceHTML(mode)}</section>`).join('');
}
const tours = [
  ['ADC resolution and total output SNR', 'Compare 16 and 17 ADC bits. Rounding error roughly halves, but system SNR barely moves because other errors dominate. Use the resolution slider to inspect both.', '#acquisition', () => selectFactor('adc_bits', 17)],
  ['Sampling rate and in-band aliasing', 'At 500 Hz sampling, a 1,350 Hz tone appears at 150 Hz—in the selected neural band. The noise source stays physically consistent across the recorded rate experiments.', '#acquisition', () => { selectFactor('fs', 500); $('#alias-fs').value = '500'; renderAlias(); }],
  ['State width and fractional precision', 'Keep the full-scale range fixed, then compare state widths. Switch to fixed fractional precision: more range alone does not improve resolution. The zero-input test below exposes another numerical limit.', '#fixed-point', () => { fixedMode = 'same_full_scale_range'; fixedWidth = 17; renderFixed(); }]
];
function renderTour() {
  const t = tours[tourStep]; $('#tour').hidden = false; document.body.classList.add('presenting');
  $('#tour-counter').textContent = `EXPERIMENT ${tourStep + 1} / ${tours.length}`;
  $('#tour-title').textContent = t[0]; $('#tour-copy').textContent = t[1];
  $('#tour-steps').innerHTML = tours.map((t, i) => `<button data-tour="${i}" class="${i === tourStep ? 'selected' : ''}" aria-pressed="${i === tourStep}">${String(i + 1).padStart(2, '0')} &nbsp; ${t[0]}</button>`).join('');
  $$('#tour-steps button').forEach(b => b.addEventListener('click', () => { tourStep = +b.dataset.tour; renderTour(); }));
  $('#tour-prev').disabled = tourStep === 0; $('#tour-next').textContent = tourStep === tours.length - 1 ? 'Close presets' : 'Next experiment →';
  t[3](); $(t[2]).scrollIntoView({ behavior: 'smooth', block: 'start' });
}
function closeTour() { $('#tour').hidden = true; document.body.classList.remove('presenting'); }
function printReport() { window.print(); }
function renderAllCharts() { if (!data) return; renderHero(); renderAcquisition(); renderFixed(); renderReference(); }
async function init() {
  try {
    const response = await fetch('data/experiments.json'); if (!response.ok) throw new Error('Data unavailable'); data = await response.json();
    $('#pipeline').innerHTML = stages.map((r, i) => `<button data-stage="${i}" aria-pressed="false"><span class="step-number">0${i + 1}</span><span class="step-name">${r[0]}</span><span class="step-sub">${r[1]}</span></button>`).join('');
    $$('#pipeline button').forEach(b => b.addEventListener('click', () => selectStage(+b.dataset.stage))); selectStage(0);
    $('#factor-tabs').innerHTML = Object.entries(factors).map(([key, meta]) => `<button data-factor="${key}" aria-pressed="false">${meta.name}</button>`).join('');
    $$('#factor-tabs button').forEach(b => b.addEventListener('click', () => selectFactor(b.dataset.factor)));
    $('#setting-slider').addEventListener('input', e => { setting = acquisitionRows()[+e.target.value].swept_value; renderAcquisition(); });
    $('#reset-acq').addEventListener('click', () => { acqView = 'spectrum'; buttonState($$('[data-acq-view]'), acqView, 'data-acq-view'); selectFactor('adc_bits'); });
    $$('[data-acq-view]').forEach(b => b.addEventListener('click', () => { acqView = b.dataset.acqView; buttonState($$('[data-acq-view]'), acqView, 'data-acq-view'); renderAcquisition(); }));
    $('#alias-freq').addEventListener('input', renderAlias); $('#alias-fs').addEventListener('change', renderAlias);
    $$('[data-fixed-mode]').forEach(b => b.addEventListener('click', () => { fixedMode = b.dataset.fixedMode; renderFixed(); }));
    $('#word-bits').addEventListener('input', renderArithmetic); $('#fraction-bits').addEventListener('input', renderArithmetic);
    $('#limit-cycle-values').innerHTML = [8, 16, 17].map(n => `<div><b>${Number(data.validation.evidence[`zero_input_tail_rms_${n}_fs`]).toExponential(2)}</b><span>${n}-bit tail RMS · FS</span></div>`).join('');
    const numeric = data.numerical;
    $('#numerical-summary').innerHTML = [[`${numeric.length} tested configurations`, numeric.length, 'A grid of numerical contracts, not a global search'], ['Stable quantized linear poles', numeric.filter(r => r.quantized_poles_stable).length, 'Does not rule out limit cycles or nonlinear overflow'], ['Pass illustrative baseline screen', numeric.filter(r => r.passes_illustrative_spec).length, 'SNR, feature error, pole stability, and limit-event thresholds']].map(r => `<tr>${r.map(v => `<td>${v}</td>`).join('')}</tr>`).join('');
    $('#channels').innerHTML = data.channelQuality.map(r => `<span class="${r.included_in_reference ? '' : 'bad'}" title="Channel ${r.channel + 1}: ${r.synthetic_fault === 'none' ? 'included in reference' : r.synthetic_fault}" aria-label="Channel ${r.channel + 1}: ${r.synthetic_fault === 'none' ? 'included in reference' : r.synthetic_fault}">${r.channel + 1}</span>`).join('');
    const referenceNames = { none: 'No reference', common_average_all: 'All-channel average', common_average_good: 'Good-channel average', local_good: 'Local good neighbors' };
    $('#reference-tabs').innerHTML = Object.entries(referenceNames).map(([key, name]) => `<button data-reference="${key}" aria-pressed="false">${name}</button>`).join('');
    $$('#reference-tabs button').forEach(b => b.addEventListener('click', () => { referenceMode = b.dataset.reference; renderReference(); }));
    $('#state-bytes').innerHTML = `${data.streaming.c_filter_state_bytes_per_channel} <small>bytes</small>`;
    $('#test-count').textContent = data.validation.tests_run;
    $('#reproducibility').innerHTML = `<p>Study schema ${data.manifest.schema_version}. C11 simulator, Python ${data.manifest.python}, NumPy ${data.manifest.numpy}, SciPy ${data.manifest.scipy}. Source and binary hashes are recorded in the downloaded manifest. The report generator checks evidence against current source hashes.</p><p>Baseline: 6 s, 2 kHz ADC, 80 kHz internal clock, gain 1×, 650 Hz per RC pole, 0.0002 FS/√Hz input noise, 16-bit ADC, 16-bit state / 15 fractional bits, 28 coefficient fractional bits, 62-bit accumulator, nearest rounding, saturation, direct form I.</p><p>${Object.entries(data.manifest.experiment_rows).map(([k, v]) => `${k.replaceAll('_', ' ')}: ${v}`).join(' · ')}. These are experiment rows, including repeated trials—not independent validation groups.</p><p>Run locally: <code>make all-results</code>. Inspect <a class="text-link" href="downloads/MODEL_AND_METRICS.md" download>the model and metric contract ↗</a> and <a class="text-link" href="downloads/INTERVIEW_GUIDE.md" download>the function reference ↗</a>.</p><p>Scientific suitability depends on measured hardware, recorded neural data, and explicit application requirements.</p>`;
    $('#start-tour').addEventListener('click', () => { tourStep = 0; renderTour(); });
    $('#close-tour').addEventListener('click', closeTour);
    $('#tour-prev').addEventListener('click', () => { if (tourStep > 0) { tourStep--; renderTour(); } });
    $('#tour-next').addEventListener('click', () => { if (tourStep === tours.length - 1) { closeTour(); $('#evidence').scrollIntoView({ behavior: 'smooth' }); } else { tourStep++; renderTour(); } });
    $('#print').addEventListener('click', printReport); $('#print-bottom').addEventListener('click', printReport);
    document.addEventListener('keydown', e => { if (e.key === 'Escape') closeTour(); });
    renderAllCharts(); renderAlias(); renderArithmetic(); renderEvidence();
    let resizeTimer; window.addEventListener('resize', () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(renderAllCharts, 120); });
    window.neuralSimulation = { data, selectFactor, renderFixed, renderReference }; // Useful for inspecting the recorded experiment contract.
  } catch (error) { $('#load-error').hidden = false; console.error(error); }
}
init();
