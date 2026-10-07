# STEP 11D.2 large log archive

GitHub rejects individual files larger than 100 MiB. The two oversized raw logs are therefore stored here as lossless gzip archives. Their uncompressed SHA-256 values are preserved below. To restore a raw log, run `gzip -dc <archive>.gz > <original-path>` and verify the restored SHA-256.

| Original path relative to the evidence directory | Archive path relative to the evidence directory | Raw bytes | Raw SHA-256 | Archive SHA-256 |
|---|---|---:|---|---|
| `diag-300s-10-low-candidate/runtime.log` | `diag-300s-10-low-candidate/runtime.log.gz` | 120,827,502 | `d4b4078934d574f9df3ca8e8c203bff3755a047e0340905324fedc6a1dbfd343` | `e2a38437565b743c17c0989f129268225ccb18c1906ce42dfdb445b83d1060bb` |
| `diag-300s-10-low-candidate/slow-visual_d3d11.log` | `diag-300s-10-low-candidate/slow-visual_d3d11.log.gz` | 112,423,775 | `3af132d04cfa37faf6eb80b4cfaba794b309c4a9bfee7e8ab6ac14d9a38a962e` | `5035373dfe66d2982fd162f6c01ec347c974fe3b6aec01445def88475ebc90e2` |

Both archives were decompressed and verified against the original bytes before commit. The original raw files remain in this local evidence tree and are excluded from Git; decompressing the tracked archive recreates each original file exactly.
