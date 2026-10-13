"""Read-only, explicit completed analysis set -> compact report supplements."""
import hashlib
import json
from pathlib import Path

E = Path(__file__).resolve().parent
tags = ('foreground-repeat', 'inactive-frontmost', 'nonkey-frontmost',
        'partial-frontmost', 'full-frontmost', 'full-repeat-01',
        'full-repeat-02', 'full-confirmation')
records = []
lines = ['# BR8 completed-run and notification-phase measurements', '',
         'Exact target notification anchors; no controller request is substituted for a notification.',
         'Rates use observed cohort coverage, not requested duration. Zero means a raw zero callback.',
         'Durations below are median / p99 / max in milliseconds. Refresh quartiles use the fixed run-wide reference and are descriptive.', '']

def f(v):
    return 'unknown' if v is None else f'{v:.4f}'

def durations(d):
    return ' / '.join(f(d.get(k) / 1e6 if d.get(k) is not None else None)
                      for k in ('median_ns', 'p99_ns', 'max_ns'))

for tag in tags:
    p = E / f'{tag}-analysis.json'
    analysis = json.loads(p.read_text())['runs'][0]
    run = Path(analysis['run_directory'])
    result = json.loads((run / 'run-result.json').read_text())
    if result['status'] != 'PASS' or not result['prefix_cleanup_pass']:
        raise RuntimeError(f'unaccepted run: {tag}')
    v = result['validation']
    raw_zero = sum(frame.get('presented_time_mach_ns') == 0 for frame in v['frames'])
    if raw_zero != v['presentation_drop_count']:
        raise RuntimeError(f'legacy drop field differs from raw zero count: {tag}')
    record = dict(tag=tag, run_id=analysis['run_id'], status=result['status'],
                  acquired=v['frame_count'], displayed=v['displayed_frames'],
                  raw_zero=raw_zero, acquired_fps=v['acquired_fps'],
                  displayed_fps=v['presented_fps'], maximum_next_drawable_ms=v['next_drawable']['maximum_ns']/1e6,
                  sustained_segments=analysis['pathology_assessment']['sustained_segments'],
                  analysis_sha256=hashlib.sha256(p.read_bytes()).hexdigest(),
                  run_result_sha256=hashlib.sha256((run/'run-result.json').read_bytes()).hexdigest())
    records.append(record)
    lines += [f'## {tag}', '', f"Run `{analysis['run_id']}`: PASS; {record['acquired']} acquired / {record['displayed']} displayed / {record['raw_zero']} raw zero; whole-run {f(record['acquired_fps'])} acquired / {f(record['displayed_fps'])} displayed FPS. Maximum nextDrawable {f(record['maximum_next_drawable_ms'])} ms.", '']
    for t in analysis['notification_transitions']:
        lines += [f"### {t['notification_name']} at +{t['elapsed_from_host_start_ns']/1e9:.9f}s", '',
                  '| Phase | Coverage s | Acquired FPS | Displayed FPS | Raw zero % | nextDrawable ms | Submit ms | Callback latency ms | Drawable ≥100/900ms | Refresh Q1/Q2/Q3/Q4 % |',
                  '|---|---:|---:|---:|---:|---|---|---|---|---|']
        for name, phase in t['phases'].items():
            if phase is None:
                continue
            c, request = phase['acquisition_cohort'], phase['request_phase']
            quartiles = request.get('refresh_phase', {}).get('quartiles', [])
            q = ' / '.join(f(x['percent']) for x in quartiles) or 'unavailable'
            lines.append(f"| {name} | {f(phase['interval']['observed_coverage_ns']/1e9)} | {f(c['acquired_fps'])} | {f(c['displayed_cohort_fps'])} | {f(c['raw_zero_share_of_acquisitions']*100 if c['raw_zero_share_of_acquisitions'] is not None else None)} | {durations(c['next_drawable_duration_ns'])} | {durations(c['host_submit_duration_ns'])} | {durations(request['request_to_callback_latency_ns'])} | {c['next_drawable_ge_100ms_count']} / {c['next_drawable_ge_900ms_count']} | {q} |")
        lines.append('')
    if not analysis['notification_transitions']:
        lines += ['No target notification transition in this run; no exact-notification phase table can be claimed. Partial-cover external action brackets are a separate supplement.', '']

for name in ('completed-run-summary.json', 'transition-metrics.md'):
    if (E/name).exists():
        raise RuntimeError('refusing existing supplement overwrite')
(E/'completed-run-summary.json').write_text(json.dumps(records, indent=2)+'\n')
(E/'transition-metrics.md').write_text('\n'.join(lines)+'\n')
print('Generated eight accepted-run summaries and notification tables.')
