/* Generic single-channel acquisition model; not a model of a specific device. */
#include "dsp.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <limits.h>

typedef struct {
    double fs, internal_fs, duration, amp, noise_density, line_amp,line_hz,line_drift,line_step;
    double harmonic_amp,oob_amp,oob_hz,analog_fc,gain,notch_hz,notch_q,artifact_amp,artifact_start,artifact_duration;
    unsigned adc_bits,state_bits,frac_bits,coeff_frac,acc_bits,seed;
    int truncate,wrap,realization;
    const char *out;
} Config;
static void usage(void) {
    puts("chain_sim --out FILE [options]\n"
         "--fs 2000 --internal-fs 80000 --duration 6 --amp .30\n"
         "--noise-density .0002 (normalized amplitude/sqrt(Hz), one-sided)\n"
         "--gain 1 --analog-fc 650 (EACH RC pole, not overall -3 dB bandwidth)\n"
         "--line-amp .12 --line-hz 60 --line-drift 0 (Hz/s) --line-step 1 (second-half multiplier)\n"
         "--harmonic-amp 0 (120 Hz) --oob-amp .10 --oob-hz 1350\n"
         "--notch-hz 60 --notch-q 25 --adc-bits 16 --state-bits 16\n"
         "--frac-bits N (defaults state-bits-1) --coeff-frac 28 --acc-bits 62\n"
         "--rounding nearest|truncate --overflow saturate|wrap --realization df1|df2t\n"
         "--artifact-amp 0 --artifact-start 1 --artifact-duration .02 --seed 7\n"
         "internal-fs/fs must be an integer; use a common internal-fs for paired experiments.");
}
static void fail(const char *s){fprintf(stderr,"%s\n",s);exit(2);}
static double number(const char *s){char *end;errno=0;double x=strtod(s,&end);if(end==s||*end||errno||!isfinite(x))fail("Require finite numeric argument");return x;}
static unsigned integer(const char *s){double x=number(s);if(x<0||x>UINT_MAX||floor(x)!=x)fail("Require nonnegative integer argument");return (unsigned)x;}
static Config parse(int argc,char **argv){
    Config c={.fs=2000,.internal_fs=80000,.duration=6,.amp=.30,.noise_density=.0002,.line_amp=.12,.line_hz=60,.line_step=1,
        .oob_amp=.10,.oob_hz=1350,.analog_fc=650,.gain=1,.notch_hz=60,.notch_q=25,.adc_bits=16,.state_bits=16,
        .coeff_frac=28,.acc_bits=62,.seed=7,.artifact_start=1,.artifact_duration=.02};
    int explicit_frac=0;
    for(int i=1;i<argc;i++){
        const char *k=argv[i];if(!strcmp(k,"--help")){usage();exit(0);}if(i+1>=argc)fail("Missing option value");const char *v=argv[++i];
#define D(flag,field) if(!strcmp(k,flag)) c.field=number(v)
#define U(flag,field) if(!strcmp(k,flag)) c.field=integer(v)
        D("--fs",fs);else D("--internal-fs",internal_fs);else D("--duration",duration);else D("--amp",amp);
        else D("--noise-density",noise_density);else D("--line-amp",line_amp);else D("--line-hz",line_hz);
        else D("--line-drift",line_drift);else D("--line-step",line_step);else D("--harmonic-amp",harmonic_amp);
        else D("--oob-amp",oob_amp);else D("--oob-hz",oob_hz);else D("--analog-fc",analog_fc);else D("--gain",gain);
        else D("--notch-hz",notch_hz);else D("--notch-q",notch_q);else D("--artifact-amp",artifact_amp);
        else D("--artifact-start",artifact_start);else D("--artifact-duration",artifact_duration);
        else U("--adc-bits",adc_bits);else U("--state-bits",state_bits);else U("--coeff-frac",coeff_frac);
        else U("--acc-bits",acc_bits);else U("--seed",seed);
        else if(!strcmp(k,"--frac-bits")){c.frac_bits=integer(v);explicit_frac=1;}
        else if(!strcmp(k,"--out"))c.out=v;
        else if(!strcmp(k,"--rounding")){if(strcmp(v,"nearest")&&strcmp(v,"truncate"))fail("Invalid rounding");c.truncate=!strcmp(v,"truncate");}
        else if(!strcmp(k,"--overflow")){if(strcmp(v,"saturate")&&strcmp(v,"wrap"))fail("Invalid overflow");c.wrap=!strcmp(v,"wrap");}
        else if(!strcmp(k,"--realization")){if(strcmp(v,"df1")&&strcmp(v,"df2t"))fail("Invalid realization");c.realization=!strcmp(v,"df2t");}
        else fail("Unknown option (old --noise was replaced by --noise-density)");
#undef D
#undef U
    }
    if(!explicit_frac)c.frac_bits=c.state_bits?c.state_bits-1:0;
    if(!c.out)fail("--out required");
    if(c.fs<=2*c.notch_hz||c.notch_hz<=0||c.notch_q<=0||c.analog_fc<=0||c.gain<=0||c.duration<=0||c.fs<1)fail("Invalid positive frequency/gain/duration");
    double ratio=c.internal_fs/c.fs;
    if(ratio<1||ratio>100000||fabs(ratio-round(ratio))>1e-9)fail("internal-fs/fs must be an integer in 1..100000");
    if(c.internal_fs<=2*fmax(183,fmax(c.oob_hz,fmax(120,c.line_hz+fabs(c.line_drift)*c.duration))))fail("Internal Nyquist too low for generated tones");
    if(c.internal_fs<20*c.analog_fc)fail("Use internal-fs >=20*analog-fc; verify convergence independently");
    if(c.adc_bits<2||c.adc_bits>24||c.state_bits<2||c.state_bits>31||c.frac_bits>=c.state_bits||c.coeff_frac>28||c.coeff_frac<2||c.acc_bits<8||c.acc_bits>62)fail("Invalid bit width");
    if(c.amp<0||c.noise_density<0||c.line_amp<0||c.line_hz<=0||c.line_step<0||c.harmonic_amp<0||c.oob_amp<0||c.oob_hz<=0||c.artifact_start<0||c.artifact_duration<0)fail("Invalid source amplitude/frequency/time");
    if(c.duration*c.internal_fs>2e8||c.duration*c.fs<2||c.gain*(c.amp+c.line_amp*fmax(1,c.line_step)+c.harmonic_amp+c.oob_amp+fabs(c.artifact_amp)+20*c.noise_density*sqrt(c.internal_fs/2))>1e6)fail("Run exceeds safe numerical/work limits");
    return c;
}
static uint32_t rng;
static double uniform01(void){uint32_t x=rng;x^=x<<13;x^=x>>17;x^=x<<5;rng=x;return ((double)x+.5)/4294967296.;}
static double gaussian(void){double a=uniform01(),b=uniform01();return sqrt(-2*log(a))*cos(2*DSP_PI*b);}
typedef struct{double x,y;} RC;
static double rc_step(RC *r,double v,double a){r->x=(1-a)*v+a*r->x;r->y=(1-a)*r->x+a*r->y;return r->y;}
int main(int argc,char **argv){
    Config c=parse(argc,argv);FILE *out=fopen(c.out,"w");if(!out){perror(c.out);return 1;}rng=c.seed?c.seed:1;
    unsigned ratio=(unsigned)llround(c.internal_fs/c.fs);size_t n=(size_t)llround(c.fs*c.duration);
    double a=exp(-2*DSP_PI*c.analog_fc/c.internal_fs),sigma=c.noise_density*sqrt(c.internal_fs/2);
    RC paths[5]={{0}};Coeff b=notch_coeff(c.fs,c.notch_hz,c.notch_q);
    FloatFilter ff={.c=b,.realization=c.realization},cf=ff,af=ff;
    FixedFilter q=fixed_init(b,c.state_bits,c.frac_bits,c.coeff_frac,c.acc_bits,c.truncate,c.wrap,c.realization);
    fprintf(out,"t,neural,clean_analog,analog,afe_noise_analog,line_analog,oob_analog,artifact_analog,adc,adc_clip,quantization_error,clipping_error,analog_notch,float_notch,clean_notch,fixed_notch,fixed_input_clip,fixed_state_clip,accumulator_clip\n");
    for(size_t k=0;k<n;k++){
        double t=0,s=0,v[5]={0};
        for(unsigned j=0;j<ratio;j++){
            /* First sample at t=0; every stored value has its actual time label. */
            t=(double)(k*ratio+j)/c.internal_fs;
            s=c.amp*(.62*sin(2*DSP_PI*93*t)+.38*sin(2*DSP_PI*183*t));
            double line=c.line_amp*(t>=c.duration/2?c.line_step:1)*sin(2*DSP_PI*(c.line_hz*t+.5*c.line_drift*t*t))+c.harmonic_amp*sin(2*DSP_PI*120*t);
            double inputs[5]={s,sigma*gaussian(),line,c.oob_amp*sin(2*DSP_PI*c.oob_hz*t),
                (t>=c.artifact_start&&t<c.artifact_start+c.artifact_duration)?c.artifact_amp:0};
            for(int m=0;m<5;m++)v[m]=rc_step(&paths[m],c.gain*inputs[m],a);
        }
        double analog=v[0]+v[1]+v[2]+v[3]+v[4],qe,ce;int clipped;
        double adc=adc_step(analog,c.adc_bits,&clipped,&qe,&ce);
        double yf=float_step(&ff,adc),yc=float_step(&cf,v[0]),ya=float_step(&af,analog);
        uint64_t ic=q.input_clips,sc=q.state_clips,ac=q.acc_clips;
        double yq=ldexp((double)fixed_step(&q,fixed_input(&q,adc)),-(int)c.frac_bits);
        fprintf(out,"%.17g,%.17g,%.17g,%.17g,%.17g,%.17g,%.17g,%.17g,%.17g,%d,%.17g,%.17g,%.17g,%.17g,%.17g,%.17g,%llu,%llu,%llu\n",
            t,s,v[0],analog,v[1],v[2],v[3],v[4],adc,clipped,qe,ce,ya,yf,yc,yq,
            (unsigned long long)(q.input_clips-ic),(unsigned long long)(q.state_clips-sc),(unsigned long long)(q.acc_clips-ac));
    }
    int error=ferror(out);if(fclose(out))error=1;if(error){fprintf(stderr,"Trace write failed\n");return 1;}return 0;
}
