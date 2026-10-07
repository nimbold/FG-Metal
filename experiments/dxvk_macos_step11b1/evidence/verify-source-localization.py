#!/usr/bin/env python3
"""Validate GPU-command metadata evidence; never inspect image content."""
from pathlib import Path
import json,re
root=Path(__file__).resolve().parent
before=json.loads((root/'source-before/result.json').read_text());after=json.loads((root/'source-after/result.json').read_text())
assert before['hashes']==after['hashes'], 'single-variable probe must use identical executable and DLLs'
a=dict(before['environment']);b=dict(after['environment'])
for d in [a,b]:
 for k in ['WINEPREFIX','DXVK_LOG_PATH']:d.pop(k)
assert b.pop('FG_STEP11B1_FLUSH_SOURCE')=='1'
assert a==b, 'probe changed an unexpected runtime setting'
result={}
for name,expected in [('source-before',1),('source-after',0)]:
 lines=(root/name/'runtime.log').read_text(errors='replace').splitlines()
 samples=[re.search(r'PRESENT_SOURCE.*image=(\S+) image_object=(\S+) storage=(\S+)',s) for s in lines if 'PRESENT_SOURCE' in s]
 clears=[re.search(r'PENDING_CLEAR image=(\S+) image_object=(\S+) storage=(\S+)',s) for s in lines if 'PENDING_CLEAR' in s]
 assert len(samples)==len(clears)==16
 assert all(x.groups()==y.groups() for x,y in zip(samples,clears)), 'cleared image/storage does not match sampled source'
 pending=[int(re.search(r'pending_clears=(\d+)',s)[1]) for s in lines if 'EXTERNAL_READY' in s]
 assert len(pending)==16 and set(pending)=={expected}
 result[name]={'same_source_image_object_storage':True,'traced_frames':len(samples),'pending_clears_at_external_ready':expected,'clean_exit':before['exit_status']==0 if name=='source-before' else after['exit_status']==0}
observations=json.loads((root/'visual-observations.json').read_text())
assert observations['source-before']['visual']=='BLACK'
assert observations['source-after']['visual']=='VISIBLE CHANGING COLOR'
result['visual_authority']='direct user observation; metadata is not visible-rendering proof'
(root/'source-localization-check.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result,indent=2))
