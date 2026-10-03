#!/usr/bin/env python3
"""Preview or append local capture hooks, preserving existing definitions and trust."""
import argparse,importlib.util,json,os,shlex,sys
from pathlib import Path
from datetime import datetime,timezone

PROJECT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('e',Path(__file__).with_name('evidence_store.py'));e=importlib.util.module_from_spec(spec);spec.loader.exec_module(e)
EVENTS=('SessionStart','UserPromptSubmit','Stop','SessionEnd','PreCompact','PostCompact','Interrupt')

def configured(original,provider,python):
    data=json.loads(original or b'{}')
    command=shlex.join([python,str(PROJECT/'scripts/alignment-capture.py'),'hook','--provider',provider])
    for event in EVENTS:
        if event=='Interrupt' and provider=='claude':continue
        groups=data.setdefault('hooks',{}).setdefault(event,[])
        if any(h.get('command')==command for g in groups for h in g.get('hooks',[])):continue
        groups.append({'hooks':[{'type':'command','command':command,'timeout':2,'statusMessage':'Queueing local alignment capture'}]})
    return data

def install(claude_home,codex_home,apply=False,statusline=False,python=None):
    python=python or sys.executable
    claude_home,codex_home=Path(claude_home).resolve(),Path(codex_home).resolve()
    paths={claude_home/'settings.json':'claude',codex_home/'hooks.json':'codex'}
    old={p:p.read_bytes() if p.exists() else b'' for p in paths}
    new={p:configured(old[p],provider,python) for p,provider in paths.items()}
    settings=claude_home/'settings.json'
    previous=None
    if statusline:
        wrapper=shlex.join([python,str(PROJECT/'scripts/statusline-observer.py')])
        if new[settings].get('statusLine',{}).get('command')!=wrapper:
            previous=new[settings].get('statusLine')
            if previous and previous.get('type')!='command':raise ValueError('only command status lines can be preserved')
            new[settings]['statusLine']={'type':'command','command':wrapper}
    encoded={p:(json.dumps(d,ensure_ascii=False,indent=2)+'\n').encode() for p,d in new.items()}
    report={'applied':False,'changed':[str(p) for p in paths if old[p]!=encoded[p]],'statusline_observer':statusline,
            'trust':'Review new Codex definitions using /hooks. No trust hashes are written.'}
    if not apply:return report
    backup=PROJECT/'data/config-backups'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
    backup.mkdir(parents=True,mode=0o700)
    for i,p in enumerate(paths):e.atomic(backup/(str(i)+'-'+p.name),old[p])
    for p in paths:
        if (p.read_bytes() if p.exists() else b'')!=old[p]:raise ValueError('configuration changed during install; retry')
    if statusline and previous is not None:
        e.atomic(PROJECT/'data/private/previous-statusline.json',(json.dumps(previous)+'\n').encode())
    # Save explicit provider roots when using non-default homes; no environment mutation required.
    e.atomic(PROJECT/'data/capture-roots.json',(json.dumps({'codex':[str(codex_home/'sessions'),str(codex_home/'archived_sessions')],'claude':[str(claude_home/'projects')]})+'\n').encode())
    for p in paths:e.atomic(p,encoded[p])
    report.update(applied=True,backup=str(backup))
    return report

def main():
    os.umask(0o077)
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--apply',action='store_true');p.add_argument('--statusline',action='store_true')
    p.add_argument('--claude-home',default=os.environ.get('CLAUDE_CONFIG_DIR') or str(Path.home()/'.claude'))
    p.add_argument('--codex-home',default=os.environ.get('CODEX_HOME') or str(Path.home()/'.codex'))
    a=p.parse_args();print(json.dumps(install(a.claude_home,a.codex_home,a.apply,a.statusline),indent=2))
if __name__=='__main__':main()
