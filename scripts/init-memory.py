#!/usr/bin/env python3
"""Create a local private Markdown notebook from synthetic templates; never overwrite it."""
import argparse,shutil
from pathlib import Path
PROJECT=Path(__file__).resolve().parents[1]

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--persona',choices=['colleague','yui'],default='colleague');a=p.parse_args()
    destination=PROJECT/'memory'
    if destination.exists():raise SystemExit('memory already exists; no files overwritten')
    shutil.copytree(PROJECT/'examples/memory',destination)
    shutil.copyfile(PROJECT/'examples/personas'/(a.persona+'.md'),destination/'PERSONA.md')
    print(str(destination))
if __name__=='__main__':main()
