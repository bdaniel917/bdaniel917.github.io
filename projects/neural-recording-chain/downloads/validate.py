#!/usr/bin/env python3
"""Independent numerical/analytical validation; writes evidence, fails on any error."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import os
import shlex
import subprocess
import tempfile
import unittest
from datetime import datetime,timezone
import numpy as np
from scipy.signal import lfilter, freqz
from analysis import (bandpower,spectrum,notch_coeff,settling_seconds,transfer,
                      tone_amplitude,folded_frequency,pole_metrics)
from sweep import BASE,ROOT,compile_sim,simulate
from build import compile_source

PROBE=ROOT/'tests'/'probe'


def compile_probe():
    compile_source(ROOT/'tests/probe.c',PROBE)


def probe(x,p=None,block=1):
    p={**BASE,**(p or {})}
    with tempfile.TemporaryDirectory() as tmp:
        inp=Path(tmp)/'input.txt';out=Path(tmp)/'out.csv';np.savetxt(inp,x,fmt='%.17g')
        cmd=[str(PROBE),str(inp),str(out),*[str(p[k]) for k in ['fs','notch_hz','notch_q','state_bits','frac_bits','coeff_frac','acc_bits']],
             str(int(p['rounding']=='truncate')),str(int(p['overflow']=='wrap')),
             f"{int(p['realization']=='df2t')}:{block}"]
        proc=subprocess.run(cmd,check=True,capture_output=True,text=True)
        table=np.genfromtxt(out,delimiter=',',names=True)
        return table,proc.stderr


def reference_fixed(x,p):
    """Python arbitrary-width integers; direct form I with explicit MAC limiting.

    This oracle has no C overflow and uses explicit rational division/remainders.
    """
    b,a=notch_coeff(p['fs'],p['notch_hz'],p['notch_q']);scale=2**p['coeff_frac']
    round_away=lambda v:int(np.copysign(np.floor(abs(v)+.5),v))
    b=[round_away(v*scale) for v in b];a=[round_away(v*scale) for v in a]
    def limit(v,bits):
        lo=-(2**(bits-1));hi=2**(bits-1)-1
        if p['overflow']=='wrap':return (v-lo)%(2**bits)+lo
        return min(hi,max(lo,v))
    def shift(v):
        sign=-1 if v<0 else 1;q,r=divmod(abs(v),scale)
        return sign*(q+int(p['rounding']=='nearest' and 2*r>=scale))
    x1=x2=y1=y2=z1=z2=0;result=[]
    for v in x:
        xx=limit(round_away(v*2**p['frac_bits']),p['state_bits'])
        if p['realization']=='df1':
            acc=0
            for term in [b[0]*xx,b[1]*x1,b[2]*x2,-a[1]*y1,-a[2]*y2]:acc=limit(acc+term,p['acc_bits'])
            yy=limit(shift(acc),p['state_bits']);x2,x1=x1,xx;y2,y1=y1,yy
        else:
            yy=limit(shift(limit(b[0]*xx+z1*scale,p['acc_bits'])),p['state_bits'])
            z1=limit(shift(limit(b[1]*xx-a[1]*yy+z2*scale,p['acc_bits'])),p['state_bits'])
            z2=limit(shift(limit(b[2]*xx-a[2]*yy,p['acc_bits'])),p['state_bits'])
        result.append(yy/2**p['frac_bits'])
    return np.array(result)


EVIDENCE={}
class Validation(unittest.TestCase):
    def sim(self,**overrides):
        with tempfile.TemporaryDirectory() as tmp:return simulate(ROOT/'chain_sim',{**BASE,**overrides},Path(tmp)/'t.csv')

    def test_adc_and_rounding_boundaries(self):
        subprocess.run([str(PROBE),'selftest'],check=True,capture_output=True)

    def test_float_independent_reference_and_block_state(self):
        rng=np.random.default_rng(51);x=rng.uniform(-.5,.5,4099);b,a=notch_coeff(2000)
        reference=lfilter(b,a,x)
        for realization in ['df1','df2t']:
            single,_=probe(x,{'realization':realization})
            for block in [1,17,64,1024]:
                actual,_=probe(x,{'realization':realization},block)
                np.testing.assert_array_equal(actual['float'],single['float'])
                np.testing.assert_array_equal(actual['fixed'],single['fixed'])
                np.testing.assert_allclose(actual['float'],reference,atol=2e-13,rtol=0)
        EVIDENCE['float_reference_max_error']=float(max(abs(single['float']-reference)))

    def test_fixed_bit_accurate_reference(self):
        x=np.r_[np.zeros(10),np.random.default_rng(5).uniform(-.999,.999,1001)]
        for realization in ['df1','df2t']:
            for rounding in ['nearest','truncate']:
                for overflow in ['saturate','wrap']:
                    for bits,frac,cf,acc in [(8,7,8,16),(16,14,28,48),(31,30,28,62)]:
                        p={**BASE,'state_bits':bits,'frac_bits':frac,'coeff_frac':cf,'acc_bits':acc,
                           'rounding':rounding,'overflow':overflow,'realization':realization}
                        actual,_=probe(x,p,37)
                        np.testing.assert_array_equal(actual['fixed'],reference_fixed(x,p))

    def test_notch_response_and_decay(self):
        b,a=notch_coeff(2000)
        self.assertLess(abs(transfer(b,a,60,2000)),1e-12)
        self.assertGreater(abs(transfer(b,a,93,2000)),.99)
        # Independent scipy frequency response agrees with explicit polynomial.
        f=np.array([0.,59.,60.,61.,93.,183.,500.]);_,h=freqz(b,a,worN=f,fs=2000)
        np.testing.assert_allclose(h,transfer(b,a,f,2000),atol=1e-13)
        t=np.arange(12000)/2000.;x=.2*np.sin(2*np.pi*60*t)
        table,_=probe(x,block=64);valid=t>=settling_seconds(BASE)
        amp=tone_amplitude(table['float'],t,60,valid)
        self.assertLess(amp,.0001)
        EVIDENCE['matched_notch_residual_peak_fs']=amp

    def test_impulse_and_zero_input_limit_cycles(self):
        x=np.zeros(16000);x[0]=.5;b,a=notch_coeff(2000)
        table,_=probe(x)
        np.testing.assert_allclose(table['float'],lfilter(b,a,x),atol=2e-14,rtol=0)
        # Persistent fixed output is recorded, not falsely required to be zero.
        for bits in [8,16,17]:
            result,_=probe(x,{'state_bits':bits,'frac_bits':bits-1})
            EVIDENCE[f'zero_input_tail_rms_{bits}_fs']=float(np.sqrt(np.mean(result['fixed'][-4000:]**2)))
        zeros,_=probe(np.zeros(1000));np.testing.assert_array_equal(zeros['fixed'],0)

    def test_quantization_uniform_rms(self):
        x=np.random.default_rng(30).uniform(-.9,.9,200000)
        ratios=[]
        for bits in [8,16,17]:
            scale=2**(bits-1);codes=np.copysign(np.floor(abs(x)*scale+.5),x)
            actual=float(np.sqrt(np.mean((codes/scale-x)**2)));ideal=1/scale/np.sqrt(12)
            self.assertLess(abs(actual/ideal-1),.01);ratios.append(actual/ideal)
        EVIDENCE['uniform_quantization_rms_to_ideal']=ratios
        # End-to-end C trace decomposition and quantization under dither/noise.
        trace=self.sim(duration=2.,adc_bits=8)
        np.testing.assert_allclose(trace['adc']-trace['analog'],trace['quantization_error']+trace['clipping_error'],atol=1e-16)
        self.assertLess(abs(np.std(trace['quantization_error'])/(2/256/np.sqrt(12))-1),.04)

    def test_psd_normalization_and_bandpower(self):
        for fs in [500.,1000.,2000.,4000.]:
            for duration in [2.,4.]:
                t=np.arange(int(fs*duration))/fs;x=.3*np.sin(2*np.pi*93*t)
                self.assertAlmostEqual(bandpower(x,fs),.3**2/2,places=7)
                f,p=spectrum(x,fs);w=np.hanning(len(x));expected=np.sum(((x-x.mean())*w)**2)/np.sum(w*w)
                self.assertAlmostEqual(np.sum(p)*fs/len(x),expected,places=13)
        for n in [999,1000]:
            x=np.random.default_rng(n).normal(size=n);f,p=spectrum(x,2000);w=np.hanning(n)
            self.assertAlmostEqual(np.sum(p)*2000/n,np.sum(((x-x.mean())*w)**2)/np.sum(w*w),places=12)

    def test_alias_edges_and_timestamps(self):
        for hz,expected in [(1350,650),(2000,0),(3000,1000)]:self.assertEqual(folded_frequency(hz,2000),expected)
        t=np.arange(1000)/2000;valid=np.ones(1000,bool)
        self.assertAlmostEqual(tone_amplitude(np.ones(1000)*.2,t,0,valid),.2)
        self.assertAlmostEqual(tone_amplitude(.2*(-1.)**np.arange(1000),t,1000,valid),.2)
        trace=self.sim(duration=2.,noise_density=0)
        np.testing.assert_allclose(trace['neural'],.3*(.62*np.sin(2*np.pi*93*trace['t'])+.38*np.sin(2*np.pi*183*trace['t'])),atol=1e-15)
        np.testing.assert_allclose(np.diff(trace['t']),1/2000,atol=1e-15)
        self.assertAlmostEqual(trace['t'][0],39/80000)
        # Actual folded component measured at nondegenerate alias.
        trace=self.sim(fs=500.,duration=4.,noise_density=0)
        valid=trace['t']>1
        amp=tone_amplitude(trace['oob_analog'],trace['t'],150,valid)
        self.assertGreater(amp,.01)

    def test_analog_transfer_and_internal_rate_convergence(self):
        errors=[]
        for internal in [40000.,80000.,160000.]:
            trace=self.sim(internal_fs=internal,duration=3.,noise_density=0,line_amp=0,oob_amp=0)
            valid=trace['t']>1;t=trace['t'][valid]
            d=np.column_stack([v for hz in [93,183] for v in [np.sin(2*np.pi*hz*t),np.cos(2*np.pi*hz*t)]])
            fit=np.linalg.lstsq(d,trace['clean_analog'][valid],rcond=None)[0];gains=[]
            for j,(hz,weight) in enumerate([(93,.62),(183,.38)]):
                measured=np.hypot(*fit[2*j:2*j+2])/(.3*weight)
                exact=1/(1+(hz/650)**2);errors.append(abs(measured-exact));gains.append(measured)
            EVIDENCE[f'analog_measured_gain_{int(internal)}']=gains
        self.assertLess(max(errors),.0002)
        self.assertLess(max(errors[-2:]),max(errors[:2]))
        EVIDENCE['analog_max_gain_error']=max(errors)

    def test_noise_density_and_common_realization(self):
        noise=[]
        for fs in [500.,1000.,2000.,4000.]:
            trace=self.sim(fs=fs,duration=12.,amp=0,line_amp=0,oob_amp=0)
            valid=trace['t']>1;noise.append(np.std(trace['afe_noise_analog'][valid]))
        # Analog white-noise one-sided ENBW of two equal RC poles is pi*fc/4.
        expected=BASE['noise_density']*np.sqrt(np.pi*BASE['analog_fc']/4)
        self.assertLess(max(abs(np.array(noise)/expected-1)),.06)
        EVIDENCE['noise_rms_by_fs_500_1000_2000_4000']=list(map(float,noise))
        EVIDENCE['analog_noise_rms_theory']=float(expected)
        slow=self.sim(fs=1000.,duration=2.)
        fast=self.sim(fs=2000.,duration=2.)
        # Samples have a common endpoint at odd indices of the faster stream.
        np.testing.assert_array_equal(slow['afe_noise_analog'],fast['afe_noise_analog'][1::2])
        levels=[]
        for internal in [40000.,80000.,160000.]:
            trace=self.sim(internal_fs=internal,duration=12.,amp=0,line_amp=0,oob_amp=0)
            levels.append(float(np.std(trace['afe_noise_analog'][trace['t']>1])))
        self.assertLess(max(abs(np.array(levels)/expected-1)),.07)
        EVIDENCE['noise_rms_internal_rate_convergence']=levels

    def test_metrics_closure_clipping_and_reference(self):
        from analysis import measure
        p={**BASE,'duration':3.,'gain':3.};trace=self.sim(duration=3.,gain=3.)
        m=measure(trace,p);valid=trace['t']>=m['analysis_start_s']
        self.assertLess(m['error_reconstruction_max'],5e-13)
        self.assertEqual(m['adc_clip_count'],int(sum(trace['adc_clip'][valid])))
        self.assertAlmostEqual(m['adc_clipping_pct'],100*m['adc_clip_count']/m['analysis_samples'])
        self.assertGreater(m['adc_clip_count'],0)
        self.assertIn('fixed_output_snr_db',m)
        EVIDENCE['component_reconstruction_max_fs']=m['error_reconstruction_max']
        clean_p={**BASE,'duration':3.,'noise_density':0.,'line_amp':0.,'oob_amp':0.,'adc_bits':24}
        clean=self.sim(**{k:clean_p[k] for k in ['duration','noise_density','line_amp','oob_amp','adc_bits']})
        mm=measure(clean,clean_p)
        self.assertGreater(mm['output_snr_db'],100)
        self.assertLess(mm['clean_path_bandpower_bias_pct'],-1)
        EVIDENCE['clean_path_bias_with_high_snr_pct']=mm['clean_path_bandpower_bias_pct']

    def test_saturation_recovery(self):
        trace=self.sim(duration=8.,artifact_amp=4.,artifact_start=2.,artifact_duration=.02)
        control=self.sim(duration=8.)
        t=trace['t'];after=t>2.02;settled=t>6.
        self.assertGreater(sum(trace['adc_clip']),0)
        self.assertLess(float(max(abs(trace['float_notch'][settled]-control['float_notch'][settled]))),1e-10)
        for path in ['float_notch','fixed_notch']:
            delta=abs(trace[path]-control[path]);threshold=1e-3
            bad=np.flatnonzero(after&(delta>threshold))
            EVIDENCE[path+'_artifact_recovery_to_1mFS_s']=float(t[bad[-1]]-2.02) if len(bad) else 0.
            EVIDENCE[path+'_artifact_tail_max_fs']=float(max(delta[settled]))
        EVIDENCE['artifact_adc_clip_count']=int(sum(trace['adc_clip']))

    def test_pole_quantization_is_reported(self):
        self.assertEqual(pole_metrics(BASE)['quantized_poles_stable'],1)
        coarse={**BASE,'coeff_frac':2,'notch_q':100}
        self.assertEqual(pole_metrics(coarse)['quantized_poles_stable'],0)

    def test_invalid_inputs_and_frac_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            for flag,value in [('--fs','nan'),('--gain','inf'),('--state-bits','-1'),('--internal-fs','79999'),('--duration','0'),('--notch-hz','-1'),('--adc-bits','25')]:
                proc=subprocess.run([str(ROOT/'chain_sim'),'--out',str(Path(tmp)/'t.csv'),flag,value],capture_output=True)
                self.assertEqual(proc.returncode,2,(flag,value))
            proc=subprocess.run([str(ROOT/'chain_sim'),'--out',str(Path(tmp)/'t.csv'),'--state-bits','8','--duration','1'],capture_output=True)
            self.assertEqual(proc.returncode,0)


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--outdir',default='results_v2');args=ap.parse_args()
    compile_sim(ROOT/'chain_sim');compile_probe()
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(Validation)
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    out=Path(args.outdir);out.mkdir(parents=True,exist_ok=True)
    report={'created_utc':datetime.now(timezone.utc).isoformat(),'tests_run':result.testsRun,
            'passed':result.wasSuccessful(),'failures':len(result.failures),'errors':len(result.errors),
            'baseline':BASE,
            'test_ids':[name for name in unittest.defaultTestLoader.getTestCaseNames(Validation)],
            'evidence':EVIDENCE,'source_sha256':{n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in ['dsp.h','chain_sim.c','analysis.py','validate.py','tests/probe.c','build.py','sweep.py']},
            'failure_details':[{'test':str(test),'traceback':tb} for test,tb in result.failures+result.errors]}
    (out/'validation.json').write_text(json.dumps(report,indent=2)+'\n')
    return 0 if result.wasSuccessful() else 1
if __name__=='__main__':raise SystemExit(main())
