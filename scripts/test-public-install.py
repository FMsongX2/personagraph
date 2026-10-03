#!/usr/bin/env python3
import importlib.util,json,os,shutil,tempfile,unittest
from pathlib import Path
PROJECT=Path(__file__).resolve().parents[1]

def load(name,file):
    spec=importlib.util.spec_from_file_location(name,PROJECT/'scripts'/file);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
installer=load('installer','install-alignment-capture.py');observer=load('observer','statusline-observer.py')

class Tests(unittest.TestCase):
    def setUp(self):self.root=Path(tempfile.mkdtemp(prefix='public-install-test-',dir=PROJECT/'runtime'))
    def tearDown(self):shutil.rmtree(self.root)
    def test_preview_and_apply_preserve_user_hooks_without_orca(self):
        claude=self.root/'claude';codex=self.root/'codex';claude.mkdir();codex.mkdir()
        old={'hooks':{'Stop':[{'hooks':[{'type':'command','command':'echo existing'}]}]},'other_setting':True}
        (claude/'settings.json').write_text(json.dumps(old));(codex/'hooks.json').write_text('{}')
        preview=installer.install(claude,codex)
        self.assertFalse(preview['applied']);self.assertEqual(json.loads((claude/'settings.json').read_text()),old)
        installer.install(claude,codex,apply=True)
        new=json.loads((claude/'settings.json').read_text())
        self.assertEqual(new['hooks']['Stop'][0],old['hooks']['Stop'][0]);self.assertTrue(new['other_setting'])
        installer.install(claude,codex,apply=True)
        self.assertEqual(json.loads((claude/'settings.json').read_text()),new)
    def test_previous_statusline_stdout_is_forwarded(self):
        raw=b'{"fixture":true}'
        self.assertEqual(observer.render(raw,{'type':'command','command':'cat'},observe=False),raw)
        self.assertEqual(observer.render(raw,None,observe=False),b'')

if __name__=='__main__':os.umask(0o077);unittest.main()
