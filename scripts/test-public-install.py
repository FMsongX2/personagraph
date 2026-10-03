#!/usr/bin/env python3
import importlib.util,json,os,shutil,tempfile,unittest,subprocess
from unittest.mock import patch
from pathlib import Path
PROJECT=Path(__file__).resolve().parents[1]

def load(name,file):
    spec=importlib.util.spec_from_file_location(name,PROJECT/'scripts'/file);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
installer=load('installer','install-alignment-capture.py');observer=load('observer','statusline-observer.py')
search_installer=load('search_installer','install-aichat-search.py')

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
    def test_cached_build_folder_gets_own_checkout_not_parent_git(self):
        def git(path,*args):
            return subprocess.check_output(['git','-C',str(path),*args],stderr=subprocess.DEVNULL,text=True).strip()
        upstream=self.root/'upstream';upstream.mkdir();git(upstream,'init')
        (upstream/'.gitignore').write_text('**/target/\n')
        (upstream/'source.txt').write_text('synthetic upstream')
        git(upstream,'add','.');git(upstream,'-c','user.name=Test','-c','user.email=test@example.invalid','-c','commit.gpgsign=false','commit','-m','fixture')
        commit=git(upstream,'rev-parse','HEAD')
        parent=self.root/'parent';parent.mkdir();git(parent,'init')
        (parent/'marker').write_text('parent repository')
        git(parent,'add','marker');git(parent,'-c','user.name=Test','-c','user.email=test@example.invalid','-c','commit.gpgsign=false','commit','-m','parent')
        parent_head=git(parent,'rev-parse','HEAD')
        checkout=parent/'runtime/aichat-upstream';cache=checkout/'rust-search-ui/target/cache-marker';cache.parent.mkdir(parents=True);cache.write_text('keep cache')
        with patch.object(search_installer,'REPO',str(upstream)),patch.object(search_installer,'COMMIT',commit):
            search_installer.ensure_checkout(checkout)
            search_installer.ensure_checkout(checkout)
        self.assertEqual(git(checkout,'rev-parse','HEAD'),commit)
        self.assertEqual(git(checkout,'rev-parse','--show-toplevel'),str(checkout.resolve()))
        self.assertEqual(cache.read_text(),'keep cache')
        self.assertEqual(git(parent,'rev-parse','HEAD'),parent_head)

if __name__=='__main__':os.umask(0o077);unittest.main()
