#!/usr/bin/env python3
from pathlib import Path
import json,csv,re,collections
root=Path(__file__).resolve().parent; observations=json.loads((root/'visual-observations.json').read_text());summary={}
for out in sorted(root.iterdir()):
 if out.name=='black-baseline':continue # preserved historical evidence remains byte-identical
 if not out.is_dir() or not (out/'result.json').exists():continue
 r=json.loads((out/'result.json').read_text());r['visual']=observations.get(out.name,{}).get('visual','VISUAL CHECK REQUIRED');(out/'result.json').write_text(json.dumps(r,indent=2)+'\n')
 log=(out/'runtime.log').read_text(errors='replace')
 r['renderer_error_lines']=[s for s in log.splitlines() if re.search(r'shader.*fail|device.*lost|DXVK.*error|CS.*exception|VK_ERROR|DrawIndex is not supported',s,re.I)]
 r['swapchain_lines']=[s for s in log.splitlines() if re.search('Format:|Color space:|Present mode:|Buffer count:|Created .*swapchain|MoltenVK version',s)]
 app=out/'d3d11_clear_window_app.csv'
 if app.exists():
  rows=list(csv.reader(app.open()));prs=[x for x in rows if x and x[0]=='present'];r['application_presents']=len(prs);r['present_hresult_counts']=dict(collections.Counter(x[3] for x in prs));r['color_ids']=sorted(set(x[rows[0].index('color_id')] for x in prs));r['last_app_row']=rows[-1]
 lifecycle=out/'d3d11_clear_window_lifecycle.csv'
 if lifecycle.exists():r['lifecycle_rows']=list(csv.reader(lifecycle.open()))
 semantics=out/'semantics.csv'
 if semantics.exists():
  rows=list(csv.reader(semantics.open()));frames=[x for x in rows if x and x[0]=='observe'];setup=[x for x in rows if x and x[0]=='setup']
  r['semantics']={'setup':setup,'frames':len(frames),'all_last_present_counts_equal_app_sequence':all(int(x[1])==int(x[3]) for x in frames),'count_hresult_counts':dict(collections.Counter(x[2] for x in frames)),'stats_hresult_counts':dict(collections.Counter(x[4] for x in frames)),'wait_result_counts':dict(collections.Counter(x[9] for x in frames)),'indices':sorted(set(x[10] for x in frames)),'COM_buffer_identities':sorted(set(x[11] for x in frames)),'device_removed_hresult_counts':dict(collections.Counter(x[12] for x in frames)),'last_frame':frames[-1] if frames else None}
 summary[out.name]=r
(root/'all-run-summary.json').write_text(json.dumps(summary,indent=2)+'\n')
print(json.dumps({k:{q:v.get(q) for q in ['visual','exit_status','application_presents','present_hresult_counts']} for k,v in summary.items()},indent=2))
