# Provenance and third-party material

This file is the project’s source of truth for the origin, license, and allowed use of third-party material. It tracks distinct kinds of material separately because the rights to a model implementation do not establish rights to its weights or training data, and a runtime dependency’s license does not establish that its bundled assets may be redistributed.

The original project code is licensed under Apache-2.0. This is not a blanket license for third-party components, model assets, or datasets. **No third-party item is approved merely because it is mentioned here or can be downloaded.** Every item must have its own complete record and maintainer review before it is added, used, bundled, or redistributed.

## Intake and approval procedure

For each proposed item:

1. Classify it in exactly the relevant inventory below: source code, model implementation, model weights, training dataset, or runtime dependency. Create separate records when a package contains multiple kinds (for example, runtime code and model weights).
2. Identify the exact name, upstream author or organization, canonical source, version/revision, and the date checked. For files, record a cryptographic checksum (SHA-256 or stronger).
3. Link the exact license and any notices, model/data terms, patent terms, or other use conditions. Record separately whether use, modification, and redistribution are permitted, and under what conditions. Do not infer rights from a repository badge, download availability, or a license covering only part of a package.
4. Record the intended project use and distribution surface: development only, build-time, optional runtime download, bundled application, or redistribution to users. Note attribution, source-offer, notice, or other obligations and who will satisfy them.
5. Attach evidence in `docs/provenance/` (for example, the license text, upstream terms, or a written permission), reference it from the record, and have a maintainer review it before adoption. Do not put copyrighted source, weights, or dataset contents in the evidence folder unless redistribution is approved.
6. Recheck material terms when changing versions, moving from development use to distribution, or preparing a release. Update the record and project notices at the same time.

If any origin, license, or right is unknown, mark the item **UNVERIFIED — DO NOT USE OR REDISTRIBUTE**. Keep it out of source, builds, tests, prompts, and release artifacts until resolved. Record rejections or removals when they materially affect prior builds.

## Inventory status

The tables below are starter inventories. An empty inventory means no item has been recorded and approved here; it is not a claim that a full dependency or asset audit has been completed.

### Third-party source code

Track source incorporated into the repository, generated from third-party source, or patched/vendored. Record the applicable license for the exact code and any required notices.

| Name / component | Upstream and exact revision | Repository path | License / notice evidence | Use and modification rights | Redistribution rights / obligations | SHA-256 (if vendored) | Review status / date |
| --- | --- | --- | --- | --- | --- | --- | --- |
| _None recorded_ | | | | | | | |

### Model implementations

Track software that implements or runs a model separately from its parameters. Include source repository, code revision, supported runtime, license, and whether linking, modification, and distribution are permitted. This category does not grant rights to any model weights or training data.

| Name | Upstream and exact revision | Implementation/runtime | License / notice evidence | Use and modification rights | Redistribution rights / obligations | SHA-256 (if vendored) | Review status / date |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Practical-RIFE v4.26 `IFNet_HDv3` model implementation | `hzwer/Practical-RIFE`; current source HEAD checked 2026-10-01: `bbfd2ea90910789a860ea3e2b32a240cd577b75e`; exact v4.26 model entry revision: `ec6aba965312eb9fd08301436145c8ac47ec545a` | Upstream PyTorch network is the offline reference; the live implementation is project-authored Objective-C++/MPSGraph/Metal | MIT; [`docs/provenance/licenses/Practical-RIFE-MIT.txt`](docs/provenance/licenses/Practical-RIFE-MIT.txt) | Porting/modification permitted | Distribution permitted with MIT copyright and license notice; no upstream Python runtime is shipped | n/a; reference source stays under ignored `models/local/` | Rights verified for this scope, 2026-10-01 |

### Model weights

Track each checkpoint or parameter file independently, including its author, release/version, source, checksum, model-specific license or terms, permitted use, and redistribution rights. Distinguish weights authored by an upstream project from weights produced by this project. Do not check in or bundle weights while their rights are unresolved.

