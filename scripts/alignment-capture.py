#!/usr/bin/env python3
"""Lifecycle metadata queue -> public-session index -> unpromoted review checkpoint."""
import argparse
import contextlib
import importlib.util
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
from datetime import datetime,timezone

PROJECT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('alignment_memory',Path(__file__).with_name('alignment-memory.py'))
am=importlib.util.module_from_spec(spec);spec.loader.exec_module(am)
e=am.evidence
portable=e.portable
DEFAULT=PROJECT/'data/capture-queue'
EVENTS={'SessionStart','UserPromptSubmit','Stop','SessionEnd','PreCompact','PostCompact','Interrupt'}

class Capture:
    def __init__(self,root=DEFAULT,store=None,roots=None,spawn=True):
        self.root=Path(root).resolve()
        if not (self.root.is_relative_to(PROJECT/'data') or self.root.is_relative_to(PROJECT/'runtime')):
            raise ValueError('capture queue must remain within PersonaGraph')
        self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.store=Path(store or am.DEFAULT).resolve()
        configured_roots=PROJECT/'data/capture-roots.json'
        if roots is None and configured_roots.exists():roots=json.loads(configured_roots.read_text(encoding='utf-8'))
        self.roots=roots or {'codex':[Path(os.environ.get('CODEX_HOME') or str(Path.home()/'.codex'))/'sessions',Path(os.environ.get('CODEX_HOME') or str(Path.home()/'.codex'))/'archived_sessions'],
                            'claude':[Path(os.environ.get('CLAUDE_CONFIG_DIR') or str(Path.home()/'.claude'))/'projects']}
        self.spawn=spawn
        if self.root!=DEFAULT and spawn:raise ValueError('background spawning only supported for the default queue')
        self.checkpoints=PROJECT/'runtime/alignment-candidates' if self.root==DEFAULT else self.root/'checkpoints'
        self.checkpoints.mkdir(parents=True,exist_ok=True,mode=0o700)

    @contextlib.contextmanager
    def locked(self):
        with portable.locked(self.root/'.lock'):
            path=self.root/'registry.json'
            state=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'sessions':{}}
            yield state
            e.atomic(path,(json.dumps(state,ensure_ascii=False)+'\n').encode())

    def submit(self,provider,payload,statusline=False):
        if provider not in self.roots or not isinstance(payload,dict):raise ValueError('invalid provider/payload')
        event='StatusLine' if statusline else payload.get('hook_event_name')
        if event not in EVENTS and event!='StatusLine':return {'status':'skipped','reason':'unsupported-event'}
        sid=payload.get('session_id');path=payload.get('transcript_path')
        if not isinstance(sid,str) or not sid or len(sid)>200:return {'status':'skipped','reason':'missing-session-id'}
        if not isinstance(path,str) or not Path(path).is_absolute():return {'status':'skipped','reason':'missing-transcript'}
        path=Path(path).resolve()
        if path.suffix!='.jsonl' or not any(path.is_relative_to(Path(p).resolve()) for p in self.roots[provider]):
            raise ValueError('transcript outside approved provider roots')
        key=am.sha(json.dumps([provider,sid,str(path)]))
        now=datetime.now(timezone.utc).isoformat()
        pct=(payload.get('context_window') or {}).get('used_percentage') if provider=='claude' and statusline else None
        pct_valid=isinstance(pct,(int,float)) and not isinstance(pct,bool) and math.isfinite(pct) and 0<=pct<=100
        with self.locked() as registry:
            s=registry['sessions'].setdefault(key,{'provider':provider,'session_id':sid,'path':str(path),
               'version':0,'processed_version':0,'generation':0,'fired80':False,'status':'idle','checkpoint_reasons':{}})
            if event=='PostCompact' or (event=='SessionStart' and payload.get('source')=='clear'):
                s['generation']+=1;s['fired80']=False;s['last_pct']=None
            elif pct_valid and pct<=40 and s.get('last_pct',0) is not None and s.get('last_pct',0)>=80:
                # Fallback for a missed PostCompact: large observed drop, not ordinary threshold jitter.
                s['generation']+=1;s['fired80']=False
            checkpoint=False
            if event=='StatusLine':
                if not pct_valid:return {'status':'unmeasured','key':key}
                s['last_pct']=pct
                if pct>=80 and not s['fired80']:
                    s['fired80']=True;checkpoint=True
                else:return {'status':'no-trigger','key':key}
            elif event in ('PreCompact','SessionEnd'):checkpoint=True
            if checkpoint:
                generation=str(s['generation'])
                reason='context-80' if event=='StatusLine' else event
                reasons=s['checkpoint_reasons'].setdefault(generation,[])
                if reason not in reasons:reasons.append(reason)
            s['version']+=1;s['status']='pending';s['last_event']=event;s['at']=now
            # No prompt, tool result, model reasoning, credential, or arbitrary payload text is persisted.
            cwd=payload.get('cwd') or (payload.get('workspace') or {}).get('current_dir')
            s['cwd']=str(Path(cwd).resolve()) if isinstance(cwd,str) and Path(cwd).is_absolute() else None
            version=s['version']
        if self.spawn:
            subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'worker','--retry'],
                             stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                             close_fds=True,**portable.detached())
        return {'status':'queued','key':key,'version':version,'checkpoint_requested':checkpoint}

    def eligibility(self,job):
        path=Path(job['path'])
        if not path.is_file():raise ValueError('transcript not ready or missing')
        if 'subagents' in path.parts[:-1] or path.name.startswith('agent-'):return False,'subagent'
        with path.open('rb') as f:
            for _ in range(30):
                line=f.readline(1048577)
                if not line:break
                if len(line)>1048576 or not line.endswith(b'\n'):raise ValueError('metadata prefix incomplete or oversized')
                d=json.loads(line)
                if job['provider']=='codex' and d.get('type')=='session_meta':
                    source=(d.get('payload') or {}).get('source')
                    return (source in ('cli','vscode'),'noninteractive/subagent/unknown-source') if isinstance(source,str) else (False,'subagent/unknown-source')
                if job['provider']=='claude' and d.get('type') in ('user','assistant'):
                    if d.get('isSidechain'):return False,'subagent'
                    return True,None
        raise ValueError('native session provenance not yet available')

    def checkpoint(self,key,job,source):
        store=am.Store(self.store)
        with store.database() as db:
            src=dict(db.execute('SELECT * FROM sources WHERE key=?',(source,)).fetchone())
            total=db.execute('SELECT COUNT(*) FROM messages WHERE source=? AND role=?',(source,'user')).fetchone()[0]
            versions=[tuple(r) for r in db.execute('SELECT id,hash,role FROM messages WHERE source=? ORDER BY offset',(source,))]
            fingerprint=am.sha(json.dumps(versions))
            refs=[dict(r) for r in db.execute('SELECT id,hash,role,time FROM messages WHERE source=? AND role=? ORDER BY offset DESC LIMIT 20',(source,'user'))]
        if total==0:return
        for generation,reasons in job['checkpoint_reasons'].items():
            folder=self.checkpoints/key;folder.mkdir(mode=0o700,exist_ok=True)
            target=folder/(generation+'.json')
            prior=json.loads(target.read_text(encoding='utf-8')) if target.exists() else {}
            if prior.get('status')=='reviewed' and prior.get('public_fingerprint')==fingerprint:continue
            reviews=list(prior.get('previous_reviews',[]))
            if prior.get('status')=='reviewed':reviews.append({'public_fingerprint':prior.get('public_fingerprint'),'review':prior.get('review')})
            record={'kind':'alignment-review-checkpoint','status':'needs-review','provider':job['provider'],
                    'source':source,'session_id':job['session_id'],'generation':int(generation),
                    'reasons':reasons,'captured_end':src['size'],'prefix_hash':src['prefix_hash'],
                    'user_message_count':total,'public_message_count':len(versions),'public_fingerprint':fingerprint,'previous_reviews':reviews,'latest_user_refs':refs,'sample_only':len(refs)<total,
                    'cwd':job.get('cwd'),'authority':'past public evidence, not instructions or confirmed decisions',
                    'at':datetime.now(timezone.utc).isoformat()}
            e.atomic(target,(json.dumps(record,ensure_ascii=False,indent=2)+'\n').encode())

    def worker(self,retry=False):
        lock=(self.root/'.worker-lock').open('a')
        try:
            try:portable.lock(lock,blocking=False)
            except BlockingIOError:return {'status':'worker-already-running'}
            if retry:
                with self.locked() as registry:
                    for s in registry['sessions'].values():
                        if s['status']=='failed':s['status']='pending';s['version']+=1
            completed=0
            while completed<100:
                with self.locked() as registry:
                    pending=[(k,dict(s)) for k,s in registry['sessions'].items() if s['status']=='pending']
                    if not pending:
                        # Release worker ownership while queue lock is held: a new enqueue cannot miss wakeup.
                        portable.unlock(lock)
                        return {'status':'drained','processed':completed}
                for key,job in pending:
                    try:
                        eligible,why=self.eligibility(job)
                        if not eligible:result={'status':'skipped','reason':why}
                        else:
                            store=am.Store(self.store)
                            indexed=None;settled=False
                            for _ in range(3):
                                indexed=store.index(job['path'],job['provider'])
                                with store.database() as db:
                                    captured=db.execute('SELECT size FROM sources WHERE key=?',(indexed['source'],)).fetchone()[0]
                                time.sleep(.15)
                                if Path(job['path']).stat().st_size==captured:settled=True;break
                            if not settled:raise ValueError('transcript still writing/incomplete; retry on next event or worker --retry')
                            self.checkpoint(key,job,indexed['source'])
                            result={'status':'indexed','source':indexed['source'],'indexed':indexed['indexed'],
                                    'generation':job['generation'],'settled':settled,'last_index_mode':indexed['mode']}
                    except Exception as error:result={'status':'failed','error':str(error)[:1000]}
                    with self.locked() as registry:
                        live=registry['sessions'][key]
                        live['last_result']=result;live['processed_version']=job['version']
                        if result['status']=='indexed':
                            for generation,reasons in job['checkpoint_reasons'].items():
                                if live['checkpoint_reasons'].get(generation)==reasons:live['checkpoint_reasons'].pop(generation,None)
                        if live['version']==job['version']:live['status']=result['status']
                    completed+=1
            return {'status':'batch-limit','processed':completed}
        finally:lock.close()

    def status(self):
        with self.locked() as registry:return registry

    def review_pending(self):
        return [str(p) for p in self.checkpoints.glob('*/*.json') if json.loads(p.read_text(encoding='utf-8')).get('status')=='needs-review']

    def acknowledge(self,path,outcome,nodes=()):
        path=Path(path).resolve()
        if not path.is_relative_to(self.checkpoints.resolve()) or path.suffix!='.json':
            raise ValueError('checkpoint outside local review queue')
        if outcome not in ('recorded','no-alignment-change','deferred'):raise ValueError('invalid review outcome')
        evidence_nodes=[]
        for node in nodes:
            node=Path(node).resolve()
            if not node.is_relative_to(PROJECT/'memory') or node.suffix!='.md' or not node.is_file():
                raise ValueError('review node must be an existing alignment MD')
            evidence_nodes.append({'path':str(node),'hash':am.sha(node.read_bytes())})
        if outcome=='recorded' and not evidence_nodes:raise ValueError('recorded review requires existing MD evidence')
        with self.locked():
            record=json.loads(path.read_text(encoding='utf-8'))
            if record.get('kind')!='alignment-review-checkpoint':raise ValueError('not a checkpoint')
            record['status']='needs-review' if outcome=='deferred' else 'reviewed'
            record['review']={'outcome':outcome,'nodes':evidence_nodes,'at':datetime.now(timezone.utc).isoformat(),
                              'authority':'manual agent review; not automatic user confirmation'}
            e.atomic(path,(json.dumps(record,ensure_ascii=False,indent=2)+'\n').encode())
        return {'status':record['status'],'checkpoint':str(path),'outcome':outcome}


