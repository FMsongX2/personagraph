#!/usr/bin/env python3
"""Local native-transcript index and immutable public-evidence snapshots."""
import argparse
import importlib.util
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import subprocess
import shutil
import uuid
from datetime import datetime, timezone

PROJECT = Path(__file__).resolve().parents[1]
DEFAULT = PROJECT / 'runtime' / 'alignment-memory'
_evidence_spec=importlib.util.spec_from_file_location('alignment_evidence_store',Path(__file__).with_name('evidence_store.py'))
evidence=importlib.util.module_from_spec(_evidence_spec);_evidence_spec.loader.exec_module(evidence)
portable=evidence.portable
GENERATION=re.compile('tantivy-[0-9a-f]{32}')

def sha(value):
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()

def public(d, adapter):
    """Only public textual messages; unknown human provenance is quarantined."""
    if adapter == 'codex':
        p = d.get('payload') or {}
        if d.get('type') != 'response_item' or p.get('type') != 'message':
            return None
        role = p.get('role')
        parts = p.get('content', [])
        meta = p.get('internal_chat_message_metadata_passthrough') or {}
        if role == 'user':
            kinds = meta.get('content_item_kinds', [])
            known = {'user.text', 'agents_md.instructions', 'environments.environment_context', 'additional_content.codex_apps_open_page'}
            if len(kinds) != len(parts) or any(k not in known for k in kinds):
                raise ValueError('unknown user provenance')
            parts = [p for p, k in zip(parts, kinds) if k == 'user.text']
            if not parts:
                return None
        elif role != 'assistant' or p.get('phase') not in ('commentary', 'final_answer'):
            return None
        if any(p.get('type') not in ('input_text', 'output_text') for p in parts):
            raise ValueError('unsupported public media')
        text = '\n'.join(p.get('text', '') for p in parts)
        mid, parent, phase = p.get('id'), meta.get('parent_id'), p.get('phase')
    else:
        role = d.get('type')
        if role not in ('user', 'assistant') or d.get('isMeta') or d.get('isSidechain'):
            return None
        p = d.get('message') or {}
        if p.get('role') != role:
            raise ValueError('role mismatch')
        parts = p.get('content', [])
        if isinstance(parts, str):
            parts = [{'type': 'text', 'text': parts}]
        if role == 'user':
            if all(b.get('type') == 'tool_result' for b in parts):
                return None
            if (d.get('origin') or {}).get('kind') != 'human':
                raise ValueError('unknown user provenance')
            if any(b.get('type') != 'text' for b in parts):
                raise ValueError('unsupported human content')
        else:
            if any(b.get('type') not in ('text', 'thinking', 'redacted_thinking', 'tool_use') for b in parts):
                raise ValueError('unknown assistant content')
            parts = [b for b in parts if b.get('type') == 'text']
        texts = []
        for b in parts:
            text = b.get('text', '')
            # Whole IDE/system context blocks are not human statements. Mixed blocks fail closed.
            if role == 'user' and re.match(r'\s*<(ide_opened_file|ide_selection|system-reminder)>', text):
                if re.fullmatch(r'\s*<(ide_opened_file|ide_selection|system-reminder)>.*</\1>\s*', text, re.S):
                    continue
                raise ValueError('mixed injected context block')
            texts.append(text)
        if not texts:
            return None
        text = '\n'.join(texts)
        mid, parent, phase = d.get('uuid'), d.get('parentUuid'), 'public-text'
    if not mid:
        raise ValueError('missing stable message id')
    return {'id': mid, 'role': role, 'origin': 'human_message' if role == 'user' else 'assistant_public',
            'phase': phase, 'occurred_at': d.get('timestamp'), 'parent_id': parent,
            'text': text, 'hash': sha(text), 'coverage': 'public text only; attachments not copied; no complete branch reconstruction'}

