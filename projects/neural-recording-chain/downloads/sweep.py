#!/usr/bin/env python3
"""Reproducible acquisition, numerical-precision, and interaction experiments."""
from __future__ import annotations
import argparse
import csv
import hashlib
import itertools
import json
import os
from pathlib import Path
import platform
import random
import shlex
import subprocess
import tempfile
from datetime import datetime,timezone
import numpy as np
import scipy
from analysis import measure, settling_seconds, spectrum
from build import compile_source, compiler_command

ROOT=Path(__file__).resolve().parent
os.environ.setdefault('MPLCONFIGDIR',str(ROOT/'.mplconfig'))
BASE=dict(fs=2000.,internal_fs=80000.,duration=6.,amp=.30,noise_density=.0002,
          line_amp=.12,line_hz=60.,line_drift=0.,line_step=1.,harmonic_amp=0.,
          oob_amp=.10,oob_hz=1350.,analog_fc=650.,gain=1.,notch_hz=60.,notch_q=25.,
          adc_bits=16,state_bits=16,frac_bits=15,coeff_frac=28,acc_bits=62,seed=7,
          rounding='nearest',overflow='saturate',realization='df1',artifact_amp=0.,
          artifact_start=1.,artifact_duration=.02)


def compile_sim(output):
    compile_source(ROOT/'chain_sim.c',output)


def read_trace(path):
    table=np.genfromtxt(path,delimiter=',',names=True)
    if table.size<2:raise ValueError('Trace is empty or too short')
    return {key:table[key] for key in table.dtype.names}


def simulate(binary,params,path):
    cmd=[str(binary),'--out',str(path)]
    for key,value in params.items():cmd += ['--'+key.replace('_','-'),str(value)]
    subprocess.run(cmd,check=True,capture_output=True,text=True)
    return read_trace(path)


def run_case(binary,params,trace_path=None):
    # Always save the complete configuration beside metrics: no hidden defaults.
    if trace_path is not None:
        trace=simulate(binary,params,trace_path)
    else:
        with tempfile.TemporaryDirectory(prefix='chain_') as tmp:
            trace=simulate(binary,params,Path(tmp)/'trace.csv')
    return {**params,**measure(trace,params)}


def write_rows(path,rows):
    if not rows:return
    keys=list(dict.fromkeys(k for row in rows for k in row))
    with Path(path).open('w',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=keys);writer.writeheader();writer.writerows(rows)


def summarize(rows,groups,metrics):
    """Empirical intervals describe repeated simulations, not manufacturing yield."""
    result=[]
    for group in sorted(set(tuple(r[k] for k in groups) for r in rows)):
        selected=[r for r in rows if tuple(r[k] for k in groups)==group]
        for metric in metrics:
            values=np.array([r[metric] for r in selected],float)
            sd=float(values.std(ddof=1)) if len(values)>1 else 0.
            result.append({**dict(zip(groups,group)), 'metric':metric,'n':len(values),
                'mean':float(values.mean()),'std':sd,'sem':sd/np.sqrt(len(values)),
                'p05':float(np.quantile(values,.05)),'median':float(np.median(values)),
                'p95':float(np.quantile(values,.95))})
    return result


