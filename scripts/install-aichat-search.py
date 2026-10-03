#!/usr/bin/env python3
"""Install pinned MIT aichat-search into this device's PersonaGraph runtime."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

PROJECT = Path(__file__).resolve().parents[1]
COMMIT = '0ca732006a387dfe38a601a4959197ce0df778d8'
REPO = 'https://github.com/pchalasani/claude-code-tools.git'

def run(args, **kw):
    return subprocess.run(args, check=True, **kw)

def ensure_checkout(source):
    # CI may restore target/ without .git. Never let Git walk up to our own repo.
    if not (source/'.git').exists():
        source.mkdir(parents=True,exist_ok=True)
        run(['git','init',str(source)])
        run(['git','-C',str(source),'remote','add','origin',REPO])
        run(['git','-C',str(source),'fetch','--depth','1','origin',COMMIT])
        run(['git','-C',str(source),'checkout','--detach',COMMIT])
    top = subprocess.check_output(['git','-C',str(source),'rev-parse','--show-toplevel'],text=True).strip()
    if Path(top).resolve() != source.resolve(): raise RuntimeError('upstream checkout resolves to a different repository')
    origin = subprocess.check_output(['git','-C',str(source),'remote','get-url','origin'],text=True).strip()
    if origin != REPO: raise RuntimeError('unexpected upstream repository; preserve checkout before replacing it')
    head = subprocess.check_output(['git','-C',str(source),'rev-parse','HEAD'],text=True).strip()
    if head != COMMIT:
        if subprocess.check_output(['git','-C',str(source),'status','--porcelain'],text=True).strip():
            raise RuntimeError('upstream checkout has changes; preserve them before switching commit')
        run(['git','-C',str(source),'fetch','--depth','1','origin',COMMIT])
        run(['git','-C',str(source),'checkout','--detach',COMMIT])

def main():
    (PROJECT/'runtime').mkdir(parents=True,exist_ok=True)
    source = PROJECT/'runtime/aichat-upstream'
    ensure_checkout(source)
    patch = PROJECT/'third_party/aichat-search/index-path.patch'
    applied = subprocess.run(['git','-C',str(source),'apply','--unidiff-zero','--reverse','--check',str(patch)],capture_output=True).returncode==0
    if not applied: run(['git','-C',str(source),'apply','--unidiff-zero','--check',str(patch)]); run(['git','-C',str(source),'apply','--unidiff-zero',str(patch)])
    # Refuse unexpected source changes rather than shipping an unrecorded binary.
    changed = subprocess.check_output(['git','-C',str(source),'diff','--name-only'],text=True).splitlines()
    if changed != ['rust-search-ui/src/main.rs']: raise RuntimeError('unexpected upstream checkout changes')
    actual = subprocess.check_output(['git','-C',str(source),'diff','--unified=0','--','rust-search-ui/src/main.rs'])
    if actual != patch.read_bytes(): raise RuntimeError('unexpected local search patch')
    run(['cargo','build','--release','--locked','--manifest-path',str(source/'rust-search-ui/Cargo.toml')])
    venv = PROJECT/'runtime/aichat-python'
    if not (venv/'bin/python').exists(): run(['uv','venv','--python','3.12',str(venv)])
    run(['uv','pip','install','--python',str(venv/'bin/python'),'tantivy==0.25.1'])
    target = PROJECT/'runtime/aichat-bin/aichat-search'; target.parent.mkdir(parents=True,exist_ok=True)
    shutil.copy2(source/'rust-search-ui/target/release/aichat-search',target)
    manifest = {'repository':REPO,'commit':COMMIT,'crate_version':'0.3.1','license':'MIT',
                'patch':'third_party/aichat-search/index-path.patch','python_dependency':'tantivy==0.25.1',
                'binary_sha256':hashlib.sha256(target.read_bytes()).hexdigest()}
    (target.parent/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    print(json.dumps(manifest,indent=2))

if __name__=='__main__': main()
