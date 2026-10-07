#!/usr/bin/env python3
"""Post-run source Present tail correlation for frozen R5 evidence."""
from __future__ import annotations
import csv, json, re, statistics
from pathlib import Path

ROOT = Path('/Users/nima/Documents/Code/FG-Metal/experiments/dxvk_macos_step11dR/evidence')
CURRENT = ROOT / 'cadence-repeatability-r5-20261006'
R5 = Path('/Users/nima/.codex/worktrees/step11d-r/FG-Metal/experiments/dxvk_macos_step11dR/evidence/renderer-integration')
RUNS = {k: CURRENT / k for k in ('A1','B1','A2','B2','A3','B3')}
CONTROLS = {
    'SOURCE_ONLY_60S': R5 / 'source-only-current-r5-30hz-60s',
    'VULKAN_CLEAR_60S': R5 / 'source-built-r5-vulkan-clear-30hz-60s',
    'METAL_ROUNDTRIP_60S': R5 / 'metal-invert-rate-30hz-60s-current-r5-controlled-nocapture',
}
THRESHOLDS_US = (5_000, 10_000, 25_000, 50_000, 100_000)

PRESENT_RE = re.compile(r'InternalWSI: source-present app=(\d+) duration-us=(\d+) hr=(\S+)')
WSI_RE = re.compile(r'WSI: present origin=APPLICATION_SOURCE wsi=(\d+) app=(\d+) internal=(\d+) acquire=(\d+) image=(\d+) generation=(\d+)')
CAP_RE = re.compile(r'MetalInterop: captured InteropJobId=(\d+) AppFrameId=(\d+) generation=(\d+) slot=(\d+) slot-serial=(\d+) history-id=(\d+) output-id=(\d+) READY=(\d+) DONE=(\d+) CONSUMED=(\d+) copy-record-us=(\d+)')
COMMIT_RE = re.compile(r'MetalInterop: committed InteropJobId=(\d+) AppFrameId=(\d+).*?ready-to-commit-us=(\d+)')
TERM_RE = re.compile(r'MetalInterop: terminal InteropJobId=(\d+) AppFrameId=(\d+).*?state=(\S+) terminal-value=(\d+) capture-to-terminal-us=(\d+)')
RETIRE_RE = re.compile(r'MetalInterop: job-retired InteropJobId=(\d+) AppFrameId=(\d+).*?capture-to-retire-us=(\d+) terminal-to-retire-us=(\d+)')
DROP_RE = re.compile(r'MetalInterop: dropped AppFrameId=(\d+) reason=(.*?) generation=(\d+)')


def percentile(values: list[int], pct: float):
    if not values: return None
    vals = sorted(values)
    # Nearest-rank percentile: rank = ceil(pct*n).
    import math
    return vals[max(0, math.ceil(pct * len(vals)) - 1)]