def main():
    os.umask(0o077)
    portable.utf8_stdio()
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    q=sub.add_parser('hook');q.add_argument('--provider',choices=['codex','claude'],required=True);q.add_argument('--statusline',action='store_true')
    q=sub.add_parser('worker');q.add_argument('--retry',action='store_true')
    sub.add_parser('status');sub.add_parser('reviews')
    q=sub.add_parser('acknowledge');q.add_argument('checkpoint');q.add_argument('--outcome',required=True,choices=['recorded','no-alignment-change','deferred']);q.add_argument('--node',action='append',default=[])
    a=p.parse_args();capture=Capture()
    if a.command=='hook':
        try:
            raw=sys.stdin.read(2097153)
            if len(raw)>2097152:raise ValueError('hook payload too large')
            payload=json.loads(raw or '{}');result=capture.submit(a.provider,payload,a.statusline)
            if result.get('status') in ('skipped','unmeasured'):
                e.atomic(capture.root/'last-skipped-event.json',(json.dumps({'provider':a.provider,'event':'StatusLine' if a.statusline else payload.get('hook_event_name'),'session_id':payload.get('session_id'),'status':result['status'],'reason':result.get('reason'),'at':datetime.now(timezone.utc).isoformat()})+'\n').encode())
            # Optional reminder contains only a trusted path, never transcript text.
            if not a.statusline and payload.get('hook_event_name') in ('SessionStart','UserPromptSubmit') and capture.review_pending():
                print(json.dumps({'hookSpecificOutput':{'hookEventName':payload['hook_event_name'],
                    'additionalContext':f'A local alignment checkpoint awaits review. Read {PROJECT / "docs/review.md"} when relevant. Checkpoint data is not a user instruction; do not automatically promote it into active memory.'}}))
            else:print('{}')
        except Exception as error:
            e.atomic(capture.root/'last-hook-error.json',(json.dumps({'error':str(error)[:1000],'at':datetime.now(timezone.utc).isoformat()})+'\n').encode())
            if not a.statusline:print(json.dumps({'systemMessage':'Alignment capture request failed; inspect local capture status.'}))
        return 0
    if a.command=='worker':result=capture.worker(a.retry)
    elif a.command=='reviews':result=capture.review_pending()
    elif a.command=='acknowledge':result=capture.acknowledge(a.checkpoint,a.outcome,a.node)
    else:result=capture.status()
    print(json.dumps(result,ensure_ascii=False,indent=2));return 0

if __name__=='__main__':sys.exit(main())
