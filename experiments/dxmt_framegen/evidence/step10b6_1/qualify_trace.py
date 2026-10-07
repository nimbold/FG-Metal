#!/usr/bin/env python3
"""Reject mixed-owner/epoch measurements; surface rows are never frame counts."""
import csv,json,sys
from collections import Counter
from pathlib import Path
p=Path(sys.argv[1]); r=list(csv.DictReader((p/'native.csv').open()))
a=json.loads((p/'display-analysis.json').read_text())['dxmt_present_call_alignment']
submitted=[(i,x) for i,x in enumerate(r) if x['event']=='presentation_stages' and int(x.get('present_call_ns','0') or 0)>0 and x['terminal_class'] in ('source_submitted','generated_submitted')]
match=submitted[a['dxmt_start_index']:a['dxmt_start_index']+a['matched_requests']]
lo,hi=match[0][0],match[-1][0]
window=r[lo:hi+1]; epochs=sorted({x['epoch'] for _,x in match})
transitions=[x['event'] for x in window if x['event'] in ('epoch_reset','layer_owner_claimed','layer_owner_released','layer_owner_draining','display_callbacks_stopped','session_active')]
claims=[(i,x) for i,x in enumerate(r[:lo]) if x['event']=='layer_owner_claimed']
proofs=[(i,x) for i,x in enumerate(r[:lo]) if x['event']=='experiment_two_fresh_sources']
active=[(i,x) for i,x in enumerate(r[:lo]) if x['event']=='session_active']
ordered=bool(claims and proofs and active and proofs[-1][0]<claims[-1][0]<active[-1][0]<lo and proofs[-1][1]['epoch']==claims[-1][1]['epoch']==active[-1][1]['epoch']==epochs[0])
app=list(csv.DictReader((p/'d3d11_clear_window_app.csv').open()));app=[x for x in app if x['event']=='present']
all_visible=all(x['window_visible']=='1' and x['window_iconic']=='0' and x['window_foreground']=='1' for x in app)
# Map QPC elapsed time to Mach using early reservation/app pairs before any
# fallback can change the source-id/app-Present-id correspondence. Require an
# additional one-second visibility margin on both sides of the trace window.
reserves=[x for x in r if x['event']=='source_candidate_reserved'][:20]
offsets=sorted(int(x['source_ns'])-int(app[int(x['source_id'])-1]['elapsed_us'])*1000 for x in reserves if 0<int(x['source_id'])<=len(app))
offset=offsets[len(offsets)//2]
start=min(int(x['present_call_ns']) for _,x in match);end=max(int(x['present_call_ns']) for _,x in match)
measured_app=[x for x in app if start-1_000_000_000<=int(x['elapsed_us'])*1000+offset<=end+1_000_000_000]
visible=bool(measured_app) and all(x['window_visible']=='1' and x['window_iconic']=='0' and x['window_foreground']=='1' for x in measured_app)
# Full-run safety ledger and measurement gate are independent; two ordinary
# priming terminals are not display-owner acceptance IDs.
summary=(p/'summary.txt').read_text()
required=['lost_source_ids=0','unsafe_terminal_ids=0','source_safety_violation_ids=0','source_regressions=0','malformed_requested_pairs=0','malformed_displayed_pairs=0','gpu_failed=0','callback_tick_ledger: status=PASS']
safety=all(k in summary for k in required) and 'DXMT_FRAMEGEN_OWNERSHIP_ASSERT' not in (p/'wine-console.log').read_text()
circuits=sorted({x['circuit_state'] for _,x in match})
f_policy=circuits==['0'] and a['matched_generated_submissions']>0
result={'qualified':f_policy and len(epochs)==1 and not transitions and ordered and visible and safety and a['status']=='ALIGNED' and a['missing_feedback_record_count']==0,
'f_policy_generation_present':f_policy,'circuit_states':circuits,'epochs':epochs,'transitions_in_aligned_measurement':transitions,'two_source_proof_claim_active_ordered':ordered,'measurement_app_presents_visible_foreground':visible,'visibility_checked_app_rows':len(measured_app),'visibility_margin_seconds':1,'all_app_presents_visible_foreground':all_visible,'full_run_safety_ledger_checked':safety,
'native_row_range':[lo,hi],'measurement_seconds':a['window_seconds'],'alignment_p95_ms':a['alignment_residual_p95_ns']/1e6,'alignment_max_ms':a['alignment_residual_max_ns']/1e6,'physical_scanout_verified':False}
(p/'qualification.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result,indent=2))
if not result['qualified']:raise SystemExit(1)