def parse_run(name: str, directory: Path):
    app_csv = directory / 'd3d11_clear_window_app.csv'
    d3dlog = directory / 'slow-visual_d3d11.log'
    if not app_csv.exists() or not d3dlog.exists():
        return {'run': name, 'available': False, 'missing': [str(p) for p in (app_csv,d3dlog) if not p.exists()]}, []
    csv_rows = []
    with app_csv.open(newline='') as f:
        for row in csv.DictReader(f):
            if row.get('event') == 'present':
                csv_rows.append(row)
    lines = d3dlog.read_text(errors='replace').splitlines()
    source = []
    for lineno, line in enumerate(lines, 1):
        m = PRESENT_RE.search(line)
        if m:
            source.append({'line': lineno, 'app': int(m.group(1)), 'duration_us': int(m.group(2)), 'hr': m.group(3)})
    # Capture id-keyed records for exact job/frame joins.
    by_app = {}
    by_job = {}
    drops_by_app = {}
    for lineno, line in enumerate(lines, 1):
        for regex, kind in ((CAP_RE,'capture'),(COMMIT_RE,'commit'),(TERM_RE,'terminal'),(RETIRE_RE,'retirement')):
            m = regex.search(line)
            if not m: continue
            job, app = int(m.group(1)), int(m.group(2))
            if kind == 'capture':
                rec = {'line':lineno,'job':job,'app':app,'generation':int(m.group(3)),'slot':int(m.group(4)),'slot_serial':int(m.group(5)),'history_id':int(m.group(6)),'output_id':int(m.group(7)),'READY':int(m.group(8)),'DONE':int(m.group(9)),'CONSUMED':int(m.group(10)),'copy_record_us':int(m.group(11))}
            elif kind == 'commit': rec={'line':lineno,'job':job,'app':app,'ready_to_commit_us':int(m.group(3))}
            elif kind == 'terminal': rec={'line':lineno,'job':job,'app':app,'state':m.group(3),'terminal_value':int(m.group(4)),'capture_to_terminal_us':int(m.group(5))}
            else: rec={'line':lineno,'job':job,'app':app,'capture_to_retire_us':int(m.group(3)),'terminal_to_retire_us':int(m.group(4))}
            by_app.setdefault(app,{})[kind] = rec
            by_job.setdefault(job,{})[kind] = rec
        m = DROP_RE.search(line)
        if m: drops_by_app.setdefault(int(m.group(1)),[]).append({'line':lineno,'reason':m.group(2),'generation':int(m.group(3))})
    wsi_by_app = {}
    for lineno,line in enumerate(lines,1):
        m=WSI_RE.search(line)
        if m:
            wsi_by_app[int(m.group(2))]={'line':lineno,'wsi_present_id':int(m.group(1)),'acquire_id':int(m.group(4)),'image':int(m.group(5)),'generation':int(m.group(6))}
    # Pair the ordered per-frame DXVK timing stream with the ordered app Present rows.
    paired = min(len(source), len(csv_rows))
    correlations=[]
    for i in range(paired):
        s=source[i]; row=csv_rows[i]; app=s['app']; related=by_app.get(app,{})
        if s['duration_us'] <= THRESHOLDS_US[0]: continue
        nearby_jobs=[]
        for near_app in (app-1,app,app+1):
            for kind,rec in by_app.get(near_app,{}).items():
                if kind in ('capture','commit','terminal','retirement'):
                    nearby_jobs.append({'frame_relation':near_app-app,'kind':kind,**rec})
        correlations.append({
            'app_frame_id':app,'source_present_seq':int(row['app_present_seq']),'elapsed_us':int(row['elapsed_us']),
            'duration_us':s['duration_us'],'thresholds_crossed_ms':[int(t/1000) for t in THRESHOLDS_US if s['duration_us']>t],
            'present_hr':s['hr'],'log_line':s['line'],'wsi':wsi_by_app.get(app),
            'job_for_same_app_frame':related,'no_free_or_other_pre_capture_drops':drops_by_app.get(app,[]),
            'adjacent_frame_job_events':nearby_jobs,
            'active_job_count_at_present':'not_measurable: interop events lack monotonic timestamps and slot snapshots',
            'unmeasured_stages':['WSI acquire duration','queue/lock wait','source command recording and submit durations','Metal bridge call duration','slot-state snapshot/scheduler state','swapchain lifecycle timestamp','Wine thread scheduling','OS scheduling','logging cost per stage']
        })
    vals=[s['duration_us'] for s in source]
    stats={'count':len(vals),'p50_us':percentile(vals,.5),'p95_us':percentile(vals,.95),'p99_us':percentile(vals,.99),'p99_9_us':percentile(vals,.999),'max_us':max(vals) if vals else None}
    # Counts are inclusive of each strict threshold (> threshold).
    tails={f'>{t//1000}ms':sum(v>t for v in vals) for t in THRESHOLDS_US}
    note=None if len(source)==len(csv_rows) else f'ordered streams differ: source trace={len(source)}, CSV rows={len(csv_rows)}; paired first {paired}'
    result={'run':name,'path':str(directory),'source_present_cpu_us':stats,'tail_counts':tails,'outlier_count_over_5ms':len(correlations),'outlier_rows':correlations,'source_trace_count':len(source),'app_csv_present_count':len(csv_rows),'pairing_note':note}
    return result, correlations

results=[]
all_corr=[]
for name,path in [*RUNS.items(),*CONTROLS.items()]:
    result, rows=parse_run(name,path)
    results.append(result)
    all_corr += [{'run':name,**row} for row in rows]
comparison={'percentile_method':'nearest rank ceil(p*n), duration values from InternalWSI source-present trace; elapsed_us joined by ordered app Present sequence','runs':results}
(ROOT/'source-latency-comparison-20261006.json').write_text(json.dumps(comparison,indent=2)+'\n')
with (ROOT/'source-present-tail-correlation-20261006.csv').open('w',newline='') as f:
    fields=['run','app_frame_id','source_present_seq','elapsed_us','duration_us','thresholds_crossed_ms','present_hr','wsi','job_for_same_app_frame','no_free_or_other_pre_capture_drops','adjacent_frame_job_events','active_job_count_at_present','unmeasured_stages','log_line']
    w=csv.DictWriter(f,fieldnames=fields); w.writeheader()
    for row in all_corr:
        w.writerow({**row,'thresholds_crossed_ms':json.dumps(row['thresholds_crossed_ms']),'wsi':json.dumps(row['wsi']),'job_for_same_app_frame':json.dumps(row['job_for_same_app_frame']),'no_free_or_other_pre_capture_drops':json.dumps(row['no_free_or_other_pre_capture_drops']),'adjacent_frame_job_events':json.dumps(row['adjacent_frame_job_events']),'unmeasured_stages':json.dumps(row['unmeasured_stages'])})
(ROOT/'source-present-tail-correlation-20261006.json').write_text(json.dumps({'rows':all_corr},indent=2)+'\n')
print(json.dumps([{'run':x['run'],'latency':x.get('source_present_cpu_us'),'tail_counts':x.get('tail_counts'),'outliers':x.get('outlier_count_over_5ms'),'pairing_note':x.get('pairing_note')} for x in results],indent=2))
