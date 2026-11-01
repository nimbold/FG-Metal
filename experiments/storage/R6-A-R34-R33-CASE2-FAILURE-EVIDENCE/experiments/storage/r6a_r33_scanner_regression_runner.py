#!/usr/bin/env python3
"""Fail-fast disposable-root regressions for the R33 structured lsof scanner."""
import base64, hashlib, importlib.util, json, os, select, stat, subprocess, sys, tempfile, time
from pathlib import Path

HERE=Path(__file__).resolve().parent
SCANNER_PATH=HERE/'r6a_r33_live_open_capture.py'
JSON_OUT=HERE/'R6-A-R33-STRUCTURED-LSOF-DISPOSABLE-REGRESSIONS.json'
MD_OUT=HERE/'R6-A-R33-STRUCTURED-LSOF-DISPOSABLE-REGRESSIONS.md'
spec=importlib.util.spec_from_file_location('r33', SCANNER_PATH)
scanner=importlib.util.module_from_spec(spec); spec.loader.exec_module(scanner)
report={'schema_version':1,'scanner':{'path':str(SCANNER_PATH),'sha256':hashlib.sha256(SCANNER_PATH.read_bytes()).hexdigest(),'argv_field':scanner.LSOF_ARG},'started_at_unix':time.time(),'cases':[],'cleanup':[],'first_failure':None}

def capview(c):
    raw=c.get('stdout_raw',b''); err=c.get('stderr_raw',b'')
    return {'argv':c.get('argv'),'child_pid':c.get('child_pid'),'child_ppid':c.get('child_ppid'),'child_identity':c.get('child_identity'),'child_identity_error':c.get('child_identity_error'),'returncode':c.get('returncode'),'stdout_bytes':c.get('stdout_total_bytes'),'stdout_captured_bytes':c.get('stdout_bytes_captured'),'stdout_sha256':c.get('stdout_sha256'),'stdout_captured_sha256':c.get('stdout_captured_sha256'),'stdout_base64':c.get('stdout_base64'),'stdout_repr':repr(raw),'stderr_bytes':c.get('stderr_total_bytes'),'stderr_sha256':c.get('stderr_sha256'),'stderr_base64':c.get('stderr_base64'),'stderr_repr':repr(err),'timed_out':c.get('timed_out'),'stdout_capped':c.get('stdout_capped'),'stderr_capped':c.get('stderr_capped'),'scan_complete':c.get('scan_complete'),'stream':c.get('stream'),'rows':[{'pid':r.get('pid'),'ppid':r.get('ppid'),'fd_raw':repr(r.get('fd_raw')),'fd_number':r.get('fd_number'),'type_raw':repr(r.get('fd_type_raw')),'device':r.get('device'),'inode':r.get('inode'),'name_raw':repr(r.get('name_raw')),'name_base64':base64.b64encode(r.get('name_raw',b'')).decode(),'parser_status':r.get('parser_status')} for r in c.get('rows',[])]}

def add(name,expected,observed,ok,evidence=None):
    x={'case':name,'expected':expected,'observed':observed,'result':'PASS' if ok else 'FAIL'}
    if evidence is not None: x['evidence']=evidence
    report['cases'].append(x)
    if not ok and report['first_failure'] is None: report['first_failure']={'case':name,'expected':expected,'observed':observed}
    return ok

def scan(root): return scanner.scan_lsof_path(os.path.realpath(root))
def opendir(path): return os.open(path,os.O_RDONLY|getattr(os,'O_DIRECTORY',0))
def pinrows(c,fd,root):
    name=os.fsencode(os.path.realpath(root))
    return [r for r in c['rows'] if r['pid']==os.getpid() and r['fd_number']==fd and r['fd_raw']==str(fd).encode() and r['fd_type_raw']==b'DIR' and r['name_raw']==name]
