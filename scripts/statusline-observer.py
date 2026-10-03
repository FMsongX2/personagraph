#!/usr/bin/env python3
"""Observe Claude context percentage while forwarding the original command's output."""
import importlib.util,json,subprocess,sys
from pathlib import Path
PROJECT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('personagraph_portable',Path(__file__).with_name('portable.py'))
portable=importlib.util.module_from_spec(spec);spec.loader.exec_module(portable)

def render(raw,previous=None,observe=True):
    if observe:
        try:subprocess.run([sys.executable,str(PROJECT/'scripts/alignment-capture.py'),'hook','--provider','claude','--statusline'],input=raw,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=2)
        except (OSError,subprocess.TimeoutExpired):pass
    if not previous:return b''
    # The shell Claude Code itself uses for status lines (sh on POSIX, Git Bash on Windows).
    result=subprocess.run(portable.shell(previous['command']),input=raw,stdout=subprocess.PIPE,timeout=10)
    return result.stdout

def main():
    raw=sys.stdin.buffer.read(2097153)
    if len(raw)>2097152:return
    file=PROJECT/'data/private/previous-statusline.json'
    previous=json.loads(file.read_text(encoding='utf-8')) if file.exists() else None
    sys.stdout.buffer.write(render(raw,previous))
if __name__=='__main__':main()
