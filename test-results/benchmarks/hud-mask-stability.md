# HUD mask stability measurement

- Corpus: `synthetic-motion-v1` (128×72)
- Source indices: `[0, 2, 4, 6, 8, 10]` at `t=0.5`
- Backend mode: `automatic`; synthetic placeholder only
- Raw mask transition MAE: 0.063097
- Stabilized mask transition MAE: 0.039563
- Transition-MAE reduction: 37.3%
- Raw HUD ROI precision/recall at confidence 0.5: `0.5239` / `0.2853`
- Stabilized HUD ROI precision/recall at confidence 0.5: `0.5278` / `0.3119`
- Raw per-frame coverage: `[0.140393, 0.157439, 0.164246, 0.157763, 0.163107]`
- Stabilized per-frame coverage: `[0.141857, 0.182606, 0.205311, 0.214302, 0.22255]`

Detection scores compare a 0.5 confidence threshold with the authored binary HUD ROI at each exact midpoint. That ROI is a proxy for pixels intended for protection, not ground-truth confidence. Lower transition difference can also mean slower response.
