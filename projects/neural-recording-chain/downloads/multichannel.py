#!/usr/bin/env python3
"""Eight-channel controlled referencing and stateful C block-processing demo.

Synthetic channel signals are explicitly known. Referencing changes the desired
signal as well as interference; both matched-reference SNR and original-signal
error are reported. This is not validation on neural recordings.
"""
from __future__ import annotations
import argparse
import io
import json
import hashlib
from pathlib import Path
import platform
import subprocess
import numpy as np
from scipy.signal import lfilter,group_delay
from analysis import bandpower,db_ratio,notch_coeff,settling_seconds,spectrum
from sweep import BASE,write_rows
from validate import compile_probe,probe,PROBE


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--outdir',default='results_v2');args=ap.parse_args()
    out=Path(args.outdir);out.mkdir(parents=True,exist_ok=True);compile_probe()
    fs=2000.;t=np.arange(12000)/fs;rng=np.random.default_rng(2026);n=8
    # A shared desired component deliberately demonstrates CAR's cost.
    neural=np.stack([.03*np.sin(2*np.pi*93*t)+.12*np.sin(2*np.pi*(110+7*c)*t+c*.4) for c in range(n)])
    gains=np.linspace(.9,1.1,n)
    clean=gains[:,None]*neural
    noise=rng.normal(size=(n,len(t)))*np.linspace(.003,.008,n)[:,None]
    common=.12*np.sin(2*np.pi*60*t)+.04*np.sin(2*np.pi*120*t)
    analog=clean+noise+gains[:,None]*common
    analog[6]=0  # flat/disconnected example
    analog[7]+=1.4*((t>=2)&(t<2.1)) # saturated bad channel
    scale=2**15;raw=np.copysign(np.floor(abs(analog)*scale+.5),analog)
    adc=np.clip(raw,-scale,scale-1)/scale
    clip_fraction=np.mean(raw!=np.clip(raw,-scale,scale-1),axis=1)
    # Deliberately simple illustrative quality gates, not clinically tuned.
    good=(np.std(adc,axis=1)>1e-5)&(clip_fraction<.001)
    detection=[{'channel':c,'included_in_reference':int(good[c]),'adc_clip_pct':float(100*clip_fraction[c]),
                'std_fs':float(np.std(adc[c])),'synthetic_fault':(['none']*6+['flat','artifact_clipping'])[c]} for c in range(n)]
    write_rows(out/'channel_quality.csv',detection)
    np.savez_compressed(out/'multichannel_inputs.npz',t=t,clean=clean,adc=adc,good=good)
    b,a=notch_coeff(fs);valid=t>=settling_seconds(BASE);rows=[];traces=[]
    modes=['none','common_average_all','common_average_good','local_good']
    for mode in modes:
        for c in range(n):
            if mode=='none':neighbors=[]
            elif mode=='common_average_all':neighbors=list(range(n))
            elif mode=='common_average_good':neighbors=list(np.flatnonzero(good))
            else:neighbors=[j for j in [c-1,c+1] if 0<=j<n and good[j]]
            reference=adc[neighbors].mean(axis=0) if neighbors else np.zeros(len(t))
            clean_reference=clean[neighbors].mean(axis=0) if neighbors else np.zeros(len(t))
            x=adc[c]-reference;desired=lfilter(b,a,clean[c]-clean_reference)
            original=lfilter(b,a,clean[c])
            # Block size 64: C preserves an independent state per channel stream.
            table,_=probe(x,block=64);float_out=table['float'];fixed_out=table['fixed']
            np.testing.assert_allclose(float_out,lfilter(b,a,x),atol=5e-13,rtol=0)
            rows.append({'mode':mode,'channel':c,'good_channel':int(good[c]),'reference_channels':','.join(map(str,neighbors)),
                'float_matched_reference_snr_db':db_ratio(np.mean(desired[valid]**2),np.mean((float_out[valid]-desired[valid])**2)),
                'fixed_matched_reference_snr_db':db_ratio(np.mean(desired[valid]**2),np.mean((fixed_out[valid]-desired[valid])**2)),
                'original_signal_error_rms_fs':float(np.sqrt(np.mean((float_out[valid]-original[valid])**2))),
                'clean_reference_feature_bias_pct':100*(bandpower(desired[valid],fs)-bandpower(original[valid],fs))/bandpower(original[valid],fs),
                'float_fixed_rmse_fs':float(np.sqrt(np.mean((float_out[valid]-fixed_out[valid])**2)))})
            if c==0:traces.append((mode,float_out))
    write_rows(out/'multichannel_metrics.csv',rows)
    summary=[]
    for mode in modes:
        selected=[r for r in rows if r['mode']==mode and r['good_channel']]
        summary.append({'mode':mode,'included_good_channels':len(selected),
            **{k:float(np.mean([r[k] for r in selected])) for k in ['float_matched_reference_snr_db','fixed_matched_reference_snr_db','original_signal_error_rms_fs','clean_reference_feature_bias_pct']}})
    write_rows(out/'multichannel_summary.csv',summary)
    result=subprocess.run([str(PROBE),'benchmark'],check=True,text=True,capture_output=True)
    (out/'benchmark.csv').write_text(result.stdout)
    bench=np.genfromtxt(io.StringIO(result.stdout),delimiter=',',names=True)
    _,gd=group_delay((b,a),w=np.array([93.,183.]),fs=fs)
    metadata={'seed':2026,'channels':8,'samples_per_channel':len(t),'fs':fs,
              'block_samples':64,'block_collection_max_ms':64/fs*1000,
              'notch_group_delay_ms_at_93_183_hz':list(gd/fs*1000),
              'median_c_channel_samples_per_second':float(np.median(bench['channel_samples_per_second'])),
              'c_filter_state_bytes_per_channel':int(bench['state_bytes_per_channel'][0]),
              'platform':platform.platform(),'machine':platform.machine(),
              'source_sha256':{str(p):hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in ['multichannel.py','dsp.h','tests/probe.c','analysis.py','validate.py','build.py','sweep.py']},
              'validation':'32 channel/mode C float streams agree with scipy lfilter within 5e-13 FS',
              'limitations':['Host timing excludes I/O, referencing, acquisition, scheduling, and transport.',
                             'Mean throughput does not establish a worst-case real-time deadline.',
                             'Collection latency is separate from frequency-dependent filter delay.',
                             'Channel-quality thresholds are illustrative; clean signals are synthetic.',
                             'Filters run in independent channel calls; benchmark keeps 8 states concurrently.']}
    (out/'streaming_manifest.json').write_text(json.dumps(metadata,indent=2)+'\n')
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        fig,axes=plt.subplots(1,2,figsize=(12,4),constrained_layout=True)
        for mode,y in traces:
            f,power=spectrum(y[valid],fs)
            axes[0].plot(f,10*np.log10(np.maximum(power,1e-20)),label=mode.replace('_',' '))
        axes[0].set(xlim=(40,180),xlabel='Frequency (Hz)',ylabel='PSD (dB FS²/Hz)',title='Channel 0: referencing and notch')
        axes[0].legend(fontsize=7)
        axes[1].bar(range(4),[r['clean_reference_feature_bias_pct'] for r in summary])
        axes[1].set_xticks(range(4),[r['mode'].replace('_','\n') for r in summary],fontsize=7)
        axes[1].set(ylabel='Clean feature bias (%)',title='Referencing also changes desired signal')
        fig.savefig(out/'multichannel.png',dpi=150);plt.close(fig)
    except ImportError:pass
    print('Multichannel reference comparison and C streaming benchmark saved to',out)
    return 0
if __name__=='__main__':raise SystemExit(main())
