#!/usr/bin/env python3
"""Summarize calibrated evidence; rates never turn Display rows into FPS."""
import csv,json,re,sys
from collections import Counter
from pathlib import Path
root=Path(__file__).resolve().parent
results={}
for name in sys.argv[1:]:
 p=root/name
 analysis=json.loads((p/'display-analysis.json').read_text());a=analysis['dxmt_present_call_alignment'];d=analysis['display_surface_intervals'];q=json.loads((p/'qualification.json').read_text())
 rows=list(csv.DictReader((p/'native.csv').open()));app=[x for x in csv.DictReader((p/'d3d11_clear_window_app.csv').open()) if x['event']=='present']
 n=lambda x,k:int(x.get(k,'0') or 0)
 stages=[x for x in rows if x['event']=='presentation_stages' and n(x,'present_call_ns')>0 and x['terminal_class'] in ('source_submitted','generated_submitted')]
 matched=stages[a['dxmt_start_index']:a['dxmt_start_index']+a['matched_requests']]
 start=min(n(x,'present_call_ns') for x in matched);end=max(n(x,'present_call_ns') for x in matched)
 feedback={n(x,'present_call_ns'):x for x in rows if x['event']=='presentation_feedback_result'}
 mf=[feedback[n(x,'present_call_ns')] for x in matched]
 source_times={}
 for x in rows:
  if x['event']=='presentation_feedback_result' and x['kind']=='1' and n(x,'actual_presented_ns')>0:
   source_times.setdefault(x['source_id'],[]).append(n(x,'actual_presented_ns'))
 g=[x for x in mf if x['kind']=='2'];good=both=missing_a=missing_b=aftertarget=0
 for x in g:
  actual=n(x,'actual_presented_ns')
  if actual<=0:continue
  at=source_times.get(x['pair_a_id'],[]);bt=source_times.get(x['pair_b_id'],[])
  if not at:missing_a+=1
  if not bt:missing_b+=1
  if at and bt:
   both+=1;good+=any(a0<actual<b0 for a0 in at for b0 in bt)
  aftertarget+=n(x,'source_b_scheduled_ns')>0 and actual>n(x,'source_b_scheduled_ns')
 requested=[x for x in rows if x['event']=='generated_requested'];ready=[x for x in rows if x['event']=='generated_ready']
 events=Counter(x['event'] for x in rows)
 # Submission window in Mach time; original harness QPC has a different origin.
 # Align source reservation timestamps with logged elapsed_us by median offset.
 reserves=[x for x in rows if x['event']=='source_candidate_reserved']
 offsets=[n(x,'source_ns')-n(app[int(x['source_id'])-1],'elapsed_us')*1000 for x in reserves[:20] if 0<int(x['source_id'])<=len(app)]
 offsets.sort();offset=offsets[len(offsets)//2]
 trace_app=[x for x in app if start <= n(x,'elapsed_us')*1000+offset <= end]
 summary=(p/'summary.txt').read_text()
 results[name]={'qualification':q,'window_seconds':a['window_seconds'],
  'app_presents_in_aligned_window':len(trace_app),'app_present_rate_hz':len(trace_app)/a['window_seconds'],
  'app_qpc_to_mach_median_offset_ns':offset,
  'callbacks_per_second':a['callback_rate_hz_in_request_window'],
  'submissions':a['matched_requests'],'submissions_per_second':a['matched_requests']/a['window_seconds'],
  'gpu_completed':a['gpu_status'].get('completed',0),'gpu_completions_per_second':a['gpu_status'].get('completed',0)/a['window_seconds'],
  'positive_presentedTime':a['positive_presented_time_count'],'positive_per_second':a['positive_presented_time_count']/a['window_seconds'],
  'zero_presentedTime':a['zero_presented_time_count'],'zero_fraction':a['zero_presented_time_count']/a['matched_requests'],
  'display':d,'vsync':analysis['vsync_by_surface_display_in_surface_window'],
  'aligned_G':{'submitted':len(g),'positive':sum(n(x,'actual_presented_ns')>0 for x in g),'zero':sum(n(x,'actual_presented_ns')==0 for x in g),
   'malformed_pair_ids':sum(n(x,'pair_b_id')!=n(x,'pair_a_id')+1 for x in g),
   'positive_both_endpoints':both,'positive_A_lt_G_lt_B':good,'missing_A_feedback':missing_a,'missing_B_feedback':missing_b,'G_positive_after_B_scheduled_target':aftertarget},
  'full_run_G':{**{k:events[k] for k in ('generated_requested','generated_ready','generated_selected','generated_presentation_submitted')},'positive':sum(x['event']=='presentation_feedback_result' and x['kind']=='2' and n(x,'actual_presented_ns')>0 for x in rows),'zero':sum(x['event']=='presentation_feedback_result' and x['kind']=='2' and n(x,'actual_presented_ns')==0 for x in rows)},
  'full_run_chronology_summary':[line for line in summary.splitlines() if line.startswith(('selection_counts_by_kind','positive_generated_timestamp_order','display_drawable_feedback','generated_frame_feedback','displayed_generated_frames','nearest_source_pair_brackets','malformed_requested_pairs','callback_tick_ledger','lost_source','source_terminal_records','submitted_unconfirmed'))],
  'layer_getters':[json.loads(x) for x in (p/'layer-runtime.jsonl').read_text().splitlines() if json.loads(x)['phase'] in ('settled_6s','measurement_15s','measurement_25s')]}
(root/'comparison.json').write_text(json.dumps(results,indent=2)+'\n')
print(json.dumps({k:{x:v[x] for x in ['app_present_rate_hz','callbacks_per_second','submissions_per_second','positive_presentedTime','zero_presentedTime','aligned_G']} for k,v in results.items()},indent=2))
