import argparse
import csv
import statistics
from pathlib import Path

parser = argparse.ArgumentParser(description='Summarize a CAMetalDisplayLink probe CSV.')
parser.add_argument('csv_path', type=Path, help='CSV written by cametal_displaylink_probe.mm')
args = parser.parse_args()

with args.csv_path.open(newline='') as f:
    rows = list(csv.DictReader(f))
if not rows:
    parser.error(f'CSV contains no display-link samples: {args.csv_path}')
num = lambda row, key: float(row[key])
host_ns = [num(r, 'callback_host_ns_since_mach_epoch') for r in rows]
ca_now = [num(r, 'ca_now_seconds') for r in rows]
target = [num(r, 'target_seconds') for r in rows]
presentation = [num(r, 'target_presentation_seconds') for r in rows]

def report(name, values, scale=1.0):
    values = [v * scale for v in values]
    print(f'{name}: n={len(values)} mean={statistics.mean(values):.9f} median={statistics.median(values):.9f} min={min(values):.9f} max={max(values):.9f} population_sd={statistics.pstdev(values):.9f}')

print(f'samples={len(rows)}')
report('callback_interval_ms', [(host_ns[i] - host_ns[i-1]) / 1e9 for i in range(1, len(rows))], 1000)
report('target_interval_ms', [target[i] - target[i-1] for i in range(1, len(rows))], 1000)
report('presentation_interval_ms', [presentation[i] - presentation[i-1] for i in range(1, len(rows))], 1000)
report('ca_minus_mach_callback_ns', [ca_now[i] - host_ns[i]/1e9 for i in range(len(rows))], 1e9)
# These summary offsets use the mach_absolute_time sample converted to ns.
# The CSV's precomputed offset columns instead use its adjacent ca_now_seconds
# read; both reads share the Core Animation/Mach monotonic epoch to sub-us here.
report('target_minus_mach_callback_ms', [(target[i]*1e9-host_ns[i])/1e6 for i in range(len(rows))], 1)
report('presentation_minus_mach_callback_ms', [(presentation[i]*1e9-host_ns[i])/1e6 for i in range(len(rows))], 1)
report('presentation_minus_target_ms', [presentation[i]-target[i] for i in range(len(rows))], 1000)
print('callback_delta_gt_1.5_refresh_periods=', sum((host_ns[i]-host_ns[i-1])/1e9 > 1.5/60 for i in range(1, len(rows))))
print('callback_delta_gt_2_refresh_periods=', sum((host_ns[i]-host_ns[i-1])/1e9 > 2/60 for i in range(1, len(rows))))
print('first_row=', rows[0])
print('last_row=', rows[-1])
