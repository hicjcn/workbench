"""Compare independent dirty workspace copies; synthetic data, no Docker or models."""
import argparse
import json
import os
from pathlib import Path
import re
import statistics
import subprocess
import sys
import time
from unittest.mock import patch


def worker(args):
    if args.code_root: sys.path.insert(0, str(args.code_root))
    from agent_workbench import workspace as w, multi_repo as multi
    root=args.output;root.mkdir(parents=True)
    source=root/'source';source.mkdir()
    names=[f'services/service-{index}' for index in range(args.repositories)]
    for name in names:
        repo=source/name;repo.mkdir(parents=True)
        w.git(repo,'init','-b','main');w.git(repo,'config','user.name','Fixture');w.git(repo,'config','user.email','fixture@example.invalid')
        for index in range(args.files):
            target=repo/f'src/group-{index % 20}/file-{index:05d}.txt';target.parent.mkdir(parents=True,exist_ok=True)
            target.write_text('synthetic independent source\n'*4)
        (repo/'tool.sh').write_text('#!/bin/sh\nexit 0\n');(repo/'tool.sh').chmod(0o755)
        (repo/'.harness').write_bytes(b'../opaque-missing-config\r\n')
        (repo/'.env').write_text('PRIVATE_FIXTURE=never_export\n')
        w.git(repo,'add','--all');w.git(repo,'-c','commit.gpgsign=false','commit','-m','synthetic source')
    (source/'AGENTS.md').write_text('synthetic common instructions\n')
    prepared=w.create_workspace(source,root/'workspaces','req-source','backend',repository_paths=names,shared_files=['AGENTS.md'])
    repo=Path(prepared['workspace'])/'repo'
    for name in names:
        (repo/name/'src/group-0/file-00000.txt').write_text('requirement dirty change\n')
        (repo/name/'src/group-1/file-00001.txt').unlink()
        (repo/name/'new.bin').write_bytes(b'\x00\xffdirty untracked\n')
    before=w.snapshot(repo)
    counters={'inventory_calls':0,'inventory_file_reads':0,'copy2_calls':0,'git_calls':0}
    inventory,copy2,git=w.inventory,w.shutil.copy2,w.git
    def counted_inventory(*argv,**kwargs):
        result=inventory(*argv,**kwargs);counters['inventory_calls']+=1;counters['inventory_file_reads']+=len(result['files']);return result
    def counted_copy(*argv,**kwargs):counters['copy2_calls']+=1;return copy2(*argv,**kwargs)
    def counted_git(*argv,**kwargs):counters['git_calls']+=1;return git(*argv,**kwargs)
    started=time.perf_counter()
    with patch.object(w,'inventory',side_effect=counted_inventory),patch.object(w.shutil,'copy2',side_effect=counted_copy),\
            patch.object(w,'git',side_effect=counted_git),patch.object(multi,'git',side_effect=counted_git):
        copied=w.create_workspace(repo,root/'attempts','req-readonly','backend',include_dirty=True,include_untracked=True)
    elapsed=time.perf_counter()-started
    target=Path(copied['workspace'])/'repo'
    assert w.snapshot(target)['tree_digest']==before['tree_digest']
    assert w.snapshot(repo)==before
    for name in names:
        assert w.git(target/name,'remote').strip()==b''
        assert not (target/name/'.env').exists()
        assert (target/name/'.harness').read_bytes()==b'../opaque-missing-config\r\n'
        assert (target/name/'new.bin').read_bytes()==b'\x00\xffdirty untracked\n'
    print(json.dumps({'seconds':elapsed,**counters,'snapshot_matches':True,'source_unchanged':True}))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--baseline-ref',help='Full 40-character commit SHA from this repository')
    parser.add_argument('--repositories',type=int,default=3)
    parser.add_argument('--files',type=int,default=500)
    parser.add_argument('--repeat',type=int,default=2)
    parser.add_argument('--worker',action='store_true',help=argparse.SUPPRESS)
    parser.add_argument('--code-root',type=Path,help=argparse.SUPPRESS)
    args=parser.parse_args();args.output=args.output.resolve()
    if not 1<=args.repositories<=16 or not 2<=args.files<=10000 or not 1<=args.repeat<=5:parser.error('Invalid benchmark size')
    if args.worker:return worker(args)
    if args.output.exists():parser.error('Use a new output directory')
    args.output.mkdir(parents=True)
    repo=Path(__file__).resolve().parents[1]
    variants={'current':repo/'src'}
    if args.baseline_ref:
        if not re.fullmatch('[a-f0-9]{40}',args.baseline_ref):parser.error('baseline-ref must be a full commit SHA')
        package=args.output/'baseline-code/agent_workbench';package.mkdir(parents=True)
        (package/'__init__.py').write_text('')
        for name in ('workspace','multi_repo','source_export','platform_support','logging_support'):
            content=subprocess.check_output(['git','show',args.baseline_ref+':src/agent_workbench/'+name+'.py'],cwd=repo)
            (package/(name+'.py')).write_bytes(content)
        variants={'baseline':package.parent,**variants}
    result={'repositories':args.repositories,'files_per_repository':args.files,'model_inference':False,
        'baseline_ref':args.baseline_ref,'platform':sys.platform,'actual_windows_host':os.name=='nt','variants':{}}
    for name,code in variants.items():
        samples=[]
        for index in range(args.repeat):
            command=[sys.executable,str(Path(__file__).resolve()),'--worker','--output',str(args.output/f'{name}-{index}'),
                '--code-root',str(code),'--repositories',str(args.repositories),'--files',str(args.files)]
            completed=subprocess.run(command,cwd=repo,capture_output=True,text=True,encoding='utf-8',timeout=600,env={**os.environ,'PYTHONUTF8':'1'})
            if completed.returncode:raise RuntimeError('Benchmark worker failed: '+completed.stderr[-4000:])
            samples.append(json.loads(completed.stdout))
        result['variants'][name]={'samples':samples,'median_seconds':statistics.median(item['seconds'] for item in samples)}
    (args.output/'report.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))


if __name__=='__main__':main()
