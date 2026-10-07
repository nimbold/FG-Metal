#!/usr/bin/env python3
"""Assert recorded visual/API regression gates without inspecting image pixels."""
from pathlib import Path
import json,hashlib
root=Path(__file__).resolve().parent
summary=json.loads((root/'all-run-summary.json').read_text());fixed=json.loads((root/'final-build-identity.json').read_text())
required=['final-original-30s','final-original-60s','final-bgra8-60s','final-repository-smoke','final-lifecycle-65s','final-waitable-30s']
for name in required:
 r=summary[name]
 assert r['exit_status']==0, (name,r['exit_status'])
 assert r['hashes']['d3d11.dll']==fixed['d3d11_sha256'] and r['hashes']['dxgi.dll']==fixed['dxgi_sha256']
 assert not any(k.startswith('FG_STEP11B1') for k in r['environment']), name
 assert not r['renderer_error_lines'], (name,r['renderer_error_lines'])
 assert r['visual'] in ['VISIBLE CHANGING COLOR THROUGH SHUTDOWN','ALL LIFECYCLE TRANSITIONS VISIBLY HEALTHY'], (name,r['visual'])
 if 'repository' not in name:
  assert len(r['color_ids'])==4
  assert set(r['present_hresult_counts']) <= {'0x00000000','0x087a0001'}
for name,minimum in [('final-original-30s',30),('final-original-60s',60),('final-bgra8-60s',60),('final-lifecycle-65s',65),('final-waitable-30s',30)]:assert summary[name]['wall_seconds']>=minimum
assert summary['final-original-30s']['hashes']['d3d11_clear_window.exe']=='18509edad2e1ae8d49941bd1868dd384d03cf693cc66b5542e7bba1611e21163'
assert summary['final-original-60s']['hashes']['d3d11_clear_window.exe']==summary['final-original-30s']['hashes']['d3d11_clear_window.exe']
events={x[0]:x for x in summary['final-lifecycle-65s']['lifecycle_rows'][1:]}
for name in ['resize_complete','focus_loss','focus_recovery','fullscreen_enter','fullscreen_exit','swapchain_recreation']:
 assert name in events,name
 assert events[name][-1]=='0x00000000',(name,events[name])
sem=summary['final-waitable-30s']['semantics'];assert sem['frames']>=850 and sem['all_last_present_counts_equal_app_sequence']
assert set(sem['count_hresult_counts'])=={'00000000'} and set(sem['device_removed_hresult_counts'])=={'00000000'}
assert set(sem['wait_result_counts'])<={'0','258'}
assert len(sem['COM_buffer_identities'])==1 and sem['indices']==['0']
setup=sem['setup'][0];assert setup[2]=='00000000' and int(setup[3],16)!=0 and setup[4:]==['00000000','00000000','1']
assert set(sem['stats_hresult_counts'])<={'00000000','887a000b'}
assert hashlib.file_digest((root/'fixed-d3d11.dll').open('rb'),'sha256').hexdigest()==fixed['d3d11_sha256']
result={'status':'PASS','visual_authority':'direct user observations','normal_renderer_diagnostic_modes':'removed','validated_runs':required,'scope':'local D3D11 clear-only baseline; no image capture or pixel readback; no internal presents'}
(root/'final-validation.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result,indent=2))