def runscan_case(name,root,check):
    try:
        c=scan(root); ok,obs=check(c)
        return add(name,obs['expected'],obs['observed'],ok,{'capture':capview(c),**obs.get('extra',{})})
    except Exception as e:
        return add(name,'scanner operation completes under stated contract',f'{type(e).__name__}: {e}',False)

def helper(mode,root,child=None):
    code=("import os,sys; m=sys.argv[1]; r=sys.argv[2]; c=sys.argv[3]; "
          "fd=os.open(r,os.O_RDONLY|getattr(os,'O_DIRECTORY',0)) if m=='root' else (os.open(c,os.O_RDONLY) if m=='child' else None); "
          "print('READY',flush=True); sys.stdin.readline(); os.close(fd) if fd is not None else None")
    p=subprocess.Popen([sys.executable,'-c',code,mode,root,child or ''],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,cwd=root if mode=='cwd' else None)
    ready,_,_=select.select([p.stdout],[],[],5)
    if not ready or p.stdout.readline().strip()!='READY': raise RuntimeError('foreign helper readiness failed')
    return p
def stop(p):
    if p.poll() is None:
        try: p.stdin.write('\n');p.stdin.flush();p.wait(timeout=5)
        except Exception: p.kill();p.wait(timeout=5)
    report['cleanup'].append({'pid':p.pid,'returncode':p.returncode})

def write_report():
    report['finished_at_unix']=time.time()
    report['result']='PASS' if report['first_failure'] is None else 'FAIL'
    with JSON_OUT.open('xb') as f: f.write((json.dumps(report,indent=2,sort_keys=True,ensure_ascii=True)+'\n').encode());f.flush();os.fsync(f.fileno())
    lines=['# R33 Disposable Structured lsof Scanner Regressions','',f"Result: {report['result']}",f"Scanner SHA-256: {report['scanner']['sha256']}",f"First failure: {report['first_failure']}",'','| Case | Result | Expected | Observed |','|---|---|---|---|']
    for x in report['cases']: lines.append(f"| {x['case']} | {x['result']} | {x['expected']} | {x['observed']} |")
    lines+=['','Full argv, raw bytes/base64, hashes, child identities, parsed rows, and cleanup are in the JSON transcript.','']
    with MD_OUT.open('xb') as f: f.write('\n'.join(lines).encode());f.flush();os.fsync(f.fileno())

