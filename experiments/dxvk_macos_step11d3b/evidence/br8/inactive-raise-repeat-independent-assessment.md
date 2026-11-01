# BR8 inactive-raise repeat: independent assessment

## Finding

The accepted repeat `step11d3b-br8-state-02-20261010-205756-a9b95137` is a valid 80-second observation of a visible, ordered target window while the target app remains inactive, nonkey, and nonmain according to its own AppKit telemetry. The target was already in that state at its initial sample; there is no recorded active-to-inactive transition. This repeat therefore does not show that Finder activation caused the inactive state.

The attempted return to the target was not attained. At about +60.06 seconds, `NSRunningApplication.activateWithOptions` returned `accepted=true`; at about +61.07 seconds, the Accessibility helper's `AXRaise` action returned `action_error=0`. Those return values record accepted/completed requests. They do not establish target activation. The AX snapshot reported `AXMain=true` and `AXFocused=true`, but every target AppKit telemetry sample across the run reports `window_main=false`, `window_key=false`, and `app_active=false`. There are no target state notification transitions. A separate CoreGraphics snapshot about +65 seconds still ranks Finder's on-screen window ahead of the on-screen target window. The AppKit and window-order evidence do not confirm restoration; the AX result conflicts with them and is insufficient to override them.

## Attainment evidence

The target trace contains 80 `window_telemetry` records: the initial record plus 79 periodic samples. All 80 report `window_visible=true`, `window_ordered=true`, `window_miniaturized=false`, `window_key=false`, `window_main=false`, `window_occluded=false`, and `app_active=false`. The first target sample reports this state immediately; the analysis finds no target notification transitions. The `window_occluded=false` bit is only AppKit's visible-bit observation and does not establish full-window exposure or quantify occlusion.

At the controller's +10, +25, and +65 snapshots, CoreGraphics lists the target as on-screen, alpha 1, rank 4, at (68, 62) with size 640×392. Finder's “Recents” window is on-screen, alpha 1, rank 3, at (734, 33) with size 920×697. The reported rectangles do not overlap, so this establishes neither physical coverage of the target nor opaque per-pixel occlusion. The rank does corroborate that Finder remains ahead of the target in the window list at the post-raise snapshot.

The target-activation helper reports `accepted=true` around +60.06 seconds, with `requires_target_telemetry_attainment=true`. The Accessibility helper ran around +61.07 seconds, returned `action_error=0`, and read `AXMain=true` / `AXFocused=true`. No target AppKit sample changes state, and no become-active/key/main notifications appear in the retained trace. Treat the AX fields as a contradictory accessibility snapshot, not as proof that the intended foreground state was reached. The controller clock/action records are retained separately; this assessment uses their shared Mach clock only for descriptive timing and infers no causation.

## Presentation outcome

The run validates `PASS` with a stable target/control binding. The target ran for 80.008 seconds, recorded 4,825 unique acquisitions, and had 4,796 exact displayed outcomes (59.9445 FPS as reported by the runner) against a 60.0007 Hz effective refresh. The 80 full one-second bins contain 60–85 acquisitions and 57–60 exact displayed outcomes per second. The 29 exact raw-zero timestamps are limited to the first bin (26) and final bin (3); all other bins have zero. Maximum `nextDrawable` wait is 25.726 ms, with no event at or above 100 ms or 900 ms.

The retained BR8 classifier reports `NO_SUSTAINED_PATHOLOGY_OBSERVED`, zero qualifying seconds, and no qualifying segments. This is a run-local negative finding for the defined high-acquisition / high-zero-share / low-displayed-rate criterion. The isolated edge-bin zero callbacks do not meet its five-contiguous-full-second condition. The finding does not establish why those callbacks had zero `presentedTime`, and this inactive-state run does not test post-restoration behavior.

## Evidence references and limits

- Accepted run result: run-result.json (`experiments/dxvk_macos_step11d3b/runs/step11d3b-br8-state-02-20261010-205756-a9b95137/run-result.json`)
- BR8 outcome and pathology analysis: [inactive-raise-repeat-analysis.json](inactive-raise-repeat-analysis.json)
- Target-owned telemetry: [host-trace.3197.jsonl](../../runs/step11d3b-br8-state-02-20261010-205756-a9b95137/host-traces/host-trace.3197.jsonl)
- External actions and snapshots: [inactive-raise-repeat-controller.json](inactive-raise-repeat-controller.json), [inactive-raise-repeat-coverage-65.json](inactive-raise-repeat-coverage-65.json), and [inactive-raise-repeat-sequence.log](inactive-raise-repeat-sequence.log)

This run does not establish a B1/B2 classification by itself. It supplies a valid inactive-state hold without an attained restoration. It cannot demonstrate whether foreground restoration changes presentation, and its accepted activation/raise calls must not be described as state attainment. No live UI interaction, graphics run, or test was performed for this assessment.
