"""Per-run sustained-regime distributions; transition cohorts stay separate."""
import csv,json,sys,statistics
from collections import Counter
from pathlib import Path
base=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(base.parents[1]))
from experiments.dxvk_macos_step11d3b.br7_analysis import duration_stats,duration_histogram
root=Path(sys.argv[1]); payload=json.loads((root/'summary.json').read_text()); frames={}
for row in csv.DictReader((root/'frames.csv').open()):
 for key,value in list(row.items()):
  if value and (key.endswith('_ns') or key in ('acquisition_sequence','presented_time_mach_ns')):
   row[key]=int(value)
 frames.setdefault(row['run_id'],[]).append(row)
outputs=[]
for run in payload['runs']:
 origin=run['host_interval']['start_ns']; groups={}; exposure={}
 segments=run['regime_analysis']['stable_segments']
 for segment in segments:exposure[segment['regime']]=exposure.get(segment['regime'],0)+segment['end_elapsed_ns']-segment['start_elapsed_ns']
 for frame in frames[run['run_id']]:
  elapsed=frame['host_acquire_ts_mach_ns']-origin
  label=next((s['regime'] for s in segments if s['start_elapsed_ns']<=elapsed<s['end_elapsed_ns']),'TRANSITION_OR_EDGE')
  groups.setdefault(label,[]).append(frame)
 period=run['refresh_phase']['reference_period_ns']; ref=run['refresh_phase']['reference_presented_time_mach_ns']
 for label,rows in groups.items():
  outcomes=Counter(r['outcome_enum'] for r in rows); n=len(rows); sec=exposure.get(label,0)/1e9
  zero=outcomes.get('CALLBACK_PRESENTED_TIME_ZERO',0);displayed=outcomes.get('DISPLAYED_VALID_PRESENTED_TIME',0)
  q=[0]*4
  if period and ref is not None:
   for r in rows:
    if r.get('request_ts_mach_ns') is not None:q[min(3,int(((r['request_ts_mach_ns']-ref)%period)*4/period))]+=1
  waits=[r['next_drawable_duration_ns'] for r in rows if r.get('next_drawable_duration_ns') is not None]
  submits=[r['host_submit_duration_ns'] for r in rows if r.get('host_submit_duration_ns') is not None]
  positive=sorted(set(r['presented_time_mach_ns'] for r in rows if r.get('presented_time_mach_ns',0)>0))
  intervals=[b-a for a,b in zip(positive,positive[1:]) if 14e6<=b-a<=19e6]
  outputs.append({'run_id':run['run_id'],'mode':label,'frames':n,'exposure_seconds':sec or None,'acquired_cohort_fps':n/sec if sec else None,'displayed_cohort_fps':displayed/sec if sec else None,'zero_fraction':zero/n,'presentation_success':displayed/n,'outcome_counts':dict(outcomes),'measured_mode_refresh_period_ns':statistics.median(intervals) if intervals else None,'measured_mode_refresh_samples':len(intervals),'phase_reference_period_ns':period,'phase_reference_source':'run-wide valid displayed timestamp fit; separate mode requests, same observed run display','request_phase_quartiles_percent':[100*x/sum(q) if sum(q) else None for x in q],'next_drawable':duration_stats(waits),'submit':duration_stats(submits),'next_drawable_histogram':duration_histogram(waits),'submit_histogram':duration_histogram(submits)})
(root/'per-mode.json').write_text(json.dumps({'assignment':'Frame acquired in sustained one-second regime segment; transition/edge cohorts excluded from normal/pathological rates. Cohort FPS is final frame outcome count divided by segment exposure, not an instantaneous callback delivery rate. All runs remain separate.','modes':outputs},indent=2)+'\n')
print('Wrote',root/'per-mode.json')
