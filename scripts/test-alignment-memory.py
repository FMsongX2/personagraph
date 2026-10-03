#!/usr/bin/env python3
"""Public-only isolated fixtures for native alignment evidence storage."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import time
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('alignment', Path(__file__).with_name('alignment-memory.py'))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)


def event(mid, text, role='user', kinds=None):
    return {'type': 'response_item', 'payload': {'type': 'message', 'id': mid, 'role': role,
            'phase': 'final_answer' if role == 'assistant' else None,
            'content': [{'type': 'input_text' if role == 'user' else 'output_text', 'text': text}],
            'internal_chat_message_metadata_passthrough': {'content_item_kinds': kinds or ['user.text']}}}

class Tests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='alignment-test-', dir=m.PROJECT / 'runtime'))
        self.source = self.root / 'native.jsonl'
        self.store = m.Store(self.root / 'store')
        self.events = [event('user1', 'CLI preferred because MCP slow'), event('answer1', 'native index snapshot', 'assistant')]
        self.write()

    def tearDown(self):
        shutil.rmtree(self.root)

    def write(self):
        self.source.write_text(''.join(json.dumps(d) + '\n' for d in self.events))

    def index(self):
        return self.store.index(self.source, 'codex', 'test-session')

    def test_import_search_idempotence_and_append(self):
        self.assertEqual(self.index()['indexed'], 2)
        self.assertEqual(self.index()['indexed'], 2)
        hits = self.store.search('MCP'); self.assertEqual(hits[0]['source'], 'test-session')
        self.assertEqual(hits[0]['public_messages'], 2)
        self.assertEqual(self.store.find('test-session','MCP')['matches'][0]['id'], 'user1')
        self.assertEqual(self.store.get('test-session', 'user1')['text'], self.events[0]['payload']['content'][0]['text'])
        self.events.append(event('user2', 'approved')); self.write()
        self.assertEqual(self.index()['indexed'], 3)

    def test_source_move_and_unsnapped_missing(self):
        self.index(); r = self.store.get('test-session', 'user1', pin=True)
        moved = self.root / 'moved.jsonl'; self.source.rename(moved)
        self.assertEqual(self.store.get('test-session', 'user1', r['hash'])['retrieved_from'], 'historical-snapshot')
        with self.assertRaises(ValueError): self.store.get('test-session', 'answer1')
        self.store.index(moved, 'codex', 'test-session')
        self.assertEqual(self.store.get('test-session', 'answer1')['retrieved_from'], 'native')

    def test_versions_and_modified_pointer(self):
        self.index(); original = self.store.get('test-session', 'user1', pin=True)
        self.events[0]['payload']['content'][0]['text'] = 'changed choice'; self.write()
        with self.assertRaises(ValueError): self.store.get('test-session', 'answer1')
        self.index()
        old = self.store.get('test-session', 'user1', original['hash'])
        self.assertEqual(old['text'], original['text']); self.assertEqual(old['retrieved_from'], 'historical-snapshot')
        self.assertEqual(self.store.get('test-session', 'user1')['text'], 'changed choice')

    def test_invalid_complete_record_rolls_back(self):
        self.index()
        with self.source.open('a') as f: f.write('broken\n')
        with self.assertRaises(ValueError): self.index()
        self.assertEqual(self.store.status()[0]['indexed'], 2)

    def test_partial_tail_recovery(self):
        self.index()
        with self.source.open('a') as f: f.write('{"incomplete":')
        r = self.index(); self.assertTrue(r['partial_tail']); self.assertEqual(r['indexed'], 2)
        self.events.append(event('user2', 'tail completed')); self.write()
        self.assertFalse(self.index()['partial_tail']); self.assertEqual(self.index()['indexed'], 3)

    def test_provenance_separation(self):
        self.events += [event('injected', 'private marker', kinds=['agents_md.instructions']), event('unknown', 'unsafe origin', kinds=['new.kind'])]
        mixed = event('mixed', 'human words'); mixed['payload']['content'].append({'type': 'input_text', 'text': 'injected marker'})
        mixed['payload']['internal_chat_message_metadata_passthrough']['content_item_kinds'].append('agents_md.instructions')
        self.events.append(mixed); self.write()
        r = self.index(); self.assertEqual(r['indexed'], 3); self.assertEqual(r['quarantined'], 1)
        self.assertEqual(self.store.get('test-session', 'mixed')['text'], 'human words')
        self.assertEqual(self.store.search('marker'), [])

    def test_claude_human_tool_thinking_separation(self):
        events = [{'type': 'user', 'uuid': 'cu', 'origin': {'kind': 'human'}, 'message': {'role': 'user', 'content': [
                  {'type': 'text', 'text': '<ide_opened_file>injected</ide_opened_file>'}, {'type': 'text', 'text': 'real request'}]}},
                  {'type': 'assistant', 'uuid': 'ca', 'message': {'role': 'assistant', 'content': [
                  {'type': 'thinking', 'thinking': 'hidden fixture'}, {'type': 'text', 'text': 'public reply'}, {'type': 'tool_use'}]}},
                  {'type': 'user', 'uuid': 'ct', 'message': {'role': 'user', 'content': [{'type': 'tool_result'}]}},
                  {'type': 'user', 'uuid': 'cx', 'message': {'role': 'user', 'content': 'unknown origin'}}]
        self.source.write_text(''.join(json.dumps(x)+'\n' for x in events))
        r = self.store.index(self.source, 'claude', 'claude-test')
        self.assertEqual(r['indexed'], 2); self.assertEqual(r['quarantined'], 1)
        self.assertEqual(self.store.get('claude-test', 'cu')['text'], 'real request')
        self.assertEqual(self.store.get('claude-test', 'ca')['text'], 'public reply')
        self.assertEqual(self.store.search('hidden OR injected'), [])

    def test_conflicting_duplicate_rolls_back(self):
        self.index(); self.events.append(event('user1', 'different')); self.write()
        with self.assertRaises(ValueError): self.index()
        self.assertEqual(self.store.status()[0]['indexed'], 2)

    def test_unrelated_source_rebind_refused(self):
        self.index(); other = self.root / 'other.jsonl'; other.write_text(json.dumps(event('other', 'unrelated'))+'\n')
        with self.assertRaises(ValueError): self.store.index(other, 'codex', 'test-session')
        self.assertEqual(self.store.status()[0]['path'], str(self.source))

    def test_snapshot_tamper_and_path_confinement(self):
        self.index(); r = self.store.get('test-session', 'user1', pin=True)
        snap = Path(r['snapshot']); saved = json.loads(snap.read_text()); saved['text'] = 'tampered'; snap.write_text(json.dumps(saved))
        with self.assertRaises(ValueError): self.store.get('test-session', 'user1', pin=True)
        self.source.unlink()
        with self.assertRaises(ValueError): self.store.get('test-session', 'user1', r['hash'])
        self.assertEqual(self.store.snapshot_path('../../escape', '../../escape', r['hash']).parent, self.store.snapdir)
        with self.assertRaises(ValueError): m.Store(m.PROJECT / 'outside-runtime')

    def test_bounded_context_and_historical_refusal(self):
        self.index()
        r = self.store.context('test-session', 'answer1', before=1)
        self.assertEqual([x['id'] for x in r['messages']], ['user1', 'answer1'])
        saved = self.store.get('test-session', 'answer1', pin=True)
        self.source.unlink()
        with self.assertRaises(ValueError): self.store.context('test-session', 'answer1', expected=saved['hash'])

    def test_korean_search_and_source_filter(self):
        self.events += [event('korean1', '오빠 그땐 움.. 을 써야지'), event('korean2', '원본 기록에 색인과 근거 스냅샷을 붙이자')]
        self.write(); self.index()
        self.assertIn('korean1', [r['id'] for r in self.store.find('test-session','움')['matches']])
        self.assertIn('korean2', [r['id'] for r in self.store.find('test-session','근거 스냅샷')['matches']])
        self.assertEqual(self.store.search('움', source='other-source'), [])
        self.assertTrue(all(r['backend']=='aichat-search' for r in self.store.search('움')))

    def test_failed_search_build_preserves_previous_pair(self):
        self.index(); previous=(self.store.root/'search-current').resolve()
        self.events.append(event('added','new material'));self.write()
        with patch.object(self.store,'build_search',side_effect=ValueError('build fixture failed')):
            with self.assertRaises(ValueError):self.index()
        hits=self.store.search('MCP');self.assertEqual(hits[0]['pending_failures'][0]['status'],'failed')
        self.assertEqual((self.store.root/'search-current').resolve(),previous)
        self.assertEqual(self.store.status()[0]['indexed'],2)
        with self.assertRaises(ValueError):self.store.get('test-session','added')
        self.assertEqual(self.store.search('material'),[])
        self.index();self.assertEqual(self.store.get('test-session','added')['text'],'new material')
        self.assertEqual(self.store.search('material')[0]['pending_failures'],[])

    def test_noop_and_append_parse_only_new_records(self):
        self.index();generation=(self.store.root/'search-current').resolve()
        with patch.object(self.store,'build_search',side_effect=AssertionError('no rebuild expected')):
            r=self.index()
        self.assertEqual(r['mode'],'unchanged');self.assertEqual(r['parsed_records'],0)
        self.assertEqual((self.store.root/'search-current').resolve(),generation)
        self.events.append(event('added','delta unique'));self.write()
        r=self.index();self.assertEqual(r['mode'],'append');self.assertEqual(r['parsed_records'],1)
        self.assertEqual(r['search_updated_sessions'],1);self.assertEqual(r['indexed'],3)
        self.assertTrue(generation.exists())

    def test_rewrite_and_append_detected_not_treated_as_append(self):
        self.index();old=self.store.get('test-session','user1',pin=True)
        self.events[0]['payload']['content'][0]['text']='edited earlier text with a longer prefix'
        self.events.append(event('added','delta'));self.write()
        r=self.index();self.assertEqual(r['mode'],'full');self.assertEqual(r['parsed_records'],3)
        self.assertEqual(self.store.search('MCP'),[])
        self.assertEqual(self.store.get('test-session','user1',old['hash'])['text'],old['text'])

    def test_update_does_not_reparse_unavailable_other_session(self):
        self.index();other=self.root/'other.jsonl';other.write_text(json.dumps(event('other','other needle'))+'\n')
        self.store.index(other,'codex','other-session');other.unlink()
        self.events.append(event('added','only changed session'));self.write()
        r=self.index();self.assertEqual(r['search_updated_sessions'],1)
        hit=self.store.search('needle')[0]
        self.assertEqual(hit['source'],'other-session');self.assertFalse(hit['source_present'])
        self.assertEqual(self.store.find('other-session','needle')['unavailable_count'],1)

    def test_publication_failure_preserves_previous_pair(self):
        self.index();previous=(self.store.root/'search-current').resolve()
        self.events.append(event('added','publication needle'));self.write()
        with patch.object(self.store,'publish_generation',side_effect=OSError('publication fixture failed')):
            with self.assertRaises(OSError):self.index()
        self.assertEqual((self.store.root/'search-current').resolve(),previous)
        self.assertEqual(self.store.status()[0]['indexed'],2)
        self.assertEqual(self.store.search('needle'),[])
        self.index();self.assertEqual(self.store.status()[0]['indexed'],3)

    def test_native_changes_during_search_build_rejected(self):
        self.index();previous=(self.store.root/'search-current').resolve()
        self.events.append(event('added','new capture'));self.write()
        original=self.store.build_search
        def mutate(*args,**kwargs):
            result=original(*args,**kwargs)
            self.events[0]['payload']['content'][0]['text']='changed during build';self.write()
            return result
        with patch.object(self.store,'build_search',side_effect=mutate):
            with self.assertRaises(ValueError):self.index()
        self.assertEqual((self.store.root/'search-current').resolve(),previous)
        self.assertEqual(self.store.status()[0]['indexed'],2)

    def test_failure_of_new_session_leaves_existing_session_searchable(self):
        self.index();other=self.root/'other.jsonl';other.write_text(json.dumps(event('other','new keyword'))+'\n')
        with patch.object(self.store,'build_search',side_effect=ValueError('other failed')):
            with self.assertRaises(ValueError):self.store.index(other,'codex','other-session')
        self.assertEqual(self.store.search('MCP')[0]['source'],'test-session')
        self.assertEqual(len(self.store.status()),1)
        self.store.index(other,'codex','other-session')
        self.assertEqual(self.store.search('keyword')[0]['pending_failures'],[])

    def test_session_candidates_not_message_documents(self):
        self.events += [event('extra'+str(i),'same keyword') for i in range(20)]
        self.write(); r = self.index()
        self.assertEqual(r['search_sessions'],1)
        self.assertEqual(r['indexed'],22)
        self.assertEqual(len(self.store.search('keyword')),1)
        other = self.root/'second.jsonl'
        other.write_text(json.dumps(event('different','same keyword'))+'\n')
        self.store.index(other,'codex','second-session')
        hits = self.store.search('keyword')
        self.assertEqual({x['source'] for x in hits},{'test-session','second-session'})
        self.assertEqual(len(self.store.search('keyword',source='second-session')),1)
        narrowed = self.store.find('test-session','keyword',limit=3)
        self.assertEqual(len(narrowed['matches']),3); self.assertTrue(narrowed['limit_reached'])
        self.assertLess(narrowed['scanned'],narrowed['indexed_messages'])

    def test_session_match_not_claimed_as_exact_message(self):
        self.events = [event('first','boundary'),event('second','joined')]
        self.write(); self.index()
        self.assertEqual(len(self.store.search('boundary joined')),1)
        self.assertEqual(self.store.find('test-session','boundary joined')['matches'],[])
        self.assertEqual(self.store.search('boundary')[0]['verification'],'candidate-only; use find/get')

    def test_missing_source_find_reports_incomplete(self):
        self.index(); old = self.store.get('test-session','user1',pin=True)
        self.source.unlink()
        candidates = self.store.search('MCP')
        self.assertEqual(candidates[0]['source'],'test-session')
        result = self.store.find('test-session','MCP')
        self.assertEqual(result['matches'],[]); self.assertEqual(result['unavailable_count'],2)
        self.assertEqual(self.store.get('test-session','user1',old['hash'])['retrieved_from'],'historical-snapshot')

    def test_scan_budget_does_not_claim_absence(self):
        self.index()
        partial = self.store.find('test-session','MCP',scan_limit=1)
        self.assertEqual(partial['matches'],[])
        self.assertTrue(partial['scan_limit_reached']); self.assertFalse(partial['complete_scan'])
        complete = self.store.find('test-session','MCP',scan_limit=2)
        self.assertEqual(complete['matches'][0]['id'],'user1')
        self.assertTrue(complete['complete_scan'])

    def test_cli_empty_search_reports_failed_pending_update(self):
        self.index();self.events.append(event('added','pending secret needle'));self.write()
        with patch.object(self.store,'build_search',side_effect=ValueError('failed update')):
            with self.assertRaises(ValueError):self.index()
        out=subprocess.check_output([sys.executable,str(m.PROJECT/'scripts/alignment-memory.py'),
              '--store',str(self.store.root),'search','never-indexed-needle'],text=True)
        result=json.loads(out);self.assertEqual(result['sessions'],[])
        self.assertEqual(result['pending_updates'][0]['status'],'failed')
        self.assertEqual(result['generation'],(self.store.root/'search-current').resolve().name)

    def test_killed_import_keeps_active_pair_and_can_resume(self):
        self.index();previous=(self.store.root/'search-current').resolve()
        self.events.append(event('added','killed import needle'));self.write()
        checkpoint=self.root/'checkpoint'
        code="""import importlib.util,sys,time
