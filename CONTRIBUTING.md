# Contributing

Thanks for helping build a renderer-integrated frame-generation library for macOS. The project is intended to be independently designed, with a core that consumes and produces opaque GPU resources and renderer-specific adapters kept outside that core.

## Clean-room and provenance boundaries

All contributions must be independently developed from first-party work and sources that are public and authorized for the intended use. Use documented Apple Metal, QuartzCore, macOS SDK, Wine, GPTK, and renderer integration mechanisms where applicable. Do not infer hidden behavior from an implementation that is unavailable for public use.

**Do not incorporate, inspect, reconstruct, imitate, or depend on proprietary Lossless Scaling (LSFG) implementation material.** This prohibition includes, without limitation:

- `lsfg-vk.dll`, `lsfg-metal`, any other Lossless Scaling binaries, and private LSFG interfaces;
- Lossless Scaling source, disassembly, decompiler output, traces, protocol descriptions, or reverse-engineered behavior;
- Lossless Scaling models, model weights, or material derived from them;
- code, pseudocode, data, design details, or behavioral specifications copied or inferred from any of the above; and
- old Highball LSFG integration as an implementation specification.

Lossless Scaling may be discussed only as a high-level product and user-experience reference. That reference may inform general UX goals, such as user controls or presentation expectations; it must not be used to derive internal algorithms, interfaces, data formats, timing rules, or implementation behavior.

If you encounter potentially restricted material or cannot establish the origin and rights for a contribution, stop work on the affected part and notify the maintainers. Do not paste it into an issue, commit, test fixture, prompt, or this repository. Maintainers will keep it out of the project unless its provenance and rights are independently resolved.

## Third-party material

Before proposing third-party code, a model implementation, weights, training data, or a runtime dependency, document it in the matching inventory in [`PROVENANCE.md`](PROVENANCE.md). Record the exact upstream, version or revision, applicable license and notices, intended use, and evidence that redistribution and any required model/data use are permitted. A public download URL alone does not establish a license or redistribution right.

Do not add material whose license, origin, or redistribution terms are unknown or incompatible with the project. Keep separately licensed components clearly identified and preserve required notices. Do not commit downloaded model weights or datasets unless maintainers have approved their provenance and redistribution terms.

## Submitting a change

For each change:

1. Describe the user-visible or architectural reason for it and the affected areas.
2. Identify newly introduced external material and update the provenance inventory before or with the change.
3. Keep renderer, Wine, and vendor-specific code in adapters; the reusable core must not acquire those dependencies.
4. State what you verified and what remains unverified. Do not imply a GPU, packaged-app, or renderer runtime check passed if it was not run.
5. Keep the change focused and include relevant tests or reproducible verification steps when appropriate.

By submitting a contribution, you confirm that you have the right to submit it under the project license and that it complies with these boundaries. Original project code is offered under Apache-2.0; contributions must be compatible with that license and must not introduce undisclosed third-party restrictions.
