#!/usr/bin/env python3
"""Isolated permanent-data migration, loss recovery, and backup failure tests."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch,Mock

spec=importlib.util.spec_from_file_location('am',Path(__file__).with_name('alignment-memory.py'))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
e=m.evidence

class Tests(unittest.TestCase):
    def setUp(self):
        self.root=Path(tempfile.mkdtemp(prefix='evidence-backup-test-',dir=m.PROJECT/'runtime'))
        self.permanent=self.root/'permanent'
        self.store=m.Store(self.root/'cache',evidence_dir=self.permanent)
        self.source=self.root/'native.jsonl'
        self.write('original public text')
        self.backup=e.Backup(self.permanent,self.root/'backup-repo',self.root/'private/password',
                             self.root/'private/state',self.root/'restic-cache')

    def tearDown(self): shutil.rmtree(self.root)

    def write(self,text):
        d={'type':'response_item','payload':{'type':'message','id':'message1','role':'user',
            'content':[{'type':'input_text','text':text}],
            'internal_chat_message_metadata_passthrough':{'content_item_kinds':['user.text']}}}
        self.source.write_text(json.dumps(d)+'\n')

    def pin(self):
        self.store.index(self.source,'codex','fixture')
        return self.store.get('fixture','message1',pin=True)

    def test_restore_without_original_cache_or_evidence(self):
        old=self.pin();self.write('changed public text');new=self.pin()
        saved=self.backup.backup();self.assertEqual(saved['files'],2)
        self.assertEqual(self.backup.check(True)['status'],'verified')
        self.assertTrue(self.backup.backup()['reused'])
        self.assertTrue(e.portable.is_private(self.backup.password))
        if not e.portable.WINDOWS:self.assertEqual(self.backup.password.stat().st_mode&0o777,0o600)
        self.source.unlink();shutil.rmtree(self.store.root);shutil.rmtree(self.permanent)
        restored=self.backup.restore(saved['snapshot']);self.assertEqual(restored['added'],2)
        fresh=m.Store(self.root/'cache',evidence_dir=self.permanent)
        for r in (old,new):
            found=fresh.get('fixture','message1',r['hash'])
            self.assertEqual(found['text'],r['text']);self.assertEqual(found['retrieved_from'],'historical-snapshot')

    def test_restore_conflict_does_not_overwrite_existing_evidence(self):
        record=self.pin();saved=self.backup.backup()
        path=Path(record['snapshot']);changed=json.loads(path.read_text());changed['occurred_at']='different metadata'
        path.write_text(json.dumps(changed)+'\n');before=path.read_bytes()
        with self.assertRaises(ValueError):self.backup.restore(saved['snapshot'])
        self.assertEqual(path.read_bytes(),before)

    def test_manifest_and_migration_preflight(self):
        record=self.pin();legacy=self.root/'legacy';legacy.mkdir()
        path=legacy/e.filename(record);path.write_bytes(Path(record['snapshot']).read_bytes())
        result=e.merge_files(legacy,self.root/'migrated')
        self.assertEqual(result['added'],1);self.assertEqual(e.read_snapshot(self.root/'migrated'/path.name)['hash'],record['hash'])
        bad=json.loads(path.read_text());bad['text']='bad content';path.write_text(json.dumps(bad))
        with self.assertRaises(ValueError):e.merge_files(legacy,self.root/'not-promoted')
        self.assertFalse((self.root/'not-promoted').exists())
        path.write_bytes(Path(record['snapshot']).read_bytes())
        (legacy/'.manifest.json').write_text(json.dumps({'version':1,'files':{}}))
        with self.assertRaises(ValueError):e.merge_files(legacy,self.root/'not-promoted',require_manifest=True)
        self.assertFalse((self.root/'not-promoted').exists())

    def test_existing_repository_missing_password_not_regenerated(self):
        self.backup.repository.mkdir();(self.backup.repository/'config').write_text('existing repository marker')
        with self.assertRaises(ValueError):self.backup.setup()
        self.assertFalse(self.backup.password.exists())

    def test_pin_persisted_backup_failure_reported(self):
        self.store.index(self.source,'codex','fixture')
        failed=Mock();failed.backup.side_effect=ValueError('backup failure fixture')
        with patch.object(m,'DEFAULT',self.store.root),patch.object(e,'PERMANENT',self.permanent),patch.object(e,'default_backup',return_value=failed):
            r=self.store.get('fixture','message1',pin=True)
        self.assertEqual(r['backup']['status'],'failed');self.assertTrue(r['backup']['snapshot_saved'])
        self.assertEqual(e.read_snapshot(r['snapshot'])['text'],'original public text')

    def test_restore_target_confinement_and_explicit_version(self):
        with self.assertRaises(ValueError):self.backup.restore('latest')
        with self.assertRaises(ValueError):self.backup.restore('a'*64,target='/tmp/outside-evidence-test')

if __name__=='__main__':
    os.umask(0o077)
    unittest.main()