class Store:
    def __init__(self, root, evidence_dir=None, catalog_path=None, read_only=False):
        self.root = Path(root).resolve()
        if not self.root.is_relative_to(PROJECT / 'runtime'):
            raise ValueError('store must be within project runtime')
        self.read_only=read_only
        if not read_only:self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.bootstrap_db = self.root / 'index.sqlite'
        self.catalog_override = Path(catalog_path).resolve() if catalog_path else None
        if self.catalog_override and not self.catalog_override.is_relative_to(self.root):
            raise ValueError('catalog must remain inside store')
        self.snapdir = Path(evidence_dir or (evidence.PERMANENT if self.root==DEFAULT.resolve() else self.root/'evidence')).resolve()
        if not (self.snapdir.is_relative_to(PROJECT/'data') or self.snapdir.is_relative_to(PROJECT/'runtime')):
            raise ValueError('evidence directory must be within project data or runtime')
        if not read_only:self.snapdir.mkdir(parents=True,exist_ok=True,mode=0o700)

    def current_generation(self):
        """The published search generation: a `search-current` symlink (POSIX) or, where
        symlinks need privileges (Windows), an atomically replaced `search-current.ref` file."""
        link=self.root/'search-current'
        if link.is_symlink():
            if not link.exists(): raise ValueError('active search generation is unavailable')
            return link.resolve()
        pointer=self.root/'search-current.ref'
        if not pointer.exists(): return None
        name=pointer.read_text(encoding='utf-8').strip()
        if not GENERATION.fullmatch(name) or not (self.root/name).is_dir():
            raise ValueError('active search generation is unavailable')
        return (self.root/name).resolve()

    @property
    def dbpath(self):
        if self.catalog_override: return self.catalog_override
        current=self.current_generation()
        if current and (current/'catalog.sqlite').exists(): return current/'catalog.sqlite'
        return self.bootstrap_db

    def pending_updates(self):
        path=self.root/'update-status.json'
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}

    def record_update(self,path,source,state,error=None):
        jobs=self.pending_updates()
        jobs[str(path)]={'source':source,'status':state,'error':error,
                         'at':datetime.now(timezone.utc).isoformat()}
        evidence.atomic(self.root/'update-status.json',(json.dumps(jobs)+'\n').encode())

    @staticmethod
    def prefix_hasher(path,size):
        h=hashlib.sha256();remaining=size
        with Path(path).open('rb') as f:
            while remaining:
                chunk=f.read(min(1048576,remaining))
                if not chunk: raise ValueError('native source truncated during prefix verification')
                h.update(chunk);remaining-=len(chunk)
        return h

    @contextlib.contextmanager
    def database(self):
        if self.read_only:
            path=self.dbpath.resolve()
            if path.exists():
                db=sqlite3.connect(path.as_uri()+'?mode=ro&immutable=1',uri=True)
            else:
                # Exact pinned evidence remains readable even after all derived catalog files are gone.
                db=sqlite3.connect(':memory:')
                db.executescript('CREATE TABLE sources(key TEXT PRIMARY KEY,path TEXT,adapter TEXT,size INTEGER,prefix_hash TEXT,updated TEXT,quarantine TEXT,partial INTEGER); CREATE TABLE messages(rowid INTEGER PRIMARY KEY,source TEXT,id TEXT,hash TEXT,role TEXT,time TEXT,offset INTEGER,size INTEGER);')
            db.row_factory=sqlite3.Row
            try:yield db
            finally:db.close()
            return
        with portable.locked(self.root / '.lock'):
            db = sqlite3.connect(self.dbpath)
            db.row_factory = sqlite3.Row
            try:
                db.executescript('''
                CREATE TABLE IF NOT EXISTS sources(key TEXT PRIMARY KEY,path TEXT,adapter TEXT,size INTEGER,prefix_hash TEXT,updated TEXT,quarantine TEXT,partial INTEGER);
                CREATE TABLE IF NOT EXISTS messages(rowid INTEGER PRIMARY KEY,source TEXT,id TEXT,hash TEXT,role TEXT,time TEXT,offset INTEGER,size INTEGER,UNIQUE(source,id));
                CREATE INDEX IF NOT EXISTS messages_source_offset ON messages(source,offset);
                ''')
                with db:
                    yield db
            finally:
                db.close()

    def index(self, path, adapter, key=None):
        if self.read_only:raise ValueError('cannot index in read-only mode')
        path=Path(path).resolve(strict=True)
        if not path.is_file() or path.is_relative_to(self.root):
            raise ValueError('source must be a native transcript outside the store')
        with portable.locked(self.root/'.search-lock'):
            target=None
            try:
                with self.database() as db:
                    old=db.execute('SELECT * FROM sources WHERE key=?',(key,)).fetchone() if key else db.execute('SELECT * FROM sources WHERE path=?',(str(path),)).fetchone()
                    old=dict(old) if old else None
                if old and old['adapter']!=adapter: raise ValueError('source key adapter conflict')
                if old: key=old['key']
                size=path.stat().st_size
                h=hashlib.sha256();start=0;mode='full';validation_bytes=0
                if old and size>=old['size']:
                    verified=self.prefix_hasher(path,old['size']);validation_bytes+=old['size']
                    if verified.hexdigest()==old['prefix_hash']:
                        start=old['size'];h=verified;mode='append'
                    elif old['path']!=str(path):
                        raise ValueError('relocated source does not match indexed prefix')
                elif old and old['path']!=str(path):
                    raise ValueError('relocated source is shorter than indexed prefix')
                previous=self.current_generation()
                old_manifest=json.loads((previous/'evidence-map.json').read_text(encoding='utf-8')) if previous and (previous/'evidence-map.json').exists() else {}
                compatible=old_manifest.get('format')=='session-candidates-v3' and (previous/'catalog.sqlite').exists()
                job=self.pending_updates().get(str(path),{})
                if old and mode=='append' and size==start and old['path']==str(path) and not old['partial'] and compatible and job.get('status') not in ('failed','pending'):
                    with self.database() as db:
                        count=db.execute('SELECT COUNT(*) FROM messages WHERE source=?',(key,)).fetchone()[0]
                    return {'source':key,'indexed':count,'mode':'unchanged','parsed_records':0,'parsed_bytes':0,
                            'prefix_validation_bytes':validation_bytes,'search_updated_sessions':0,
                            'search_sessions':len(old_manifest['mapping']),'quarantined':len(json.loads(old['quarantine'])),
                            'partial_tail':False,'search_backend':'aichat-search','native_copied':False}
                rows=[];quarantine=json.loads(old['quarantine']) if old and mode=='append' else []
                consumed=start;complete=start;partial=False;parsed_records=0
                with path.open('rb') as f:
                    f.seek(start)
                    while consumed<size:
                        off=consumed;line=f.readline(size-consumed)
                        if not line: raise ValueError('native source truncated during capture')
                        consumed+=len(line)
                        if not line.endswith(b'\n'):
                            partial=True;break
                        h.update(line);complete=consumed
                        try: d=json.loads(line)
                        except (ValueError,UnicodeError) as error:
                            raise ValueError(f'invalid complete JSON record at {off}') from error
                        parsed_records+=1
                        if key is None:
                            sid=(d.get('payload') or {}).get('id') if d.get('type')=='session_meta' else d.get('sessionId')
                            if sid:key=adapter+':'+sid
                        try:r=public(d,adapter)
                        except (ValueError,TypeError,AttributeError) as error:
                            quarantine.append({'offset':off,'reason':str(error)});continue
                        if r:rows.append((r,off,len(line)))
                key=key or adapter+':'+sha(str(path))[:24]
                if self.prefix_hasher(path,complete).hexdigest()!=h.hexdigest():
                    raise ValueError('source changed during capture')
                validation_bytes+=complete
                seen={}
                for r,off,length in rows:
                    if r['id'] in seen and seen[r['id']][0]!=r: raise ValueError('conflicting duplicate message id')
                    seen[r['id']]=(r,off,length)
                target=self.root/('tantivy-'+uuid.uuid4().hex)
                if compatible:
                    shutil.copytree(previous,target,ignore=shutil.ignore_patterns('catalog.sqlite','catalog.sqlite-*'))
                else: target.mkdir(mode=0o700)
                # Snapshot the old catalog. Only the staging catalog will receive changes.
                with self.database() as db:
                    dst=sqlite3.connect(target/'catalog.sqlite')
                    try:db.backup(dst)
                    finally:dst.close()
                staged=Store(self.root,evidence_dir=self.snapdir,catalog_path=target/'catalog.sqlite')
                inserted=0
                with staged.database() as db:
                    existing=db.execute('SELECT * FROM sources WHERE key=?',(key,)).fetchone()
                    if existing and existing['adapter']!=adapter:raise ValueError('source key adapter conflict')
                    if existing and existing['path']!=str(path) and not old:
                        if self.prefix_hasher(path,existing['size']).hexdigest()!=existing['prefix_hash']:
                            raise ValueError('relocated source does not match indexed prefix')
                    if mode!='append':db.execute('DELETE FROM messages WHERE source=?',(key,))
                    for r,off,length in seen.values():
                        duplicate=db.execute('SELECT * FROM messages WHERE source=? AND id=?',(key,r['id'])).fetchone()
                        if duplicate:
                            with path.open('rb') as f:
                                f.seek(duplicate['offset']);prior=public(json.loads(f.read(duplicate['size'])),adapter)
                            if prior!=r:raise ValueError('conflicting duplicate message id in append')
                            continue
                        db.execute('INSERT INTO messages(source,id,hash,role,time,offset,size) VALUES(?,?,?,?,?,?,?)',
                                   (key,r['id'],r['hash'],r['role'],r['occurred_at'],off,length));inserted+=1
                    db.execute('INSERT OR REPLACE INTO sources VALUES(?,?,?,?,?,?,?,?)',
                               (key,str(path),adapter,complete,h.hexdigest(),datetime.now(timezone.utc).isoformat(),json.dumps(quarantine),int(partial)))
                    count=db.execute('SELECT COUNT(*) FROM messages WHERE source=?',(key,)).fetchone()[0]
                changed=[key] if inserted or mode!='append' or (old and old['path']!=str(path)) else []
                self.record_update(path,key,'pending')
                stats=self.build_search(target,changed if compatible else None)
                # Final prefix validation before publication, not merely after parsing.
                if self.prefix_hasher(path,complete).hexdigest()!=h.hexdigest():
                    raise ValueError('source changed during search build')
                validation_bytes+=complete
                self.publish_generation(target)
                warnings=[]
                try:self.record_update(path,key,'ready')
                except OSError as error:warnings.append('published; status write failed: '+str(error))
                # Maintenance cannot turn an already published coherent pair into a failed import.
                try:
                    with self.database():
                        for folder in self.root.glob('tantivy-*'):
                            if folder!=target and folder!=previous and folder.is_dir():shutil.rmtree(folder)
                except OSError as error:warnings.append('old generation cleanup failed: '+str(error))
                return {'source':key,'indexed':count,'mode':mode,'parsed_records':parsed_records,
                        'parsed_bytes':complete-start,'prefix_validation_bytes':validation_bytes,
                        'search_updated_sessions':stats['updated_sessions'],'search_sessions':stats['indexed'],
                        'unsearchable_other_records':stats['skipped'],'quarantined':len(quarantine),'partial_tail':partial,
                        'search_backend':'aichat-search','native_copied':False,'maintenance_warnings':warnings}
            except Exception as error:
                try:published=self.current_generation()
                except ValueError:published=None
                if target and target.exists() and published!=target.resolve():
                    shutil.rmtree(target)
                self.record_update(path,key,'failed',str(error))
                raise

    def snapshot_path(self, source, mid, expected):
        return self.snapdir / (sha(json.dumps([source, mid, expected])) + '.json')

    def read_native(self, db, loc):
        src = db.execute('SELECT * FROM sources WHERE key=?', (loc['source'],)).fetchone()
        with Path(src['path']).open('rb') as f:
            f.seek(loc['offset'])
            r = public(json.loads(f.read(loc['size'])), src['adapter'])
        if not r or r['id'] != loc['id'] or r['hash'] != loc['hash'] or r['role'] != loc['role']:
            raise ValueError('stale source pointer or changed content; reindex required')
        return dict(r, source=loc['source'], retrieved_from='native')

    def get(self, source, mid, expected=None, pin=False):
        if self.read_only and pin:raise ValueError('cannot pin in read-only mode')
        with self.database() as db:
            loc = db.execute('SELECT * FROM messages WHERE source=? AND id=?', (source, mid)).fetchone()
            if expected is None:
                if loc is None:
                    raise ValueError('message not indexed; provide a pinned version hash if available')
                expected = loc['hash']
            if not re.fullmatch('[0-9a-f]{64}', expected):
                raise ValueError('invalid evidence hash')
            try:
                if loc is None or loc['hash'] != expected:
                    raise ValueError('version not in current index')
                r = self.read_native(db, loc)
            except (OSError, ValueError, TypeError, KeyError):
                sp = self.snapshot_path(source, mid, expected)
                if not sp.exists():
                    raise ValueError('source unavailable/changed and requested version has no snapshot')
                r = evidence.read_snapshot(sp)
                r['retrieved_from'] = 'historical-snapshot'
            if r['source'] != source or r['id'] != mid or sha(r['text']) != expected or r['hash'] != expected:
                raise ValueError('evidence integrity mismatch')
            if loc is not None and loc['hash'] == expected and r['role'] != loc['role']:
                raise ValueError('evidence role mismatch')
            if pin:
                sp = self.snapshot_path(source, mid, expected)
                with evidence.lock(self.snapdir):
                    if sp.exists():
                        saved = evidence.read_snapshot(sp)
                        if saved['role'] != r['role'] or saved['origin'] != r['origin']:
                            raise ValueError('existing snapshot integrity mismatch')
                    else:
                        evidence.atomic(sp,(json.dumps(r,ensure_ascii=False)+'\n').encode())
                        evidence.read_snapshot(sp)
                r['snapshot'] = str(sp)
        if pin and self.root==DEFAULT.resolve() and self.snapdir==evidence.PERMANENT:
            try:
                r['backup']=evidence.default_backup().backup()
            except (ValueError,OSError,subprocess.TimeoutExpired) as error:
                r['backup']={'status':'failed','error':str(error),'snapshot_saved':True}
        return r

    def migrate_evidence(self):
        if self.root!=DEFAULT.resolve() or self.snapdir!=evidence.PERMANENT:
            raise ValueError('production evidence migration uses the default store only')
        legacy=self.root/'evidence'
        with self.database():
            with evidence.lock(self.snapdir):
                result=evidence.merge_files(legacy,self.snapdir) if legacy.exists() else {'files':0,'added':0,'file_hashes':{}}
            backup=evidence.default_backup().backup()
            checked=evidence.default_backup().check(read_data=True)
            # Retire verified duplicate cache assets only after durable copy and checked backup exist.
            if legacy.exists():
                for name,h in result['file_hashes'].items():
                    if sha((legacy/name).read_bytes())!=h: raise ValueError('legacy evidence changed during migration')
                for name in result['file_hashes']: (legacy/name).unlink()
                if not any(legacy.iterdir()): legacy.rmdir()
        return dict(result,backup=backup,check=checked,target=str(self.snapdir))

    def build_search(self,target,changed_sources=None):
        python=portable.venv_python(PROJECT/'runtime/aichat-python')
        if not python.exists():raise ValueError('aichat index runtime missing; run installer')
        plan={'changed_sources':changed_sources}
        (target/'update-plan.json').write_text(json.dumps(plan),encoding='utf-8')
        result=subprocess.run([str(python),str(PROJECT/'scripts/aichat-index.py'),str(self.root),str(target)],
                              capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=120)
        if result.returncode:raise ValueError('aichat index build failed: '+result.stderr[-1500:])
        return json.loads(result.stdout)

    def publish_generation(self,target):
        current=self.root/'search-current'
        temp=self.root/('.search-'+uuid.uuid4().hex)
        if portable.WINDOWS:
            # One atomic pointer-file replacement; the previous pointer stays valid on failure.
            with self.database():evidence.atomic(self.root/'search-current.ref',(target.name+'\n').encode())
            return
        with self.database():
            prior=os.readlink(current) if current.is_symlink() else None
            try:
                temp.symlink_to(target.name);os.replace(temp,current)
                portable.fsync_directory(self.root)
            except Exception:
                if current.is_symlink() and current.resolve()==target:
                    if prior is None:current.unlink()
                    else:
                        rollback=self.root/('.rollback-'+uuid.uuid4().hex)
                        rollback.symlink_to(prior);os.replace(rollback,current)
                if temp.exists() or temp.is_symlink():temp.unlink()
                raise

    def search(self, query, source=None, limit=8):
        binary = portable.executable(PROJECT / 'runtime/aichat-bin/aichat-search')
        with portable.locked(self.root / '.search-lock', shared=True, mode='r' if self.read_only else 'a'):
            current = self.current_generation()
            if not current or not binary.exists():
                raise ValueError('search backend/index missing; install and index a transcript first')
            with self.database() as db:
                catalog = [dict(r) for r in db.execute('SELECT * FROM sources ORDER BY key')]
            if (current/'catalog-hash').read_text(encoding='utf-8') != sha(json.dumps(catalog,sort_keys=True)):
                raise ValueError('search index stale; rerun index before searching')
            manifest = json.loads((current/'evidence-map.json').read_text(encoding='utf-8'))
            if manifest.get('format') not in ('session-candidates-v2','session-candidates-v3'):
                raise ValueError('old message-candidate index; rerun index to migrate')
            evidence_map = manifest['mapping']
            # Parse through upstream Tantivy, but reject unknown field access and huge queries.
            if not query.strip() or len(query)>1000 or ':' in query:
                raise ValueError('use a nonempty content query without field selectors (max 1000 chars)')
            command = [str(binary),'--index-path',str(current),'--json','--global',
                       '--claude-home','','--codex-home','','--query',query]
            result = subprocess.run(command,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=30)
            if result.returncode:
                raise ValueError('aichat-search failed: '+result.stderr[-1500:])
            hits = []
            for line in result.stdout.splitlines():
                hit = json.loads(line)
                if isinstance(hit,list) and not hit: continue
                ref = evidence_map.get(hit['session_id'])
                if ref is None: raise ValueError('unknown upstream candidate id')
                if source and ref['source'] != source: continue
                # Discovery is a session candidate, not verified message-level evidence.
                failed=[dict(path=path,**job) for path,job in self.pending_updates().items() if job['status'] in ('failed','pending')]
                hits.append(dict(ref,backend='aichat-search',source_present=Path(ref['path']).is_file(),
                                 generation=current.name,pending_failures=failed,verification='candidate-only; use find/get'))
                if len(hits)>=limit: break
            self.last_search_meta={'generation':current.name,'pending_updates':
                [dict(path=path,**job) for path,job in self.pending_updates().items() if job['status'] in ('failed','pending')],
                'coverage':'last published index; unindexed appends may be absent'}
            return hits

    def find(self, source, query, limit=8, scan_limit=2000):
        """Narrow one session by literal public-text containment; not a second global index."""
        if not query.strip() or len(query)>1000:
            raise ValueError('use a nonempty literal (max 1000 chars)')
        needle = query.casefold()
        with self.database() as db:
            if not db.execute('SELECT 1 FROM sources WHERE key=?',(source,)).fetchone():
                raise ValueError('session not indexed')
            total = db.execute('SELECT COUNT(*) FROM messages WHERE source=?',(source,)).fetchone()[0]
            refs = db.execute('SELECT id,hash FROM messages WHERE source=? ORDER BY offset DESC LIMIT ?',(source,scan_limit)).fetchall()
        hits, unavailable, scanned = [], [], 0
        for ref in refs:
            scanned += 1
            try:
                r = self.get(source,ref['id'],ref['hash'])
                if r['retrieved_from'] != 'native':
                    raise ValueError('native evidence unavailable; use get --hash for historical snapshot')
            except (OSError, ValueError, TypeError, KeyError) as e:
                unavailable.append({'id':ref['id'],'reason':str(e)})
                continue
            if needle in r['text'].casefold():
                hits.append({'source':source,'id':r['id'],'hash':r['hash'],
                             'role':r['role'],'time':r['occurred_at'],'verified':'native'})
                if len(hits)>=limit: break
        return {'source':source,'matches':hits,'scanned':scanned,'indexed_messages':total,
                'limit_reached':len(hits)>=limit, 'scan_limit_reached':scanned<total and scanned>=scan_limit,
                'complete_scan':scanned==total,'unavailable_count':len(unavailable),
                'unavailable':unavailable[:8],'coverage':'bounded literal scan, newest public records first'}

    def context(self, source, mid, before=2, after=0, expected=None):
        anchor = self.get(source, mid, expected)
        if anchor['retrieved_from'] != 'native':
            raise ValueError('historical snapshot has no verified current neighboring context')
        with self.database() as db:
            loc = db.execute('SELECT offset,hash FROM messages WHERE source=? AND id=?', (source, mid)).fetchone()
            if loc is None or loc['hash'] != anchor['hash']:
                raise ValueError('anchor index changed')
            previous = db.execute('SELECT id,hash FROM messages WHERE source=? AND offset<? ORDER BY offset DESC LIMIT ?', (source, loc['offset'], before)).fetchall()
            following = db.execute('SELECT id,hash FROM messages WHERE source=? AND offset>? ORDER BY offset LIMIT ?', (source, loc['offset'], after)).fetchall()
            refs = list(reversed(previous)) + [{'id': mid, 'hash': anchor['hash']}] + list(following)
        return {'coverage': 'bounded public record order; not reconstructed causal branches',
                'messages': [self.get(source, r['id'], r['hash']) for r in refs]}

    def status(self):
        with self.database() as db:
            current=self.current_generation()
            self.last_status_generation=current.name if current else None
            return [dict(r) for r in db.execute('SELECT s.*, (SELECT COUNT(*) FROM messages m WHERE m.source=s.key) AS indexed FROM sources s')]

