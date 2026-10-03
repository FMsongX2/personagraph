"""Immutable evidence validation, migration, and restic-backed local recovery."""
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone

PROJECT = Path(__file__).resolve().parents[1]
_portable_spec=importlib.util.spec_from_file_location('personagraph_portable',Path(__file__).with_name('portable.py'))
portable=importlib.util.module_from_spec(_portable_spec);_portable_spec.loader.exec_module(portable)
PERMANENT = PROJECT / 'data/alignment-evidence'
TAG = 'persona-alignment-evidence'


def digest(value):
    return hashlib.sha256(value.encode() if isinstance(value,str) else value).hexdigest()


def filename(record):
    return digest(json.dumps([record['source'],record['id'],record['hash']]))+'.json'


def read_snapshot(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('snapshot must be a regular file')
    r = json.loads(path.read_text(encoding='utf-8'))
    if not all(isinstance(r.get(k),str) for k in ('source','id','hash','text','role','origin')):
        raise ValueError('invalid evidence record')
    if r['role'] not in ('user','assistant') or r['origin'] != ('human_message' if r['role']=='user' else 'assistant_public'):
        raise ValueError('invalid evidence provenance')
    if digest(r['text']) != r['hash'] or path.name != filename(r):
        raise ValueError('snapshot content/identity mismatch')
    return r


@contextlib.contextmanager
def lock(folder):
    folder = Path(folder)
    folder.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    with portable.locked(folder.parent / ('.'+folder.name+'.lock')):
        yield


def atomic(path, raw):
    path = Path(path)
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd,temp = tempfile.mkstemp(dir=path.parent,prefix='.pending-')
    try:
        with os.fdopen(fd,'wb') as f:
            f.write(raw);f.flush();os.fsync(f.fileno())
        portable.replace(temp,path)
        portable.fsync_directory(path.parent)
    finally:
        if os.path.exists(temp): os.unlink(temp)


def inventory(folder):
    folder = Path(folder)
    entries = {}
    for p in sorted(folder.iterdir()):
        if p.name=='.manifest.json' or p.name.startswith('.pending-'): continue
        if not re.fullmatch('[0-9a-f]{64}\\.json',p.name):
            raise ValueError('unexpected evidence asset: '+p.name)
        read_snapshot(p)
        entries[p.name] = digest(p.read_bytes())
    return entries


def merge_files(source, target, require_manifest=False):
    source,target = Path(source),Path(target)
    entries = inventory(source)
    if require_manifest:
        manifest_path=source/'.manifest.json'
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise ValueError('restored manifest must be a regular file')
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        if manifest.get('version')!=1 or manifest.get('files')!=entries:
            raise ValueError('restored evidence manifest mismatch')
    target.mkdir(parents=True,exist_ok=True,mode=0o700)
    existing = inventory(target)
    # Validate every conflict before writing anything. Never overwrite immutable assets.
    for name,h in entries.items():
        if name in existing and existing[name]!=h:
            raise ValueError('existing evidence conflict: '+name)
    added=0
    for name in entries:
        if name not in existing:
            atomic(target/name,(source/name).read_bytes());read_snapshot(target/name);added+=1
    return {'files':len(entries),'added':added,'file_hashes':entries}


class Backup:
    def __init__(self,evidence,repository,password_file,state,cache):
        self.evidence=Path(evidence).resolve()
        self.repository=Path(repository).resolve()
        self.password=Path(password_file).resolve()
        self.state=Path(state).resolve()
        self.cache=Path(cache).resolve()
        if self.repository.is_relative_to(self.evidence) or self.password.is_relative_to(self.evidence):
            raise ValueError('backup repository and password must be outside evidence')

    def run(self,args):
        binary=shutil.which('restic')
        if not binary: raise ValueError('restic not installed; install restic before backing up')
        p=subprocess.run([binary,'--repo',str(self.repository),'--password-file',str(self.password),
                          '--cache-dir',str(self.cache),*args],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=60)
        if p.returncode:
            raise ValueError('restic failed: '+p.stderr[-1200:])
        return p.stdout

    def setup(self):
        self.password.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        if not self.password.exists():
            if (self.repository/'config').exists():
                raise ValueError('backup password is missing; cannot replace an existing repository key')
            fd=os.open(self.password,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
            portable.make_private(self.password)  # Windows ignores the mode; apply an owner-only ACL before writing
            with os.fdopen(fd,'w') as f:
                f.write(secrets.token_urlsafe(48)+'\n');f.flush();os.fsync(f.fileno())
        if not portable.is_private(self.password):
            raise ValueError('backup password file must be private (0600 / owner-only ACL)')
        self.repository.mkdir(parents=True,exist_ok=True,mode=0o700)
        if not (self.repository/'config').exists(): self.run(['init'])

    def backup(self):
        with lock(self.evidence):
            self.evidence.mkdir(parents=True,exist_ok=True,mode=0o700)
            entries=inventory(self.evidence)
            self.setup()
            dataset=digest(json.dumps(entries,sort_keys=True))
            previous=json.loads(self.state.read_text(encoding='utf-8')) if self.state.exists() else {}
            if previous.get('dataset')==dataset and previous.get('snapshot'):
                saved=json.loads(self.run(['snapshots','--json','--tag',TAG,previous['snapshot']]))
                if len(saved)==1 and saved[0]['id']==previous['snapshot']:
                    return {'status':'backed-up','snapshot':previous['snapshot'],'files':len(entries),'reused':True}
            manifest={'version':1,'files':entries,'created_at':datetime.now(timezone.utc).isoformat()}
            atomic(self.evidence/'.manifest.json',(json.dumps(manifest,sort_keys=True)+'\n').encode())
            output=self.run(['backup',str(self.evidence),'--json','--tag',TAG,'--exclude','**/.pending-*'])
            summaries=[json.loads(line) for line in output.splitlines() if line.startswith('{')]
            summary=next(r for r in reversed(summaries) if r.get('message_type')=='summary')
            snapshot=summary['snapshot_id']
            if not re.fullmatch('[0-9a-f]{64}',snapshot): raise ValueError('invalid backup snapshot id')
            atomic(self.state,(json.dumps({'dataset':dataset,'snapshot':snapshot,'files':len(entries)})+'\n').encode())
            return {'status':'backed-up','snapshot':snapshot,'files':len(entries),'reused':False}

    def check(self,read_data=False):
        args=['check']+(['--read-data'] if read_data else [])
        self.run(args)
        return {'status':'verified','read_data':read_data}

    def restore(self,snapshot,target=None):
        if not re.fullmatch('[0-9a-f]{8,64}',snapshot):
            raise ValueError('restore requires an explicit snapshot ID')
        target=Path(target or self.evidence).resolve()
        if not (target.is_relative_to(PROJECT/'data') or target.is_relative_to(PROJECT/'runtime')):
            raise ValueError('restore target must be within project data or runtime')
        snapshots=json.loads(self.run(['snapshots','--json','--tag',TAG,snapshot]))
        if len(snapshots)!=1 or len(snapshots[0]['paths'])!=1:
            raise ValueError('snapshot must contain exactly one evidence root')
        original=Path(snapshots[0]['paths'][0])
        if not original.is_absolute() or '..' in original.parts:
            raise ValueError('invalid backed-up evidence path')
        staging=Path(tempfile.mkdtemp(prefix='evidence-restore-',dir=PROJECT/'runtime'))
        try:
            # Restore to isolated staging. Never let restic overwrite live evidence directly.
            if portable.WINDOWS:
                # Restoring the full path also restores C:/Users' ACL and then fails on its metadata;
                # restore only the evidence subtree (restic >= 0.17), directly into staging.
                self.run(['restore',snapshots[0]['id']+':'+portable.snapshot_subtree(original),'--target',str(staging),'--verify'])
                recovered=staging
            else:
                self.run(['restore',snapshots[0]['id'],'--target',str(staging),'--verify'])
                recovered=staging/original.relative_to(original.anchor)
            with lock(target): result=merge_files(recovered,target,require_manifest=True)
            return dict(result,status='restored',snapshot=snapshots[0]['id'],target=str(target))
        finally: shutil.rmtree(staging)


def default_backup():
    return Backup(PERMANENT,PROJECT/'data/backups/alignment-evidence-restic',
                  PROJECT/'data/private/restic-password',PROJECT/'data/private/evidence-backup-state.json',
                  PROJECT/'runtime/restic-cache')
