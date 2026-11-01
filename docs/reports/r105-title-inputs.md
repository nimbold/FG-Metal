# FG-METAL R105 — Elden Ring Title Inputs

Date: 2026-10-28

Scope: Find real Elden Ring depth and motion inputs associated with the R100 D3DMetal drawable producer. R104's GPU-resident color-to-MetalFX path remains the accepted baseline; it was not redesigned or re-proven.

## Verdict

| Check | Result | Evidence |
|---|---|---|
| `ELDEN_RING_RENDER` | `VALIDATED` | Three bounded selections each correlated 72 source producers to real drawables; all selected producer command buffers completed successfully. |
| `REAL_TITLE_DEPTH` | `NOT VALIDATED` | The corrected sampler successfully read the recurring stored, shader-readable depth attachment in three same-producer frames. Each 128×128 grid contained one repeated value across all 16,384 samples, with no spatial geometry structure; depth semantics therefore remain unvalidated. |
| `REAL_TITLE_MOTION` | `NOT FOUND` (trustworthy input) | Several `RG16Float` attachments were found, but the strongest recurring full-resolution candidate returned near-zero values in all 16,384 grid pairs on both samples, including a user-reported character/camera movement interval. Other format-only candidates were not validated. |
| `REAL_TITLE_METALFX_INTERNAL_TEXTURE` | `NOT ATTEMPTED` | R105 has no validated title depth and motion inputs. |

The R105 objective is not complete: no trustworthy depth/motion pair has been established, so the title-specific MetalFX experiment remains gated. The motion result follows the request's fallback rule: no trustworthy motion resource was observable, so direct motion hunting stops here. The report records the untested RG16 attachments without treating their format as proof.

## 1. Elden Ring render state

The game ran through the existing Highball/D3DMetal route. The controlled world run used PID `46649`, executable `/Users/nima/Games/Elden Ring/Game/eldenring.exe`, Metal device registry ID `4294968554`, and queue `0x7fa0e8ac0000`. The drawable was `1470×956`, pixel format `80` (`BGRA8Unorm`), usage `23`, and `framebufferOnly=0`.

The observer correlated 72 selected source sequences to drawable IDs and producer command buffers in that run. All 72 producer completion records ended with Metal status `4` and no error. The user confirmed being in the game and moving, then performed a still interval followed by character movement that also moved the camera. No paired full-resolution color image was retained for the depth/motion grid sequences, so scene identification and action timing are operator-reported rather than independently image-certified.

The final depth-only run used PID `47578` on the same device and game route. After the restart, the user loaded the same save, held still briefly, rotated the camera, and signaled completion. The one-shot inventory was triggered after that signal, so exact camera-input timing is not paired to individual sampled frames. The visible game scene and operator actions are user-confirmed; no full-resolution color image was retained for the sampled depth frames.

The startup color capture from the earlier relaunched process (PID `45715`) saved sequences `159–166` at `1470×956`, pixel format `80`; sequence `166` visually shows the Bandai Namco startup splash. That earlier capture is startup evidence only. Its later bounded window did not retain a paired image and is treated as scene-uncertain.

## 2. Frame and render-pass inventory

### Scene-uncertain selection — PID 45715

The one-shot trigger started at source sequence `5261`, sampled every third drawable sequence, and stopped after 72 correlated source producers at sequence `5624`. It recorded 692 attachment entries across 378 distinct render-pass IDs, including same-producer passes and up to 12 recently committed predecessor command buffers on the same queue within 100 ms.

### Controlled world selection — PID 46649

The second one-shot trigger started at source sequence `7562`, used the same stride and 72-producer limit, and ended at sequence `7904`. It recorded 1,657 attachment entries across 981 render-pass IDs. All 72 selected producer command buffers completed successfully.

For these windows, records tie `sourceSequence → drawableID → producerCommandBuffer → producer completion`. Each attachment record contains command-buffer, encoder, pass and texture identities; dimensions; pixel format; usage; storage mode; texture type/sample count; mip, slice and array details; load/store actions; and resolve information. The strongest depth and RG16 candidates below occur in passes marked `same-producer-command-buffer`, so their candidate lineage is tied to the exact drawable producer rather than inferred from texture size.

### Final corrected depth-only selection — PID 47578