def main():
    try:
        with tempfile.TemporaryDirectory(prefix='r33-scanner-regressions-') as temp:
            roots={i:str(Path(temp)/f'case_{i}') for i in range(1,17)}
            for p in roots.values(): os.mkdir(p)
            # 1 root pin only
            fd=opendir(roots[1])
            try:
                c=scan(roots[1]); m=pinrows(c,fd,roots[1]); ok=len(c['rows'])==1 and len(m)==1 and c['returncode']==0 and c['stderr_total_bytes']==0 and c['scan_complete'] is True
                if not add('1_root_pin_only','exactly one pin row and complete rc0 scan',{'rows':len(c['rows']),'pin_matches':len(m),'rc':c['returncode'],'complete':c['scan_complete']},ok,{'capture':capview(c),'pin_fd':fd}): return 1
            finally: os.close(fd)
            # 2 same checker pin + child
            child2=str(Path(roots[2])/'child.bin');Path(child2).write_bytes(b'x');rf=opendir(roots[2]);cf=os.open(child2,os.O_RDONLY)
            try:
                c=scan(roots[2]);m=pinrows(c,rf,roots[2]);cm=[r for r in c['rows'] if r['pid']==os.getpid() and r['fd_number']==cf and r['name_raw']==os.fsencode(child2)]
                ok=len(c['rows'])==2 and len(m)==1 and len(cm)==1 and c['returncode']==0 and c['stderr_total_bytes']==0 and c['scan_complete'] is True
                if not add('2_checker_pin_plus_child_fd','both rows visible and complete',{'rows':len(c['rows']),'pin_matches':len(m),'child_matches':len(cm),'rc':c['returncode']},ok,{'capture':capview(c),'pin_fd':rf,'child_fd':cf}): return 1
            finally: os.close(cf);os.close(rf)
            # 3 cwd under root
            cwd=os.getcwd();rf=opendir(roots[3])
            try:
                os.chdir(roots[3]);c=scan(roots[3]); same=[r for r in c['rows'] if r['pid']==os.getpid()]; cr=[r for r in same if r['fd_raw']==b'cwd' and r['name_raw']==os.fsencode(os.path.realpath(roots[3]))];pm=pinrows(c,rf,roots[3])
                ok=len(same)==2 and len(cr)==1 and len(pm)==1 and c['returncode']==0 and c['stderr_total_bytes']==0 and c['scan_complete'] is True
                if not add('3_checker_cwd_under_root','cwd and pin rows visible',{'same_pid_rows':len(same),'cwd_rows':len(cr),'pin_rows':len(pm),'rc':c['returncode']},ok,{'capture':capview(c),'pin_fd':rf}): return 1
            finally: os.chdir(cwd);os.close(rf)
            # 4-6 foreign holders
            for n,mode in ((4,'root'),(5,'child'),(6,'cwd')):
                child=str(Path(roots[n])/'foreign-child.bin') if mode=='child' else None
                if child: Path(child).write_bytes(b'x')
                p=helper(mode,roots[n],child)
                try:
                    c=scan(roots[n]);rr=[r for r in c['rows'] if r['pid']==p.pid]
                    expected_name=os.fsencode(child if mode=='child' else os.path.realpath(roots[n]))
                    ok=len(rr)==1 and rr[0]['name_raw']==expected_name and (rr[0]['fd_raw']==b'cwd' if mode=='cwd' else rr[0]['fd_number'] is not None) and c['returncode']==0 and c['stderr_total_bytes']==0 and c['scan_complete'] is True
                    if not add(f'{n}_foreign_{mode}_holder',f'foreign {mode} row visible',{'foreign_pid':p.pid,'rows':len(rr),'rc':c['returncode'],'complete':c['scan_complete']},ok,{'capture':capview(c)}): return 1
                finally: stop(p)
            # 7 no holders: row set empty, but lsof's rc1 must still block absence conclusion.
            c=scan(roots[7]);ok=not c['rows'] and c['stream'].get('complete') is True and c['returncode']==1 and c['scan_complete'] is False
            if not add('7_no_holders','zero rows; rc1 remains fail-closed',{'rows':len(c['rows']),'stream_complete':c['stream'].get('complete'),'rc':c['returncode'],'scan_complete':c['scan_complete']},ok,{'capture':capview(c)}): return 1
            # 8 newline-containing filename
            child8=str(Path(roots[8])/'child\nline.bin');Path(child8).write_bytes(b'x');rf=opendir(roots[8]);cf=os.open(child8,os.O_RDONLY)
            try:
                c=scan(roots[8]);rr=[r for r in c['rows'] if r['fd_number']==cf]
                ok=len(rr)==1 and rr[0]['name_raw']==os.fsencode(child8) and b'\n' in rr[0]['name_raw'] and c['returncode']==0 and c['stderr_total_bytes']==0 and c['scan_complete'] is True
                if not add('8_newline_child_filename','LF retained inside n field payload',{'matches':len(rr),'newline_preserved':bool(rr and b'\n' in rr[0]['name_raw']),'rc':c['returncode']},ok,{'capture':capview(c),'pin_fd':rf,'child_fd':cf}): return 1
            finally: os.close(cf);os.close(rf)
            valid=b'p123\0R1\0cPython\0\nf3\0tDIR\0D0x100000d\0i7\0n/tmp/root\0\n'
            invalid=[
              ('9_malformed_synthetic_stream',b'x123\0R1\0cPython\0\nf3\0tDIR\0D0x100000d\0i7\0n/tmp/root\0\n'),
              ('10_truncated_nul_stream',b'p123\0R1\0cPython\0\nf3\0tDIR\0D0x100000d\0i7\0n/tmp/root'),
              ('11_truncated_set_boundary',valid[:-1]),
              ('12_missing_n_field',b'p123\0R1\0cPython\0\nf3\0tDIR\0D0x100000d\0i7\0\n'),
              ('13_wrong_numeric_field',b'p123\0R1\0cPython\0\nf3\0tDIR\0D0x100000d\0ibad\0n/tmp/root\0\n'),
              ('14_unexpected_field_set_order',b'f3\0tDIR\0D0x100000d\0i7\0n/tmp/root\0\n'),
            ]
            for name,raw in invalid:
                try:
                    rows,stream=scanner.parse_lsof(raw); rejected=False;obs={'accepted':True,'rows':len(rows),'stream':stream}
                except scanner.LsofParseError as e: rejected=True;obs={'accepted':False,'error':str(e),'prefix_rows':len(e.rows)}
                ok=rejected
                if not add(name,'strict parser rejects invalid stream',obs,ok,{'input_bytes':len(raw),'input_sha256':hashlib.sha256(raw).hexdigest(),'input_base64':base64.b64encode(raw).decode(),'input_repr':repr(raw)}): return 1
            # Status gate: empty valid grammar cannot prove absence unless rc0 + empty stderr.
            rows,stream=scanner.parse_lsof(valid)
            if not add('14b_status_gate','complete scan requires rc0 and empty stderr',{'valid_fixture_rows':len(rows),'fixture_complete':stream['complete'],'scan_lsof_path_gate':'rc==0 and stderr_bytes==0'},stream['complete'] and len(rows)==1,{'fixture_sha256':hashlib.sha256(valid).hexdigest()}): return 1
            # 15 lsof child is not a false holder.
            rf=opendir(roots[15])
            try:
                c=scan(roots[15]); lr=[r for r in c['rows'] if r['pid']==c['child_pid']]
                ok=not lr and c['returncode']==0 and c['scan_complete'] is True
                if not add('15_scanner_child_not_false_holder','lsof child PID absent from rows',{'lsof_child_pid':c['child_pid'],'child_rows':len(lr),'rc':c['returncode']},ok,{'capture':capview(c),'pin_fd':rf}): return 1
            finally: os.close(rf)
            # 16 root identity stable around scan.
            rf=opendir(roots[16]); n0=os.stat(roots[16],follow_symlinks=False); f0=os.fstat(rf)
            before=(n0.st_dev,n0.st_ino,stat.S_IFMT(n0.st_mode),f0.st_dev,f0.st_ino,stat.S_IFMT(f0.st_mode))
            try:
                c=scan(roots[16]);n1=os.stat(roots[16],follow_symlinks=False);f1=os.fstat(rf)
                after=(n1.st_dev,n1.st_ino,stat.S_IFMT(n1.st_mode),f1.st_dev,f1.st_ino,stat.S_IFMT(f1.st_mode))
                ok=before==after and c['returncode']==0 and c['stderr_total_bytes']==0 and c['scan_complete'] is True
                if not add('16_root_identity_stable','name and pin identity unchanged before/after',{'before':before,'after':after,'stable':before==after,'rc':c['returncode']},ok,{'capture':capview(c),'pin_fd':rf}): return 1
            finally: os.close(rf)
    except Exception as e:
        if report['first_failure'] is None: report['first_failure']={'case':'runner','expected':'safe regression completion','observed':f'{type(e).__name__}: {e}'}
    finally:
        try: write_report()
        except Exception as e:
            print(f'report_write_error={type(e).__name__}: {e}',file=sys.stderr)
            return 2
    print(report['result'])
    if report['first_failure']: print('first_failure='+json.dumps(report['first_failure'],sort_keys=True))
    return 0 if report['first_failure'] is None else 1

if __name__=='__main__': raise SystemExit(main())
