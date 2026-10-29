"""Metric definitions and independent transfer-function calculations.

Amplitude units are normalized ADC full scale (FS); PSD is FS^2/Hz.
SNR reference is explicit: clean signal through identical linear filters.
"""
from __future__ import annotations
import math
import numpy as np
from scipy.signal import lfilter


def rms(x):
    return float(np.sqrt(np.mean(np.asarray(x)**2)))


def db_ratio(signal_power, error_power):
    return float(10*np.log10(max(signal_power, 1e-30)/max(error_power, 1e-30)))


def spectrum(x, fs):
    """One-sided Hann periodogram; sum(PSD)*df = window-weighted mean square."""
    x=np.asarray(x,dtype=float)
    if len(x)<8: raise ValueError('At least eight samples required')
    w=np.hanning(len(x)); z=np.fft.rfft((x-x.mean())*w)
    psd=np.abs(z)**2/(fs*np.sum(w*w))
    psd[1:-1 if len(x)%2==0 else None]*=2
    return np.fft.rfftfreq(len(x),1/fs),psd


def bandpower(x,fs,low=70.,high=250.):
    f,p=spectrum(x,fs)
    return float(np.sum(p[(f>=low)&(f<=min(high,fs/2))])*fs/len(x))


def folded_frequency(hz,fs):
    return float(abs((hz+fs/2)%fs-fs/2))


def tone_amplitude(x,t,freq,valid):
    """Sampled peak amplitude; DC/Nyquist return identifiable sampled amplitude.

    At DC and Nyquist a continuous-time sinusoid's phase and amplitude cannot
    separately be inferred from the samples. No fictitious quadrature is fit.
    """
    xx=np.asarray(x)[valid];tt=np.asarray(t)[valid]
    if len(xx)<8: raise ValueError('Insufficient samples for tone fit')
    if abs(freq)<1e-9: return float(abs(np.mean(xx)))
    fs=1/np.median(np.diff(tt))
    if abs(freq-fs/2)<1e-7*fs:
        a=np.cos(np.pi*np.arange(len(xx)));d=np.column_stack((a,np.ones(len(xx))))
        return float(abs(np.linalg.lstsq(d,xx,rcond=None)[0][0]))
    d=np.column_stack((np.sin(2*np.pi*freq*tt),np.cos(2*np.pi*freq*tt),np.ones(len(xx))))
    a=np.linalg.lstsq(d,xx,rcond=None)[0]
    return float(np.hypot(a[0],a[1]))


def notch_coeff(fs,hz=60.,q=25.):
    w=2*np.pi*hz/fs;alpha=np.sin(w)/(2*q)
    return np.array([1.,-2*np.cos(w),1.])/(1+alpha),np.array([1.,-2*np.cos(w)/(1+alpha),(1-alpha)/(1+alpha)])


def transfer(b,a,hz,fs):
    z=np.exp(-2j*np.pi*np.asarray(hz)/fs)
    return sum(v*z**i for i,v in enumerate(b))/sum(v*z**i for i,v in enumerate(a))


def settling_seconds(p,tolerance=1e-3):
    """60 dB pole-envelope warm-up, NOT a bound on arbitrary artifact recovery."""
    b,a=notch_coeff(p['fs'],p['notch_hz'],p['notch_q'])
    radius=max(abs(np.roots(a)))
    notch=math.log(tolerance)/(p['fs']*math.log(radius))
    analog=12/(2*np.pi*p['analog_fc'])  # conservative repeated-pole startup
    return float(max(.25,notch,analog))


def pole_metrics(p):
    b,a=notch_coeff(p['fs'],p['notch_hz'],p['notch_q'])
    scale=2**p['coeff_frac']; aq=np.copysign(np.floor(np.abs(a)*scale+.5),a)/scale
    bq=np.copysign(np.floor(np.abs(b)*scale+.5),b)/scale
    radius=float(max(abs(np.roots(aq))))
    return {'quantized_pole_radius':radius,'quantized_poles_stable':int(radius<1),
            'quantized_notch_gain_db':float(20*np.log10(max(abs(transfer(bq,aq,p['notch_hz'],p['fs'])),1e-15)))}


