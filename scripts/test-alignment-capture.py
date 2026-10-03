#!/usr/bin/env python3
import importlib.util,json,os,shutil,tempfile,unittest
from pathlib import Path
spec=importlib.util.spec_from_file_location('capture',Path(__file__).with_name('alignment-capture.py'))
c=importlib.util.module_from_spec(spec);spec.loader.exec_module(c)

class Tests(unittest.TestCase):
    def setUp(self):
        self.root=Path(tempfile.mkdtemp(prefix='capture-test-',dir=c.PROJECT/'runtime'))
        self.native=self.root/'native.jsonl'
        self.native.write_text(json.dumps({'type':'session_meta','payload':{'id':'fixture-session','source':'cli'}})+'\n'+json.dumps({'type':'response_item','payload':{'type':'message','role':'user','id':'user1','content':[{'type':'input_text','text':'public preference'}],'internal_chat_message_metadata_passthrough':{'content_item_kinds':['user.text']}}})+'\n')
        self.capture=c.Capture(self.root/'queue',self.root/'store',{'codex':[self.root],'claude':[self.root]},spawn=False)
        self.payload={'session_id':'fixture-session','transcript_path':str(self.native),'cwd':str(self.root),'hook_event_name':'Stop','prompt':'private input is not copied','secret':'not copied'}

    def tearDown(self):shutil.rmtree(self.root)
    def percent(self,n):return dict(self.payload,context_window={'used_percentage':n})

    def test_enqueue_coalesces_and_does_not_copy_payload(self):
        a=self.capture.submit('codex',self.payload);b=self.capture.submit('codex',self.payload)
        self.assertEqual(a['key'],b['key']);self.assertEqual(b['version'],2)
        text=(self.capture.root/'registry.json').read_text();self.assertNotIn('private input',text);self.assertNotIn('not copied',text)
        self.assertEqual(len(self.capture.status()['sessions']),1)

    def test_80_once_per_generation_and_null_not_zero(self):
        self.assertEqual(self.capture.submit('claude',self.percent(None),True)['status'],'unmeasured')
        self.assertEqual(self.capture.submit('claude',self.percent(79),True)['status'],'no-trigger')
        self.assertTrue(self.capture.submit('claude',self.percent(80),True)['checkpoint_requested'])
        self.assertEqual(self.capture.submit('claude',self.percent(81),True)['status'],'no-trigger')
        self.capture.submit('claude',dict(self.payload,hook_event_name='PostCompact'))
        self.assertTrue(self.capture.submit('claude',self.percent(90),True)['checkpoint_requested'])
        s=next(iter(self.capture.status()['sessions'].values()));self.assertEqual(s['generation'],1)

    def test_codex_percent_not_inferred(self):
        result=self.capture.submit('codex',self.percent(90),True)
        self.assertEqual(result['status'],'unmeasured')
        self.assertFalse(next(iter(self.capture.status()['sessions'].values()))['fired80'])

    def test_worker_indexes_without_active_md_or_full_prompt_dump(self):
        memory={str(p):c.am.sha(p.read_bytes()) for p in (c.PROJECT/'memory').rglob('*.md')}
        result=self.capture.submit('codex',dict(self.payload,hook_event_name='PreCompact'))
        processed=self.capture.worker();self.assertEqual(processed['processed'],1)
        s=self.capture.status()['sessions'][result['key']];self.assertEqual(s['status'],'indexed')
        checkpoint=Path(self.capture.review_pending()[0]);d=json.loads(checkpoint.read_text())
        self.assertEqual(d['status'],'needs-review');self.assertEqual(d['latest_user_refs'][0]['id'],'user1')
        self.assertNotIn('public preference',checkpoint.read_text())
        self.assertEqual(memory,{str(p):c.am.sha(p.read_bytes()) for p in (c.PROJECT/'memory').rglob('*.md')})
        old=checkpoint.read_bytes();self.capture.submit('codex',self.payload);self.capture.worker()
        self.assertEqual(checkpoint.read_bytes(),old)

    def test_worker_skips_exec_and_subagent(self):
        for source in ['exec',{'subagent':{'thread_spawn':{}}}]:
            self.native.write_text(json.dumps({'type':'session_meta','payload':{'id':'fixture-session','source':source}})+'\n')
            result=self.capture.submit('codex',self.payload);self.capture.worker()
            self.assertEqual(self.capture.status()['sessions'][result['key']]['status'],'skipped')
        self.assertEqual(self.capture.review_pending(),[])

    def test_invalid_path_and_missing_transcript(self):
        with self.assertRaises(ValueError):self.capture.submit('codex',dict(self.payload,transcript_path='/tmp/not-approved.jsonl'))
        self.assertEqual(self.capture.submit('codex',dict(self.payload,transcript_path=None))['reason'],'missing-transcript')

    def test_manual_acknowledgement_does_not_write_memory(self):
        self.capture.submit('codex',dict(self.payload,hook_event_name='PreCompact'));self.capture.worker()
        path=Path(self.capture.review_pending()[0])
        with self.assertRaises(ValueError):self.capture.acknowledge(path,'recorded')
        self.capture.acknowledge(path,'no-alignment-change')
        self.assertEqual(self.capture.review_pending(),[])

    def test_claude_80_full_pipeline_and_rate_limit_ignored(self):
        self.native.write_text(json.dumps({'type':'user','uuid':'claude-user','sessionId':'fixture-session',
            'origin':{'kind':'human'},'message':{'role':'user','content':'literal public request'}})+'\n')
        rate=dict(self.payload,rate_limits={'five_hour':{'used_percentage':99}})
        self.assertEqual(self.capture.submit('claude',rate,True)['status'],'unmeasured')
        self.capture.submit('claude',self.percent(80),True);self.capture.worker()
        d=json.loads(Path(self.capture.review_pending()[0]).read_text())
        self.assertEqual(d['reasons'],['context-80'])
        self.assertEqual(d['latest_user_refs'][0]['id'],'claude-user')
        self.capture.submit('claude',dict(self.payload,hook_event_name='SessionStart',source='clear'))
        self.assertEqual(next(iter(self.capture.status()['sessions'].values()))['generation'],1)

    def test_new_public_content_reopens_reviewed_checkpoint(self):
        self.capture.submit('codex',dict(self.payload,hook_event_name='PreCompact'));self.capture.worker()
        path=Path(self.capture.review_pending()[0]);self.capture.acknowledge(path,'no-alignment-change')
        extra={'type':'response_item','payload':{'type':'message','role':'user','id':'user2',
          'content':[{'type':'input_text','text':'a later explicit preference'}],
          'internal_chat_message_metadata_passthrough':{'content_item_kinds':['user.text']}}}
        with self.native.open('a') as f:f.write(json.dumps(extra)+'\n')
        self.capture.submit('codex',dict(self.payload,hook_event_name='SessionEnd'));self.capture.worker()
        record=json.loads(path.read_text());self.assertEqual(record['status'],'needs-review')
        self.assertEqual(len(record['previous_reviews']),1);self.assertEqual(record['public_message_count'],2)

    def test_failure_retry_and_previous_index_preserved(self):
        self.capture.submit('codex',self.payload);self.capture.worker()
        with self.native.open('a') as f:f.write('bad record\n')
        result=self.capture.submit('codex',dict(self.payload,hook_event_name='PreCompact'));self.capture.worker()
        self.assertEqual(self.capture.status()['sessions'][result['key']]['status'],'failed')
        self.assertEqual(c.am.Store(self.capture.store).search('preference')[0]['public_messages'],1)
        lines=self.native.read_text().splitlines();self.native.write_text('\n'.join(lines[:-1])+'\n')
        self.capture.worker(retry=True)
        self.assertEqual(self.capture.status()['sessions'][result['key']]['status'],'indexed')
        self.assertEqual(len(self.capture.review_pending()),1)

if __name__=='__main__':os.umask(0o077);unittest.main()