The corrected one-shot trigger began at source sequence `3001`, selected every third drawable, and completed 72 source producers through sequence `3265`. The inventory recorded 483 render-pass records across 483 pass IDs and 907 attachment records; all 72 selected producer command buffers completed with status `4` and no error. The inventory recorded 675 same-producer attachment rows in 357 same-producer pass records.

## 3. Depth candidates and accessibility

### Candidate in the scene-uncertain selection

The actual depth attachment was texture `0x7fe5d0f695a0`, `1472×828×1`, pixel format `260` (`Depth32Float_Stencil8`), usage `21` (including shader read), private storage (`0`), 2D-array texture type with array length 1, sample count 1, mip 0, slice 0, and store action `Store`. It recurred across 11 unique source sequences.

The direct GPU diagnostic sampled this resource using `depth2d<float>` without changing its usage or creating a format view. The depth aspect is sampled from the original combined texture; Apple's [depth sampling example](https://developer.apple.com/documentation/arkit/creating-a-fog-effect-using-scene-depth?changes=lat_7__4_2&language=objc) uses the same Metal depth-texture access form.

The first observer repeated its readback six times for the same source sequence `5324` and producer command buffer. Every read was a `128×128` grid of 16,384 finite zeros. These were duplicates, not six frames. The cause was a bookkeeping bug: unrelated command-buffer commits reinitialized the depth de-duplication set. That bug was fixed in the next observer build. Since the scene in this run was not image-certified, the zero result alone does not establish world-depth semantics.

### Candidate in the controlled world selection

The world window observed a stored, shader-readable `Depth32Float_Stencil8` attachment, texture `0x7fa09b63cc90`, `1472×828×1`, usage `21`, private storage, single sample, array length 1. It appeared in 237 same-producer depth attachment records across 15 unique source sequences: `7565`, `7574`, `7577`, `7580`, `7586`, `7592`, `7595`, `7601`, `7604`, `7607`, `7610`, `7616`, `7634`, `7646`, and `7775`.

No depth grid was read in this controlled run. The diagnostic was still scheduled on every ninth producer ordinal; the eight scheduled source sequences (`7562`, `7589`, `7619`, `7664`, `7703`, `7748`, `7793`, `7847`) had no stored, shader-readable depth attachment in the same producer command buffer. This is a sampling-schedule miss, not proof the observed world depth texture was inaccessible or empty.

The current verdict is `REAL_TITLE_DEPTH = NOT VALIDATED`. No normalization or MetalFX input conversion was attempted. The fixed-ordinal scheduling gap is corrected and the final run sampled each eligible same-producer depth candidate available in its bounded window.

### Final corrected depth-only sample

In PID `47578`, texture `0x7fd40884b560` was a `1472×828×1` `Depth32Float_Stencil8` attachment (pixel format `260`), shader-read usage `21`, private storage, single sample, and store action `Store`. It appeared in 102 same-producer depth-attachment records spanning 11 selected source sequences. The corrected sampler successfully sampled the original depth aspect directly in the producer command buffer at source sequences `3040`, `3049`, and `3082`; each readback completed and saved a `128×128` float32 grid.

Each grid contained 16,384 finite values and exactly one unique value: `0.00230990350`, `0.00230873562`, and `0.00230917637`, respectively. There were no spatial differences within any grid. The small frame-to-frame value changes do not establish scene depth, and the flat samples do not show foreground/background separation or geometry-aligned discontinuities. Thus the candidate is accessible to the diagnostic shader, but `REAL_TITLE_DEPTH` remains `NOT VALIDATED`; no depth normalization or MetalFX conversion was attempted. The user's camera rotation preceded the inventory trigger, and its exact timing is not bound to these individual samples.

## 4. Motion candidates and semantic test

The controlled world inventory found four recurring `RG16Float` color-attachment objects with shader-read usage `21`, private storage, single sample, array length 1, and store action `Store`:

| Texture identity | Dimensions | Same-producer source sequences |
|---|---:|---|
| `0x7fa09b982150` | `1472×828` | `7574`, `7592`, `7607`, `7610`, `7616`, `7634`, `7646`, `7775` |
| `0x7fa09b5baaf0` | `1472×828` | `7592`, `7607`, `7646`, `7775` |
| `0x7fa09b5bf4a0` | `1472×828` | `7616`, `7646` |
| `0x7fa09b9993c0` | `736×414` | `7574`, `7592`, `7607`, `7610`, `7634`, `7646` |

