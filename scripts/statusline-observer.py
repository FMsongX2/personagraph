#!/usr/bin/env python3
"""Observe Claude context percentage while forwarding the original command's output."""
import json,subprocess,sys
from pathlib import Path
PROJECT=Path(__file__).resolve().parents[1]

def render(raw,previous=None,observe=True):
    if observe:
        try:subprocess.run([sys.executable,str(PROJECT/'scripts/alignment-capture.py'),'hook','--provider','claude','--statusline'],input=raw,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=2)
        except (OSError,subprocess.TimeoutExpired):pass
    if not previous:return b''
    result=subprocess.run(['/bin/sh','-c',previous['command']],input=raw,stdout=subprocess.PIPE,timeout=10)
    return result.stdout

def main():
    raw=sys.stdin.buffer.read(2097153)
    if len(raw)>2097152:return
    file=PROJECT/'data/private/previous-statusline.json'
    previous=json.loads(file.read_text()) if file.exists() else None
    sys.stdout.buffer.write(render(raw,previous))
if __name__=='__main__':main()