def measure(trace,p):
    fs=p['fs']; start=settling_seconds(p);valid=trace['t']>=start
    if np.count_nonzero(valid)<max(128,fs*.5):
        raise ValueError(f'Duration must leave >=0.5 s and 128 samples after {start:.3f} s warm-up')
    clean=trace['clean_analog'][valid];ref=trace['clean_notch'][valid]
    out=trace['float_notch'][valid];fixed=trace['fixed_notch'][valid]
    original=p['gain']*trace['neural'][valid]
    bp_ref=bandpower(original,fs);bp_out=bandpower(out,fs);bp_fixed=bandpower(fixed,fs)
    bp_clean=bandpower(ref,fs)
    alias=folded_frequency(p['oob_hz'],fs)
    amp=tone_amplitude(trace['oob_analog'],trace['t'],alias,valid)
    degenerate=alias<1e-9 or abs(alias-fs/2)<1e-7*fs
    alias_rms=rms(trace['oob_analog'][valid]) if degenerate else amp/math.sqrt(2)
    qe=trace['quantization_error'][valid];noise=trace['afe_noise_analog'][valid]
    b,a=notch_coeff(fs,p['notch_hz'],p['notch_q'])
    components={name:lfilter(b,a,trace[key])[valid] for name,key in {
        'afe_noise':'afe_noise_analog','line':'line_analog','oob':'oob_analog','artifact':'artifact_analog',
        'rounding':'quantization_error','clipping':'clipping_error'}.items()}
    err=out-ref;reconstructed=sum(components.values())
    component_power=sum(float(np.mean(v*v)) for v in components.values())
    result={
        'analysis_start_s':start,'analysis_samples':int(valid.sum()),'clean_signal_rms':rms(clean),
        'input_snr_db':db_ratio(np.mean(clean**2),np.mean((trace['adc'][valid]-clean)**2)),
        'output_snr_db':db_ratio(np.mean(ref**2),np.mean(err**2)),
        'fixed_output_snr_db':db_ratio(np.mean(ref**2),np.mean((fixed-ref)**2)),
        'inband_output_snr_db':db_ratio(bp_clean,bandpower(err,fs)),
        'fixed_inband_output_snr_db':db_ratio(bp_clean,bandpower(fixed-ref,fs)),
        'bandpower_ref':bp_ref,'bandpower_out':bp_out,'fixed_bandpower_out':bp_fixed,
        'bandpower_bias_pct':100*(bp_out-bp_ref)/max(bp_ref,1e-30),
        'bandpower_error_pct':100*abs(bp_out-bp_ref)/max(bp_ref,1e-30),
        'fixed_bandpower_bias_pct':100*(bp_fixed-bp_ref)/max(bp_ref,1e-30),
        'clean_path_bandpower_bias_pct':100*(bp_clean-bp_ref)/max(bp_ref,1e-30),
        'float_fixed_rmse':rms(fixed-out),'float_fixed_max_error':float(max(abs(fixed-out))),
        'afe_noise_rms':rms(noise),'line_interference_rms':rms(trace['line_analog'][valid]),
        'oob_component_rms':rms(trace['oob_analog'][valid]),
        'adc_quantization_rms':rms(qe),'adc_quantization_rms_ideal':2**(1-p['adc_bits'])/math.sqrt(12),
        'adc_quantization_snr_db':db_ratio(np.mean(clean**2),np.mean(qe**2)),
        'quantization_to_afe_noise_db':db_ratio(np.mean(qe**2),np.mean(noise**2)),
        'adc_clipping_distortion_rms':rms(trace['clipping_error'][valid]),
        'post_adc_total_effect_rms':rms(out-trace['analog_notch'][valid]),
        'alias_frequency_hz':alias,'alias_degenerate_sampling':int(degenerate),
        'alias_in_70_250_hz':int(70<=alias<=min(250,fs/2)),
        'alias_to_signal_db':db_ratio(alias_rms**2,np.mean(clean**2)),
        'line_residual_float_amplitude':tone_amplitude(trace['float_notch']-trace['clean_notch'],trace['t'],folded_frequency(p['line_hz'],fs),valid),
        'line_residual_fixed_amplitude':tone_amplitude(trace['fixed_notch']-trace['clean_notch'],trace['t'],folded_frequency(p['line_hz'],fs),valid),
        'error_reconstruction_max':float(max(abs(err-reconstructed))),
        'output_error_power':float(np.mean(err**2)), 'component_power_sum':component_power,
        'component_cross_power':float(np.mean(err**2))-component_power,
    }
    for label,key in [('adc','adc_clip'),('fixed_input','fixed_input_clip'),('fixed_state','fixed_state_clip'),('accumulator','accumulator_clip')]:
        values=trace[key][valid];result[label+'_clip_count']=int(values.sum())
        result[label+'_clipping_pct']=float(100*np.mean(values>0))
        result[label+'_clip_count_full_record']=int(trace[key].sum())
    for name,x in components.items():result['post_notch_'+name+'_rms']=rms(x)
    # Fit only the isolated clean path: gain/phase not contaminated by noise.
    t=trace['t'][valid]
    design=np.column_stack([f(t) for hz in [93,183] for f in
        [lambda t,hz=hz:np.sin(2*np.pi*hz*t),lambda t,hz=hz:np.cos(2*np.pi*hz*t)]])
    coeff=np.linalg.lstsq(design,ref,rcond=None)[0]
    for j,(hz,weight) in enumerate([(93,.62),(183,.38)]):
        amplitude=np.hypot(*coeff[2*j:2*j+2]);phase=np.arctan2(coeff[2*j+1],coeff[2*j])
        result[f'clean_gain_{hz}_db']=20*np.log10(max(amplitude/max(p['gain']*p['amp']*weight,1e-30),1e-30))
        result[f'clean_phase_{hz}_deg']=float(np.degrees(phase))
        result[f'clean_phase_delay_{hz}_ms']=float(-phase/(2*np.pi*hz)*1000)
    result.update(pole_metrics(p));return result
