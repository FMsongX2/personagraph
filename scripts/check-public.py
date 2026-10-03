#!/usr/bin/env python3
"""Check tracked release files for forbidden runtime artifacts and obvious credentials."""
from pathlib import Path
import re,subprocess
PROJECT=Path(__file__).resolve().parents[1]

def main():
    files=subprocess.check_output(['git','ls-files','-z'],cwd=PROJECT).decode().split('\0')
    forbidden={'data','runtime','memory','archive','references','notes'}
    issues=[]
    patterns=[r'gh[pousr]_[A-Za-z0-9]{20,}',r'sk-[A-Za-z0-9_-]{30,}',r'-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----',r'/Users/[A-Za-z0-9_-]+/',r'/home/[A-Za-z0-9_-]+/',r'(?i)[A-Z]:[\\/]+Users[\\/]+[A-Za-z0-9_.-]+[\\/]']
    for file in filter(None,files):
        if Path(file).parts[0] in forbidden:issues.append(file+': forbidden local artifact')
        p=PROJECT/file
        if p.is_symlink():issues.append(file+': unexpected release symlink');continue
        try:text=p.read_text(encoding='utf-8')
        except UnicodeDecodeError:issues.append(file+': binary release asset requires review');continue
        for pattern in patterns:
            if re.search(pattern,text):issues.append(file+': sensitive pattern (value not printed)')
    if issues:raise SystemExit('\n'.join(issues))
    print('Tracked public files: no forbidden local artifacts or checked sensitive patterns')
if __name__=='__main__':main()
