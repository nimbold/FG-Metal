# BR8 storage-retention plan

## Decision

The BR7 attempt ledgers document exactly two rejected, superseded BR7 short-run directories as candidates for a future per-file-verified archive:

| Exact directory | Recorded rejection | Accepted replacement |
| --- | --- | --- |
| `runs/step11d3b-br7-sample-03-20261010-075645-48e18549` | `run-result.json` is `FAIL` / invalid: trace validation reports malformed dyld loader process prefix at line 11178. Process exit was 0; the trace validator rejected the run. Prefix cleanup passed. | `runs/step11d3b-br7-sample-03-20261010-080324-f416269a`, reported PASS and accepted as short run 3. |
| `runs/step11d3b-br7-sample-05-20261010-081350-dbe1558c` | `run-result.json` is `FAIL` / invalid for the same malformed dyld loader prefix at line 11178. Process exit was 0; the trace validator rejected the run. Prefix cleanup passed. | `runs/step11d3b-br7-sample-05-20261010-082920-99c26e88`, reported PASS and accepted as short run 5. |

The BR7 report says these first attempts were rejected without relaxing the parser or assigning a quantitative classification. The first- and second-attempt ledgers identify no other rejected BR7 short-run directories. Earlier failed smoke directories are outside this BR7-only candidate set.

These are candidate paths, not pre-certified archive inputs. The existing `archive_superseded.py` uses per-file SHA256, size, mtime, inode, and device checks; verifies the archive member set and file hashes; hashes the resulting archive; records a receipt before retirement; and has a restore path. Its current selector covers older smoke runs and BR5 probes, not these sample directories. The companion extra script selects BR4 evidence. A future pass must add only the two exact IDs above and expand its protected set before any retirement. Do not add a `step11d3b-br7-sample-*` selector: it would match accepted BR7 samples too. The scripts reject symlinks and verify files at archive time, so eligibility remains conditional on those existing checks passing. Neither script was run for this plan.

## Savings estimate and headroom

The saved `run-result.json` files report target host-trace sizes of 42,020,938 and 42,173,178 bytes, 84,194,116 bytes total (80.3 MiB) of JSONL trace data. Exact directory logical bytes and hypothetical archive bytes are **UNKNOWN** here because no storage inventory or compression pass was run. Historical verified retirements provide only a sensitivity reference: 1,866,705,304 logical bytes became a 335,039,183-byte archive (82.05% reduction), while 136,288,833 bytes became a 3,777,532-byte archive (97.23% reduction). Applying those unlike-contents ratios to the known 80.3 MiB trace subset yields an illustrative 65.9–78.1 MiB saved; it is not a forecast for the complete run directories, which also contain binaries and screenshots. Do not credit any savings in Gate0 until a later archive receipt records exact input and output bytes.

The supplied fresh scoped value is 43.68 GiB against the 45 GiB cap, a nominal 1.32 GiB margin. The accepted smoke `step11d3b-smoke-20261010-201913-d1f81d96` and the pending seven 80-second states plus possible 300-second run have no storage deltas established in this read-only pass; remaining headroom after them is therefore **UNKNOWN**.

## Protected evidence

Keep expanded and untouched all accepted BR6 and BR7 runs and their `run-result.json`, `run-manifest.json`, raw traces, trace indexes, per-run hashes, and linked analysis. The accepted BR6 IDs are smoke `step11d3b-smoke-20261009-223106-0e5e3a6b` and `step11d3b-smoke-20261010-053718-a78894d0`, plus baselines `step11d3b-baseline-01-20261009-223854-cfea0560`, `step11d3b-baseline-02-20261010-054210-3a16e5aa`, and `step11d3b-baseline-03-20261010-062321-91bb8f4d`. The accepted BR7 set includes smoke `step11d3b-smoke-20261010-074106-d99e7461`, samples 01–06 (including the replacements above), and accepted 300-second baseline `step11d3b-baseline-02-20261010-084004-9ef35a0d`. Keep accepted BR8 smoke `step11d3b-smoke-20261010-201913-d1f81d96` and current BR8 state run `step11d3b-br8-state-01-20261010-202408-c3f551ea` in place as well.

Keep all reports, analyses, attempt ledgers, source/runtime manifests, hash records, and receipts under `evidence/br7` and `evidence/br8`; keep the signed per-run admission manifests under `experiments/storage/manifests` and runtime identity manifest under `experiments/storage/runtime-manifests`. Preserve the already verified `superseded-pre-br7.tar.gz` and `superseded-pre-br7-extra.tar.gz` with their SHA256 receipts. If a later archive is authorized, only the two rejected run directories above may be included; their original storage admission manifests remain in place, and the new receipt must retain each archived file's hash and size plus the archive hash and restore location.

## Evidence and limits

Read: `STEP-11D3-BR7-REPORT.md` §§ 3, 14–16; `short-series-first-attempts.json`; `short-series-second-attempts.json`; both candidate `run-result.json` files; `archive_superseded.py`; `archive_superseded_extra.py`; and the two existing retirement receipts. No live graphics, build, archive, deletion, mutation, or storage-inventory command was run. The archive savings are not verified, and the two candidate directories have not been certified against the retention script's file-type and identity checks.