def main():
    os.umask(0o077)
    portable.utf8_stdio()
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--store', default=str(DEFAULT))
    sub = p.add_subparsers(dest='command', required=True)
    q = sub.add_parser('index'); q.add_argument('path'); q.add_argument('--adapter', choices=['codex', 'claude'], required=True); q.add_argument('--key')
    q = sub.add_parser('search'); q.add_argument('query'); q.add_argument('--source'); q.add_argument('--limit', type=int, default=8)
    q = sub.add_parser('find'); q.add_argument('query'); q.add_argument('--source', required=True); q.add_argument('--limit', type=int, default=8); q.add_argument('--scan-limit',type=int,default=2000)
    for command in ('get', 'pin'):
        q = sub.add_parser(command); q.add_argument('--source', required=True); q.add_argument('--id', required=True); q.add_argument('--hash')
    q = sub.add_parser('context'); q.add_argument('--source', required=True); q.add_argument('--id', required=True); q.add_argument('--hash'); q.add_argument('--before', type=int, default=2); q.add_argument('--after', type=int, default=0)
    sub.add_parser('status')
    sub.add_parser('migrate-evidence')
    sub.add_parser('backup')
    q=sub.add_parser('backup-check');q.add_argument('--read-data',action='store_true')
    q=sub.add_parser('restore');q.add_argument('--snapshot',required=True);q.add_argument('--target')
    a = p.parse_args()
    try:
        store = Store(a.store,read_only=a.command in ('get','find','context','search','status'))
        if a.command == 'index': result = store.index(a.path, a.adapter, a.key)
        elif a.command == 'search':
            sessions=store.search(a.query, a.source, max(1, min(a.limit,100)))
            result=dict(sessions=sessions,**store.last_search_meta)
        elif a.command == 'find': result = store.find(a.source, a.query, max(1, min(a.limit,100)), max(1,min(a.scan_limit,100000)))
        elif a.command == 'migrate-evidence': result = store.migrate_evidence()
        elif a.command in ('backup','backup-check','restore'):
            if store.root!=DEFAULT.resolve(): raise ValueError('backup CLI operates on default permanent evidence only')
            backup=evidence.default_backup()
            if a.command=='backup': result=backup.backup()
            elif a.command=='backup-check': result=backup.check(a.read_data)
            else: result=backup.restore(a.snapshot,a.target)
        elif a.command == 'status':
            sources=store.status()
            result={'sources':sources,'generation':store.last_status_generation,'updates':store.pending_updates()}
        elif a.command == 'context': result = store.context(a.source, a.id, max(0, min(a.before, 10)), max(0, min(a.after, 10)), a.hash)
        else: result = store.get(a.source, a.id, a.hash, a.command == 'pin')
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if a.command=='pin' and result.get('backup',{}).get('status')=='failed': return 1
    except (ValueError, OSError, sqlite3.Error, TypeError, KeyError, subprocess.TimeoutExpired) as e:
        print(json.dumps({'error': str(e)}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0

if __name__ == '__main__':
    sys.exit(main())
