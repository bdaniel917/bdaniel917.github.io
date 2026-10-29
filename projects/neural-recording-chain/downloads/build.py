#!/usr/bin/env python3
"""Compile C11 tools, checking macOS SDK compatibility before building sources."""
from __future__ import annotations
import argparse
from functools import lru_cache
import os
from pathlib import Path
import platform
import shlex
import subprocess
import tempfile

ROOT=Path(__file__).resolve().parent


@lru_cache(maxsize=1)
def compiler_command():
    """Respect explicit CC/CFLAGS/SDKROOT; otherwise find a compatible host SDK."""
    command=shlex.split(os.environ.get('CC','cc'))
    flags=shlex.split(os.environ.get('CFLAGS','-O2 -std=c11 -Wall -Wextra -Wpedantic'))
    base=command+flags
    explicit=os.environ.get('SDKROOT')
    candidates=[explicit] if explicit else [None]
    if not explicit and platform.system()=='Darwin':
        sdk_dir=Path('/Library/Developer/CommandLineTools/SDKs')
        paths=set(p.resolve() for p in sdk_dir.glob('MacOSX*.sdk'))
        def version(path):
            try:return tuple(map(int,path.name.removeprefix('MacOSX').removesuffix('.sdk').split('.')))
            except ValueError:return ()
        candidates+=list(map(str,sorted(paths,key=version,reverse=True)))
    failures=[]
    with tempfile.TemporaryDirectory(prefix='chain_compiler_') as tmp:
        source=Path(tmp)/'check.c';source.write_text('#include <math.h>\nint main(void){volatile double x=.5;return sin(x)>2;}\n')
        for sdk in candidates:
            cmd=base+(['-isysroot',sdk] if sdk else [])
            result=subprocess.run(cmd+[str(source),'-lm','-o',str(Path(tmp)/'check')],text=True,capture_output=True)
            if result.returncode==0:return cmd
            failures.append(result.stderr)
    raise RuntimeError('No compatible compiler/SDK combination. Explicit CC/CFLAGS/SDKROOT were respected.\n'+failures[-1])


def compile_source(source,output):
    subprocess.run(compiler_command()+[str(source),'-lm','-o',str(output)],check=True)


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--source',default=str(ROOT/'chain_sim.c'));ap.add_argument('--output',default=str(ROOT/'chain_sim'))
    args=ap.parse_args();compile_source(args.source,args.output)
    print('Compiled with',shlex.join(compiler_command()))
if __name__=='__main__':main()
