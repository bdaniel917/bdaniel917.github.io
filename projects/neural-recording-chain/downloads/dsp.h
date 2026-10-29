#ifndef DSP_H
#define DSP_H
/* Portable C11 reference DSP. Normalized amplitude: ADC full scale is [-1,1). */
#include <math.h>
#include <stdint.h>
#include <stddef.h>
#define DSP_PI 3.14159265358979323846

typedef struct { double b[3], a[3]; } Coeff;
typedef struct { Coeff c; double x1,x2,y1,y2,z1,z2; int realization; } FloatFilter;
typedef struct {
    int64_t b[3], a[3], x1,x2,y1,y2,z1,z2;
    unsigned bits, frac, coeff_frac, acc_bits;
    int truncate, wrap, realization;
    uint64_t input_clips, state_clips, acc_clips;
} FixedFilter;

static inline Coeff notch_coeff(double fs,double hz,double q) {
    double w=2*DSP_PI*hz/fs, alpha=sin(w)/(2*q), a0=1+alpha;
    Coeff c={{1/a0,-2*cos(w)/a0,1/a0},{1,-2*cos(w)/a0,(1-alpha)/a0}};
    return c;
}
static inline double float_step(FloatFilter *f,double x) {
    double y;
    if (f->realization) {
        y=f->c.b[0]*x+f->z1;
        f->z1=f->c.b[1]*x-f->c.a[1]*y+f->z2;
        f->z2=f->c.b[2]*x-f->c.a[2]*y;
    } else {
        y=f->c.b[0]*x+f->c.b[1]*f->x1+f->c.b[2]*f->x2-f->c.a[1]*f->y1-f->c.a[2]*f->y2;
        f->x2=f->x1;f->x1=x;f->y2=f->y1;f->y1=y;
    }
    return y;
}
static inline void float_block(FloatFilter *f,const double *in,double *out,size_t n) {
    for(size_t i=0;i<n;i++) out[i]=float_step(f,in[i]);
}
static inline int64_t limit_int(int64_t v,unsigned bits,int wrap,uint64_t *count) {
    int64_t hi=(INT64_C(1)<<(bits-1))-1, lo=-hi-1;
    if(v>=lo && v<=hi) return v;
    (*count)++;
    if(!wrap) return v<lo?lo:hi;
    /* Explicit modulo, without implementation-defined unsigned-to-signed casts. */
    uint64_t mask=(UINT64_C(1)<<bits)-1, u=(uint64_t)v&mask;
    return u>(uint64_t)hi?-(int64_t)((mask+1)-u):(int64_t)u;
}
static inline int64_t shift_round(int64_t x,unsigned shift,int truncate) {
    if(!shift) return x;
    if(truncate) return x/(INT64_C(1)<<shift); /* truncation toward zero */
    int64_t half=INT64_C(1)<<(shift-1);
    return x>=0?(x+half)/(INT64_C(1)<<shift):-((-x+half)/(INT64_C(1)<<shift));
}
static inline FixedFilter fixed_init(Coeff c,unsigned bits,unsigned frac,unsigned cf,unsigned acc,int trunc,int wrap,int realization) {
    FixedFilter f={0}; f.bits=bits;f.frac=frac;f.coeff_frac=cf;f.acc_bits=acc;
    f.truncate=trunc;f.wrap=wrap;f.realization=realization;
    for(int i=0;i<3;i++){f.b[i]=llround(c.b[i]*ldexp(1,cf));f.a[i]=llround(c.a[i]*ldexp(1,cf));}
    return f;
}
static inline int64_t fixed_input(FixedFilter *f,double x) {
    /* All callers provide bounded ADC values, so this conversion fits int64. */
    return limit_int(llround(x*ldexp(1,f->frac)),f->bits,f->wrap,&f->input_clips);
}
static inline int64_t acc_limit(FixedFilter *f,int64_t x) {
    return limit_int(x,f->acc_bits,f->wrap,&f->acc_clips);
}
static inline int64_t fixed_step(FixedFilter *f,int64_t x) {
    int64_t y;
    if(f->realization) {
        /* States use the same Q format as input/output; round each state update. */
        int64_t acc=acc_limit(f,f->b[0]*x+f->z1*(INT64_C(1)<<f->coeff_frac));
        y=limit_int(shift_round(acc,f->coeff_frac,f->truncate),f->bits,f->wrap,&f->state_clips);
        acc=acc_limit(f,f->b[1]*x-f->a[1]*y+f->z2*(INT64_C(1)<<f->coeff_frac));
        f->z1=limit_int(shift_round(acc,f->coeff_frac,f->truncate),f->bits,f->wrap,&f->state_clips);
        acc=acc_limit(f,f->b[2]*x-f->a[2]*y);
        f->z2=limit_int(shift_round(acc,f->coeff_frac,f->truncate),f->bits,f->wrap,&f->state_clips);
    } else {
        /* Emulate accumulator saturation/wrap after every left-to-right MAC. */
        int64_t acc=acc_limit(f,f->b[0]*x);
        acc=acc_limit(f,acc+f->b[1]*f->x1);acc=acc_limit(f,acc+f->b[2]*f->x2);
        acc=acc_limit(f,acc-f->a[1]*f->y1);acc=acc_limit(f,acc-f->a[2]*f->y2);
        y=limit_int(shift_round(acc,f->coeff_frac,f->truncate),f->bits,f->wrap,&f->state_clips);
        f->x2=f->x1;f->x1=x;f->y2=f->y1;f->y1=y;
    }
    return y;
}
static inline void fixed_block(FixedFilter *f,const double *in,double *out,size_t n) {
    for(size_t i=0;i<n;i++) out[i]=ldexp((double)fixed_step(f,fixed_input(f,in[i])),-(int)f->frac);
}
static inline double adc_step(double x,unsigned bits,int *clipped,double *quant,double *distortion) {
    double scale=ldexp(1,bits-1), raw=round(x*scale), lo=-scale,hi=scale-1;
    double code=fmax(lo,fmin(hi,raw));
    *clipped=raw<lo||raw>hi; *quant=raw/scale-x; *distortion=(code-raw)/scale;
    return code/scale;
}
#endif