def make_plots(outdir,sweeps,bits,mc,trace,base,interactions):
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except ImportError:
        print('matplotlib unavailable; CSV and provenance still saved');return
    factors=['noise_density','gain','analog_fc','adc_bits','fs']
    labels=['Noise density (FS/√Hz)','Gain (V/V)','Each RC pole (Hz)','ADC bits','Sampling rate (Hz)']
    fig,axes=plt.subplots(3,5,figsize=(17,9),constrained_layout=True)
    for col,(factor,label) in enumerate(zip(factors,labels)):
        rows=sorted([r for r in sweeps if r['swept_factor']==factor],key=lambda r:r['swept_value'])
        for j,(metric,ylabel) in enumerate([('output_snr_db','Float SNR (dB)'),('fixed_output_snr_db','Fixed SNR (dB)'),('bandpower_bias_pct','Bandpower bias (%)')]):
            axes[j,col].plot([r['swept_value'] for r in rows],[r[metric] for r in rows],'o-')
            axes[j,col].set_xlabel(label);axes[j,col].grid(alpha=.25)
            if col==0:axes[j,col].set_ylabel(ylabel)
    fig.suptitle('Acquisition tradeoffs — signed feature bias and separate float/fixed quality')
    fig.savefig(outdir/'acquisition_sweep.png',dpi=150);plt.close(fig)
    fig,axes=plt.subplots(1,2,figsize=(10,4),constrained_layout=True)
    adc=[r for r in bits if r['bit_path']=='ADC']
    for metric,label in [('adc_quantization_rms','Measured'),('adc_quantization_rms_ideal','Δ/√12')]:
        axes[0].semilogy([r['bits'] for r in adc],[r[metric] for r in adc],'o-',label=label)
    axes[0].set(xlabel='ADC bits',ylabel='Rounding RMS (FS)',title='ADC quantization')
    for mode in ['same_fractional_precision','same_full_scale_range']:
        rows=[r for r in bits if r['comparison']==mode]
        axes[1].semilogy([r['bits'] for r in rows],[max(r['float_fixed_rmse'],1e-15) for r in rows],'o-',label=mode.replace('_',' '))
    axes[1].set(xlabel='State bits',ylabel='Error RMS (FS)',title='Fixed-point error')
    for ax in axes:ax.legend(fontsize=8);ax.grid(alpha=.25)
    fig.savefig(outdir/'quantization_comparison.png',dpi=150);plt.close(fig)
    fig,axes=plt.subplots(1,2,figsize=(10,4),constrained_layout=True)
    for mode in ['parameter_only','noise_only','combined']:
        rows=[r for r in mc if r['variation']==mode]
        for ax,key in zip(axes,['output_snr_db','bandpower_bias_pct']):
            ax.hist([r[key] for r in rows],bins=12,alpha=.45,label=mode.replace('_',' '));ax.legend(fontsize=8)
    axes[0].set(xlabel='Output SNR (dB)',ylabel='Trials');axes[1].set(xlabel='Signed feature bias (%)')
    fig.savefig(outdir/'monte_carlo.png',dpi=150);plt.close(fig)
    valid=trace['t']>=settling_seconds(base)
    fig,axes=plt.subplots(2,1,figsize=(10,7),constrained_layout=True)
    short=valid&(trace['t']<settling_seconds(base)+.08)
    for key in ['clean_notch','float_notch','fixed_notch']:
        axes[0].plot(trace['t'][short],trace[key][short],label=key)
        f,p=spectrum(trace[key][valid],base['fs']);axes[1].plot(f,10*np.log10(np.maximum(p,1e-30)),label=key)
    f,p=spectrum((trace['fixed_notch']-trace['float_notch'])[valid],base['fs'])
    axes[1].plot(f,10*np.log10(np.maximum(p,1e-30)),label='fixed minus float')
    axes[0].set(xlabel='Time (s)',ylabel='Amplitude (FS)');axes[1].set(xlabel='Frequency (Hz)',ylabel='PSD (dB FS²/Hz)')
    for ax in axes:ax.legend();ax.grid(alpha=.25)
    fig.savefig(outdir/'baseline_signals.png',dpi=150);plt.close(fig)
    names=list(dict.fromkeys(r['experiment'] for r in interactions))
    fig,axes=plt.subplots(2,2,figsize=(11,8),constrained_layout=True)
    for ax,name in zip(axes.flat,names):
        rows=[r for r in interactions if r['experiment']==name]
        for value in sorted(set(r['y_value'] for r in rows)):
            rr=sorted([r for r in rows if r['y_value']==value],key=lambda r:r['x_value'])
            xs=sorted(set(r['x_value'] for r in rr))
            means=[np.mean([r['fixed_output_snr_db'] for r in rr if r['x_value']==x]) for x in xs]
            stds=[np.std([r['fixed_output_snr_db'] for r in rr if r['x_value']==x],ddof=1) for x in xs]
            ax.errorbar(xs,means,yerr=stds,fmt='o-',capsize=3,label=f"{rr[0]['y_factor']}={value}")
        ax.set(title=name,xlabel=rows[0]['x_factor'],ylabel='Fixed output SNR (dB)');ax.legend(fontsize=7);ax.grid(alpha=.25)
    fig.savefig(outdir/'interactions.png',dpi=150);plt.close(fig)


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--outdir',default='results_v2');ap.add_argument('--no-compile',action='store_true')
    ap.add_argument('--mc-trials',type=int,default=40);ap.add_argument('--repeats',type=int,default=3)
    ap.add_argument('--duration',type=float,default=6.);ap.add_argument('--seed',type=int,default=123)
    ap.add_argument('--adc-bit-widths',default='8,16,17');ap.add_argument('--fixed-bit-widths',default='8,16,17')
    ap.add_argument('--fixed-frac-bits',type=int);ap.add_argument('--noise-density',type=float,default=BASE['noise_density'])
    ap.add_argument('--sampling-rate',type=float,default=BASE['fs']);ap.add_argument('--internal-fs',type=float,default=BASE['internal_fs'])
    ap.add_argument('--afe-gain',type=float,default=1.);ap.add_argument('--afe-bandwidth',type=float,default=650.)
    ap.add_argument('--signal-amplitude',type=float,default=.30);ap.add_argument('--adc-bits',type=int,default=16)
    args=ap.parse_args()
    try:
        adc_widths=sorted(set(map(int,args.adc_bit_widths.split(','))))
        fixed_widths=sorted(set(map(int,args.fixed_bit_widths.split(','))))
        if not adc_widths or min(adc_widths)<2 or max(adc_widths)>24:raise ValueError('ADC widths must be 2..24')
        if not fixed_widths or min(fixed_widths)<2 or max(fixed_widths)>31:raise ValueError('State widths must be 2..31')
        shared=args.fixed_frac_bits if args.fixed_frac_bits is not None else min(fixed_widths)-1
        if shared<0 or shared>=min(fixed_widths):raise ValueError('Shared fractional width must be smaller than every state width')
        if args.mc_trials<2 or args.repeats<2:raise ValueError('Use >=2 MC trials and repeats to measure variation')
        if args.seed<0 or args.seed+args.mc_trials+args.repeats>=2**32:raise ValueError('Seed range exceeds uint32')
        base={**BASE,'fs':args.sampling_rate,'internal_fs':args.internal_fs,'duration':args.duration,
              'noise_density':args.noise_density,'gain':args.afe_gain,'analog_fc':args.afe_bandwidth,
              'amp':args.signal_amplitude,'adc_bits':args.adc_bits,'seed':args.seed}
        for v in base.values():
            if isinstance(v,(int,float)) and not np.isfinite(v):raise ValueError('All numeric inputs must be finite')
        if base['fs']<=120 or base['analog_fc']<=0 or base['gain']<=0 or base['amp']<=0 or base['noise_density']<0 or not 2<=base['adc_bits']<=24:raise ValueError('Invalid baseline settings')
        if base['duration']<settling_seconds(base)+.5:raise ValueError('Duration too short for baseline settling')
    except ValueError as e:ap.error(str(e))
    outdir=Path(args.outdir).resolve();outdir.mkdir(parents=True,exist_ok=True)
    binary=ROOT/'chain_sim'
    if not args.no_compile:compile_sim(binary)
    baseline=run_case(binary,base,outdir/'baseline_trace.csv');write_rows(outdir/'baseline_metrics.csv',[baseline])
    print('Baseline:',{k:round(baseline[k],5) for k in ['output_snr_db','fixed_output_snr_db','bandpower_bias_pct','adc_clipping_pct']},flush=True)
    bits=[]
    for n in adc_widths:
        bits.append({**run_case(binary,{**base,'adc_bits':n}),'bit_path':'ADC','bits':n,'comparison':'ADC resolution'})
    for mode in ['same_fractional_precision','same_full_scale_range']:
        for n in fixed_widths:
            bits.append({**run_case(binary,{**base,'state_bits':n,'frac_bits':shared if mode=='same_fractional_precision' else n-1}),
                         'bit_path':'fixed_state','bits':n,'comparison':mode})
    write_rows(outdir/'bit_comparison.csv',bits)
    widths=[]
    for n,f,mode in [(16,14,'same_fractional_precision'),(17,14,'same_fractional_precision'),(16,15,'same_full_scale_range'),(17,16,'same_full_scale_range')]:
        widths.append({**run_case(binary,{**base,'state_bits':n,'frac_bits':f}),'comparison':mode})
    write_rows(outdir/'fixed_point_width_sweep.csv',widths)
    budget=[]
    for row in [r for r in bits if r['bit_path']=='ADC']:
        for name in ['afe_noise','line','oob','artifact','rounding','clipping']:
            budget.append({'adc_bits':row['bits'],'stage':'post_notch','source':name,'rms_fs':row['post_notch_'+name+'_rms'],
                           'cross_power_fs2':row['component_cross_power'],'closure_max_fs':row['error_reconstruction_max']})
    write_rows(outdir/'noise_budget.csv',budget)
    factors={'noise_density':[.00005,.0002,.0005],'gain':[.5,1.,3.], 'analog_fc':[250.,650.,1200.],
             'adc_bits':sorted(set([6,8,12,16,*adc_widths,base['adc_bits']])),'fs':[500.,1000.,2000.,4000.]}
    sweeps=[]
    for factor,values in factors.items():
        for value in values:sweeps.append({**run_case(binary,{**base,factor:value}),'swept_factor':factor,'swept_value':value})
    write_rows(outdir/'acquisition_sweep.csv',sweeps)
    print('Acquisition and bit-width sweeps complete; running paired interactions.',flush=True)
    specs=[('gain_adc','gain',[.5,1.,3.],'adc_bits',[8,12,16]),
           ('bandwidth_sampling','analog_fc',[250.,650.,1200.],'fs',[500.,2000.,4000.]),
           ('state_fraction','state_bits',[12,16,17],'frac_bits',[7,10,11]),
           ('notch_coefficient','notch_q',[10.,25.,100.],'coeff_frac',[8,12,20,28])]
    interactions=[]
    for name,x,xs,y,ys in specs:
        for xv,yv in itertools.product(xs,ys):
            p={**base,x:xv,y:yv}
            # Narrow-notch cases need a longer warm-up and >=2 s analysis.
            p['duration']=max(base['duration'],settling_seconds(p)+2)
            for repeat in range(args.repeats):
                interactions.append({**run_case(binary,{**p,'seed':args.seed+repeat}),
                    'experiment':name,'x_factor':x,'x_value':xv,'y_factor':y,'y_value':yv,'repeat':repeat})
    write_rows(outdir/'interactions.csv',interactions)
    metrics=['output_snr_db','fixed_output_snr_db','bandpower_bias_pct','float_fixed_rmse']
    write_rows(outdir/'interaction_summary.csv',summarize(interactions,['experiment','x_value','y_value'],metrics))
    # Paired replicate differences, rather than unrelated error bars.
    deltas=[]
    for name,x,xs,y,ys in specs:
        for yv in ys:
            for repeat in range(args.repeats):
                rows=[r for r in interactions if r['experiment']==name and r['y_value']==yv and r['repeat']==repeat]
                ref=next(r for r in rows if r['x_value']==xs[0])
                for row in rows:
                    deltas.append({'experiment':name,'x_value':row['x_value'],'y_value':yv,'repeat':repeat,
                                   **{k:row[k]-ref[k] for k in metrics}})
    write_rows(outdir/'paired_deltas.csv',deltas)
    write_rows(outdir/'paired_delta_summary.csv',summarize(deltas,['experiment','x_value','y_value'],metrics))
    numerical=[]
    for cf,acc,rounding,overflow,realization in itertools.product([2,8,16,28],[32,48,62],['nearest','truncate'],['saturate','wrap'],['df1','df2t']):
        numerical.append(run_case(binary,{**base,'coeff_frac':cf,'acc_bits':acc,'rounding':rounding,'overflow':overflow,'realization':realization}))
    write_rows(outdir/'numerical_sweep.csv',numerical)
    stress=[]
    conditions=[('matched',{}),('offset',{'line_hz':60.5}),('drift',{'line_drift':.2}),
                ('harmonic',{'harmonic_amp':.06}),('amplitude_step',{'line_step':3.}),
                ('artifact',{'artifact_amp':4.,'artifact_start':2.,'artifact_duration':.02})]
    for q in [10.,25.,100.]:
        for scenario,overrides in conditions:
            p={**base,**overrides,'notch_q':q};p['duration']=max(base['duration'],settling_seconds(p)+4)
            stress.append({**run_case(binary,p),'scenario':scenario})
    write_rows(outdir/'notch_stress.csv',stress)
    rng=random.Random(args.seed);mc=[]
    for trial in range(args.mc_trials):
        # Unbounded log-normal distributions: hypothetical sensitivity, not yield.
        varied={key:base[key]*np.exp(rng.gauss(0,sigma)) for key,sigma in [('gain',.08),('analog_fc',.12),('noise_density',.25)]}
        for mode in ['parameter_only','noise_only','combined']:
            p={**base,**(varied if mode!='noise_only' else {}),'seed':args.seed if mode=='parameter_only' else args.seed+trial+1}
            mc.append({**run_case(binary,p),'variation':mode,'trial':trial})
    write_rows(outdir/'monte_carlo.csv',mc)
    write_rows(outdir/'monte_carlo_summary.csv',summarize(mc,['variation'],metrics+['adc_clipping_pct']))
    source_names=['chain_sim.c','dsp.h','analysis.py','sweep.py','build.py']
    manifest={'schema_version':2,'created_utc':datetime.now(timezone.utc).isoformat(),'baseline':base,
              'arguments':vars(args),'python':platform.python_version(),'numpy':np.__version__,'scipy':scipy.__version__,
              'platform':platform.platform(),'compiler':subprocess.run(shlex.split(os.environ.get('CC','cc'))+['--version'],capture_output=True,text=True,check=True).stdout.splitlines()[0],
              'compiler_command':compiler_command(),
              'source_sha256':{n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in source_names},
              'binary_sha256':hashlib.sha256(binary.read_bytes()).hexdigest(),
              'units':{'amplitude':'normalized ADC full scale (FS)','noise_density':'FS/sqrt(Hz), one-sided','power':'FS^2','psd':'FS^2/Hz'},
              'experiment_rows':{'acquisition':len(sweeps),'interactions':len(interactions),'numerical':len(numerical),'notch_stress':len(stress),'monte_carlo':len(mc)},
              'notes':['Original results/ is legacy schema; do not compare raw bandpower numbers.',
                       'SNR references clean through same linear filters; distortion is separately reported.',
                       'Monte Carlo priors are hypothetical and unbounded; not device yield.',
                       'Fixed coefficients and accumulator are software-emulated, not target benchmarks.']}
    (outdir/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    make_plots(outdir,sweeps,bits,mc,read_trace(outdir/'baseline_trace.csv'),base,interactions)
    print(f'Wrote reproducible experiment tables and plots to {outdir}',flush=True)
    return 0
if __name__=='__main__':raise SystemExit(main())
