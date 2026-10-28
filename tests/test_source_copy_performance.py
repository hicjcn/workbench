"""Content, isolation and race guards for reduced source-copy work."""
import errno
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from agent_workbench import workspace as w, multi_repo as multi


class SourceCopyTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve()

    def source(self, name='source'):
        source=self.root/name;source.mkdir(parents=True)
        w.git(source,'init','-b','main');w.git(source,'config','user.name','Fixture');w.git(source,'config','user.email','fixture@example.invalid')
        # Preserve fixture bytes before the first commit, including on hosts
        # whose global Git configuration normalizes CRLF to LF on git add.
        w.git(source,'config','core.autocrlf','false');w.git(source,'config','core.eol','lf')
        for name,content in [('same.txt','same'),('dirty.txt','aaaa'),('deleted.txt','delete'),('.harness','../opaque-missing-target\r\n'),('.env','PRIVATE-CONTENT')]:
            (source/name).write_bytes(content.encode())
        w.git(source,'add','--all');w.git(source,'-c','commit.gpgsign=false','commit','-m','fixture')
        return source

    def dirty_source(self):
        created=w.create_workspace(self.source(),self.root/'base','source','backend')
        source=Path(created['workspace'])/'repo'
        original=(source/'dirty.txt').stat()
        (source/'dirty.txt').write_text('bbbb')
        os.utime(source/'dirty.txt',ns=(original.st_atime_ns,original.st_mtime_ns))
        (source/'deleted.txt').unlink();(source/'new.bin').write_bytes(b'\x00\xffnew')
        return source

    def test_overlay_copies_only_changed_and_added_content_including_same_size_and_mtime(self):
        source=self.dirty_source();before=w.snapshot(source);copy2=shutil.copy2;copied=[]
        def copy(src,dst,*args,**kwargs):copied.append(Path(src).name);return copy2(src,dst,*args,**kwargs)
        with patch.object(shutil,'copy2',side_effect=copy):
            created=w.create_workspace(source,self.root/'attempts','readonly','backend',include_dirty=True,include_untracked=True)
        repo=Path(created['workspace'])/'repo'
        self.assertCountEqual(copied,['dirty.txt','new.bin'])
        self.assertEqual(w.snapshot(repo)['tree_digest'],before['tree_digest'])
        self.assertEqual(w.git(repo,'show','HEAD:dirty.txt'),b'aaaa')
        self.assertFalse((repo/'deleted.txt').exists());self.assertFalse((repo/'.env').exists())
        self.assertEqual((repo/'.harness').read_bytes(),b'../opaque-missing-target\r\n')
        (repo/'same.txt').write_text('independent')
        self.assertEqual((source/'same.txt').read_text(),'same');self.assertEqual(w.snapshot(source),before)

    def test_source_mutation_during_overlay_still_aborts_and_removes_partial_copy(self):
        source=self.dirty_source();copy2=shutil.copy2
        def copy(src,dst,*args,**kwargs):
            result=copy2(src,dst,*args,**kwargs);(source/'same.txt').write_text('changed during copy');return result
        with patch.object(shutil,'copy2',side_effect=copy):
            with self.assertRaisesRegex(w.WorkbenchError,'Source changed'):
                w.create_workspace(source,self.root/'attempts','readonly','backend',include_dirty=True,include_untracked=True)
        self.assertFalse((self.root/'attempts/readonly/backend').exists())
        self.assertEqual(list((self.root/'attempts/readonly').iterdir()),[])

    def multi_source(self):
        parent=self.root/'parent';parent.mkdir()
        for name in ('first','nested/second'):self.source('parent/'+name)
        created=w.create_workspace(parent,self.root/'base','source','backend',repository_paths=['first','nested/second'])
        return Path(created['workspace'])/'repo'

    def test_global_snapshot_still_detects_mutation_between_full_passes(self):
        source=self.multi_source();value=multi.layout(source);capture=multi.capture;calls=0
        def scan(*args):
            nonlocal calls
            result=capture(*args);calls+=1
            if calls==1:(source/'first/same.txt').write_text('mutated between passes')
            return result
        with patch.object(multi,'capture',side_effect=scan):
            with self.assertRaisesRegex(w.WorkbenchError,'快照期间变化'):w.snapshot(source)

    def test_global_snapshot_still_detects_head_changes_with_identical_content(self):
        source=self.multi_source();capture=multi.capture;calls=0
        def scan(*args):
            nonlocal calls
            result=capture(*args);calls+=1
            if calls==1:w.git(source/'first','-c','commit.gpgsign=false','commit','--allow-empty','-m','same contents new head')
            return result
        with patch.object(multi,'capture',side_effect=scan):
            with self.assertRaisesRegex(w.WorkbenchError,'快照期间变化'):w.snapshot(source)

    def test_cross_drive_copy_corruption_is_detected_before_publishing_workspace(self):
        source=self.multi_source();rename=Path.rename;copytree=shutil.copytree
        def move(path,target):
            if path.name=='repo':raise OSError(errno.EXDEV,'controlled cross-drive move')
            return rename(path,target)
        def copy(src,dst,*args,**kwargs):
            result=copytree(src,dst,*args,**kwargs)
            if Path(dst).name in ('first','second'):(Path(dst)/'same.txt').write_text('copy corruption')
            return result
        with patch.object(Path,'rename',autospec=True,side_effect=move),patch.object(shutil,'copytree',side_effect=copy):
            with self.assertRaisesRegex(w.WorkbenchError,'副本与已核实的源码不一致'):
                w.create_workspace(source,self.root/'attempts','readonly','backend',include_dirty=True,include_untracked=True)
        self.assertFalse((self.root/'attempts/readonly/backend').exists())
        self.assertEqual((source/'first/same.txt').read_text(),'same')

    def test_multi_repository_aggregate_size_limit_is_still_enforced(self):
        parent=self.root/'parent';parent.mkdir()
        for name in ('first','second'):self.source('parent/'+name)
        with patch.object(multi,'LIMIT',4):
            with self.assertRaisesRegex(w.WorkbenchError,'100 MiB'):
                w.create_workspace(parent,self.root/'attempts','readonly','backend',repository_paths=['first','second'])
        self.assertFalse((self.root/'attempts/readonly/backend').exists())

    def test_windows_executable_modes_are_batched_with_nul_delimited_unicode_paths(self):
        source=self.source();names=['tools/run space 中文 '+str(index)+'.sh' for index in range(30)]+['-leading.sh']
        for name in names:
            path=source/name;path.parent.mkdir(exist_ok=True);path.write_text('#!/bin/sh\nexit 0\n')
        w.git(source,'add','--all');w.index_executable(source,names)
        w.git(source,'-c','commit.gpgsign=false','commit','-m','executable fixtures')
        original=w.git
        with patch.object(w,'IS_WINDOWS',True),patch.object(w,'git',wraps=original) as calls:
            created=w.create_workspace(source,self.root/'attempts','readonly','backend')
        updates=[call for call in calls.call_args_list if len(call.args)>1 and call.args[1]=='update-index']
        self.assertEqual(len(updates),1)
        self.assertEqual(updates[0].kwargs['input_bytes'],b''.join(name.encode()+b'\0' for name in sorted(names)))
        modes=w.git(Path(created['workspace'])/'repo','ls-files','--stage','-z').decode().split('\0')
        self.assertEqual(sum(line.startswith('100755 ') for line in modes),len(names))

    def test_windows_dirty_index_mode_change_is_preserved_in_readonly_copy(self):
        source=self.source();w.index_executable(source,['same.txt'])
        with patch.object(w,'IS_WINDOWS',True):
            before=w.snapshot(source)
            created=w.create_workspace(source,self.root/'attempts','readonly','backend',include_dirty=True,include_untracked=True)
            self.assertEqual(w.snapshot(Path(created['workspace'])/'repo')['tree_digest'],before['tree_digest'])
        self.assertIn(b'100755',w.git(Path(created['workspace'])/'repo','ls-files','--stage','--','same.txt'))