from pathlib import Path
spec=importlib.util.spec_from_file_location('am',sys.argv[1]);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
s=m.Store(sys.argv[2])
def blocked(*args,**kwargs):
 Path(sys.argv[4]).write_text('staged')
 time.sleep(30)
s.build_search=blocked
s.index(sys.argv[3],'codex','test-session')
"""
        process=subprocess.Popen([sys.executable,'-c',code,str(m.PROJECT/'scripts/alignment-memory.py'),
                                  str(self.store.root),str(self.source),str(checkpoint)],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            deadline=time.monotonic()+5
            while not checkpoint.exists() and process.poll() is None and time.monotonic()<deadline:time.sleep(.02)
            self.assertTrue(checkpoint.exists())
            process.terminate();process.communicate(timeout=5)
            self.assertEqual((self.store.root/'search-current').resolve(),previous)
            self.assertEqual(self.store.status()[0]['indexed'],2)
            self.assertEqual(self.store.search('MCP')[0]['pending_failures'][0]['status'],'pending')
            self.index();self.assertEqual(self.store.status()[0]['indexed'],3)
            self.assertEqual(self.store.search('needle')[0]['pending_failures'],[])
        finally:
            if process.poll() is None:process.kill();process.communicate()

    def test_read_only_lookup_does_not_open_write_locks_or_change_catalog(self):
        self.index();r=self.store.get('test-session','user1',pin=True)
        hashes={str(p):m.sha(p.read_bytes()) for p in self.store.root.rglob('*') if p.is_file()}
        reader=m.Store(self.store.root,read_only=True)
        with patch.object(m.fcntl,'flock',side_effect=AssertionError('get must not open catalog write lock')):
            self.assertEqual(reader.get('test-session','user1')['text'],r['text'])
        self.assertEqual(reader.search('MCP')[0]['source'],'test-session')
        self.assertEqual(hashes,{str(p):m.sha(p.read_bytes()) for p in self.store.root.rglob('*') if p.is_file()})
        with self.assertRaises(ValueError):reader.get('test-session','user1',pin=True)
        self.source.unlink();catalog=self.store.dbpath;catalog.unlink()
        self.assertEqual(reader.get('test-session','user1',r['hash'])['retrieved_from'],'historical-snapshot')
        self.assertFalse(catalog.exists())

    def test_single_device_concurrent_import(self):
        cmd = [sys.executable, str(m.PROJECT / 'scripts/alignment-memory.py'), '--store', str(self.store.root), 'index', str(self.source), '--adapter', 'codex', '--key', 'test-session']
        ps = [subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
        for p in ps:
            out, err = p.communicate(timeout=20); self.assertEqual(p.returncode, 0, err.decode())
        self.assertEqual(self.store.status()[0]['indexed'], 2)
        self.assertEqual(len(self.store.search('native')), 1)

if __name__ == '__main__':
    os.umask(0o077)
    unittest.main()