The observer sampled the most recurrent full-resolution candidate (`0x7fa09b982150`) on the exact producer command buffer at source sequences `7616` (frame ordinal 18) and `7775` (ordinal 51). Each GPU sample produced a `128×128` raw `float32 RG` grid with 16,384 finite pairs and 16,384 near-zero pairs. At `7616`, R ranged `0..0` and G was `-5.96046448e-08` throughout; at `7775`, both channels were signed zero throughout. The user reported character and camera movement during the controlled window, but the observed values did not show vector-like response. The probe did not timestamp the user's exact input transitions, so it cannot prove whether either grid landed inside the movement interval; it does show the chosen recurring candidate was zero at both sampled times.

The other full-resolution RG16 candidates and half-resolution candidate remain format/lineage candidates only. There is no accepted component order beyond the texture's stored R/G channels, no sign convention, coordinate convention, unit scale, or motion response. `REAL_TITLE_MOTION = NOT FOUND` means no trustworthy motion input was validated; it does not mean the inventory contained no RG16 textures. Direct motion-resource hunting stops per the R105 fallback rule.

## 5. Frame lineage and MetalFX

Depth and RG16 attachments were recorded in the same producer command-buffer lineage as selected source drawables, and the selected producer command buffers completed. The world motion diagnostic also sampled its candidate inside the same producer command buffer. These facts establish candidate lineage, not semantics.

No title depth/motion inputs were normalized, no sequential Elden Ring frames were submitted to MetalFX, and no generated midpoint texture was inspected. R104's provider and presentation architecture remain unchanged.

## 6. Corrected depth probe executed

The observer removed the every-ninth-frame depth schedule and sampled three eligible same-producer depth candidates in the final 72-frame window. It preserved GPU-only sampling of the original depth aspect, command-buffer de-duplication, and the bounded inventory. The output path is `experiments/metalfx_frame_interpolator_R93/evidence/r105-title-inputs/run-04-depth-probe/`.

The corrected library compiled successfully as an x86_64 Mach-O dylib. SHA-256: `541a99632178bfe8dfcefac404ab5a416edd654c4056a76d85a9843ea32dc08a`. It was loaded in PID `47578`; the process log records `R105_DEPTH_PIPELINE=READY` and has no motion sampler pipeline. The final inventory and three raw grids are retained in the run directory above; the process log is `/tmp/fgmetal-r105-title-inputs/observer.47578.log`. The game remains open. After capture, the temporary Highball environment variables (`DYLD_INSERT_LIBRARIES`, `FGMETAL_R105_CAPTURE`, and `FGMETAL_R105_FRAMEBUFFER_ONLY`) were cleared; the Highball Advanced settings view confirmed the environment-variable field is blank.

## Evidence locations

- Current observer source: `experiments/metalfx_frame_interpolator_R93/evidence/r105-title-inputs/observer-title-inputs-r105.m`
- Build script: `experiments/metalfx_frame_interpolator_R93/evidence/r105-title-inputs/build_r105_observer.sh`
- Corrected depth-probe library: `experiments/metalfx_frame_interpolator_R93/evidence/r105-title-inputs/libfgmetal_r105_title_inputs.dylib`
- Motion-probe library used for PID 46649: `experiments/metalfx_frame_interpolator_R93/evidence/r105-title-inputs/libfgmetal_r105_title_inputs_motion-probe.dylib` (SHA-256 `0fcb3fa572f0c0ffdc44e22d5e8c54a3beebd8c125301bc4c041e252cb027eea`)
- Scene-uncertain process log: `/tmp/fgmetal-r105-title-inputs/observer.45715.log`
- Controlled world process log: `/tmp/fgmetal-r105-title-inputs/observer.46649.log`
- Final depth-only process log: `/tmp/fgmetal-r105-title-inputs/observer.47578.log`
- Final depth grids: `experiments/metalfx_frame_interpolator_R93/evidence/r105-title-inputs/run-04-depth-probe/depth-sequence-3040.raw`, `depth-sequence-3049.raw`, and `depth-sequence-3082.raw`
- Controlled world raw grids and startup captures: `experiments/metalfx_frame_interpolator_R93/evidence/r105-title-inputs/run-03-controlled-world-probe/`
- Earlier title/menu inventory: `experiments/metalfx_frame_interpolator_R93/evidence/r105-title-inputs/run-02-title-menu/`
- Accepted baseline report: `FG-METAL-R104-PRODUCT-CONSOLIDATED-REPORT.md`