| Model / file | Author and exact release | Source and SHA-256 | License / terms evidence | Use rights and restrictions | Redistribution rights / obligations | Intended delivery | Review status / date |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Practical-RIFE v4.26 source checkpoint `flownet.pkl` | hzwer; v4.26 model entry dated 2024-09-21 | [Official Google Drive archive](https://drive.google.com/file/d/1gViYvvQrtETBgU1w8axZSsr7YUuw31uy/view?usp=sharing); checkpoint SHA-256 `45c7f74156704769dc9f85cfcaf8552e1e926f9399dcfa3a553dee88fac6f53f`; archive SHA-256 `c2452dd2b244947d4be580156bbead60d6b72af5736860f7d6b3f99648c9c4cc` | Upstream README at [`ec6aba965312eb9fd08301436145c8ac47ec545a`](https://github.com/hzwer/Practical-RIFE/commit/ec6aba965312eb9fd08301436145c8ac47ec545a) says trained-model links use project MIT; copy at [`docs/provenance/licenses/Practical-RIFE-MIT.txt`](docs/provenance/licenses/Practical-RIFE-MIT.txt) | Use and modification permitted under MIT | Redistribution permitted with MIT copyright and license notice | Downloaded for local reference and conversion; not committed | Rights verified, 2026-10-01 |
| FrameGen converted Practical-RIFE v4.26 weights (`rife-v4.26.fgweights`) | Derived from the upstream checkpoint above using `tools/rife/convert_checkpoint.py` v1.0.0 | Local ignored file; SHA-256 `26f43050da2e4ec186a491099536ebdf82886e3213a66443aeb9a326031c7b53`; converter script SHA-256 `94d1f0db4f3c67910f90ef460046a23ca630479922f294aa446f7cfcae8fa14c` | Converted parameters retain model values; upstream states linked model contents use MIT | Conversion and use permitted under MIT | Redistribution permitted with MIT copyright and license notice | `models/local/rife-v4.26/`, ignored by Git | Rights verified, 2026-10-01 |

### Training datasets

Track each dataset, subset, or material training corpus separately. Record original source, version, collection method where relevant, license and consent/usage terms, privacy or attribution conditions, and rights to train on and redistribute the data. A model license does not establish dataset rights.

| Dataset / subset | Provider and exact version | Source / acquisition record | License, consent, and terms evidence | Training/use rights | Redistribution rights / obligations | Data location and checksum/manifest | Review status / date |
| --- | --- | --- | --- | --- | --- | --- | --- |
| _None recorded_ | | | | | | | |

### Runtime dependencies

Track build-time and runtime packages, frameworks, tools, and dynamically fetched components. Include transitive components that are bundled or shipped, identify static/dynamic/linking mode where relevant, and record notices and redistribution obligations. System-provided Apple frameworks should be identified as such when used.

| Name / component | Provider and exact version | Source / package identifier | Build/runtime role and linkage | License / notice evidence | Use rights | Redistribution rights / obligations | Bundled or fetched? | Review status / date |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Foundation | Apple macOS SDK 27.0 | System framework from the active Command Line Tools SDK | Error reporting and Objective-C runtime support; dynamically linked by the Metal backend and test host | Apple developer-tools and macOS platform terms supplied with the SDK | Use as a system framework under Apple terms | Not copied into the repository or bundled as a framework; the OS supplies it | System-provided | Used in bootstrap; recheck Apple terms before distribution |
| Metal | Apple macOS SDK 27.0 | System framework from the active Command Line Tools SDK | GPU resource, queue, shader, and event APIs; dynamically linked | Apple developer-tools and macOS platform terms supplied with the SDK | Use as a system framework under Apple terms | Not copied into the repository or bundled as a framework; the OS supplies it | System-provided | Used in bootstrap; recheck Apple terms before distribution |
| MetalPerformanceShaders | Apple macOS SDK 27.0 | System framework from the active Command Line Tools SDK | MPS command-buffer integration used by MPSGraph; dynamically linked | Apple developer-tools and macOS platform terms supplied with the SDK | Use as a system framework under Apple terms | Not copied into the repository or bundled as a framework; the OS supplies it | System-provided | Used by the RIFE Metal backend; recheck Apple terms before distribution |
| MetalPerformanceShadersGraph | Apple macOS SDK 27.0 | System framework from the active Command Line Tools SDK | Shape-specialized RIFE convolution graphs; dynamically linked | Apple developer-tools and macOS platform terms supplied with the SDK | Use as a system framework under Apple terms | Not copied into the repository or bundled as a framework; the OS supplies it | System-provided | Used by the RIFE Metal backend; recheck Apple terms before distribution |
| AppKit | Apple macOS SDK 27.0 | System framework from the active Command Line Tools SDK | Native window for the test host; dynamically linked | Apple developer-tools and macOS platform terms supplied with the SDK | Use as a system framework under Apple terms | Not copied into the repository or bundled as a framework; the OS supplies it | System-provided | Used in test host; recheck Apple terms before distribution |
| MetalKit | Apple macOS SDK 27.0 | System framework from the active Command Line Tools SDK | `MTKView` presentation in the test host; dynamically linked | Apple developer-tools and macOS platform terms supplied with the SDK | Use as a system framework under Apple terms | Not copied into the repository or bundled as a framework; the OS supplies it | System-provided | Used in test host; recheck Apple terms before distribution |
| QuartzCore | Apple macOS SDK 27.0 | System framework from the active Command Line Tools SDK | Drawable/presentation integration for the test host; dynamically linked | Apple developer-tools and macOS platform terms supplied with the SDK | Use as a system framework under Apple terms | Not copied into the repository or bundled as a framework; the OS supplies it | System-provided | Used in test host; recheck Apple terms before distribution |

## Build tools used in bootstrap verification

These are development tools only; they are not runtime dependencies and are not bundled in the application. Versions below record the local verification environment, not a project-wide pin.

| Name / component | Provider and exact version | Source / package identifier | Build role | License / notice evidence | Redistribution status | Review status |
| --- | --- | --- | --- | --- | --- | --- |
| Apple Clang | Apple Clang 21.0.0.21000334 | Apple Command Line Tools; `/Library/Developer/CommandLineTools` | C++23 and Objective-C++ compiler | Apple developer-tools terms supplied with Command Line Tools | Not redistributed by the project | Used for bootstrap verification |
| macOS SDK | 27.0 | Apple Command Line Tools; `/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk` | System headers, frameworks, and linker stubs | Apple developer-tools terms supplied with Command Line Tools | Not redistributed by the project | Used for bootstrap verification |
| CMake | 4.4.3 | Homebrew formula `cmake` | Project configuration and build generation | BSD-3-Clause in the Homebrew formula | Not bundled by the project | Used for bootstrap verification |
| Ninja | 1.13.2 | Homebrew formula `ninja` | Native build execution | Apache-2.0 in the Homebrew formula | Not bundled by the project | Used for bootstrap verification |

The full Xcode application, `xcodebuild`, the standalone `metal` compiler, and GPTK were not used in this step. No renderer adapter is implemented.

## Boundary-specific notes

- No Lossless Scaling binary, model, weight, private interface, reverse-engineered behavior, or derived material is an acceptable project dependency or source. Lossless Scaling is a high-level UX reference only. See [`CONTRIBUTING.md`](CONTRIBUTING.md).
- The core must remain independent of Highball, Wine, D3DMetal, DXMT, and DXVK. Renderer and compatibility-layer integrations belong in adapters and must use documented/public mechanisms.
- An Apache-2.0 license on original code does not relicense third-party material and does not authorize redistribution of third-party models, weights, datasets, or runtime assets.

## Record template

Copy this template into the relevant table and replace every placeholder. Use `UNVERIFIED — DO NOT USE OR REDISTRIBUTE` until the evidence and rights review are complete.

```text
Name / exact version or revision:
Category:
Upstream author / organization:
Canonical source URL or package identifier:
Date checked:
Repository path, delivery location, or intended use:
Checksum (SHA-256 or stronger, for fixed artifacts):
License and exact terms source:
Evidence stored at:
Use rights (including modification, linking, execution, or training as applicable):
Redistribution rights and obligations:
Attribution, notices, source-offer, consent, or other conditions:
Build/runtime delivery surface:
Maintainer reviewer and review date:
Status: UNVERIFIED — DO NOT USE OR REDISTRIBUTE | APPROVED (scope below) | REJECTED
Approval scope / notes:
```
