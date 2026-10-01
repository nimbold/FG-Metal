# Practical-RIFE v4.26 checkpoint conversion

`convert_checkpoint.py` is an offline converter for the Practical-RIFE v4.26
`flownet.pkl` checkpoint. It does not download weights or import the upstream
model code. Upstream lists the v4.26 checkpoint on its [trained-model page](https://github.com/hzwer/Practical-RIFE#trained-model)
and states that the model-link contents use the project MIT license. The source
implementation is also MIT licensed. The upstream repository revision that
lists this checkpoint is [`ec6aba965312eb9fd08301436145c8ac47ec545a`](https://github.com/hzwer/Practical-RIFE/tree/ec6aba965312eb9fd08301436145c8ac47ec545a)
(the checkpoint is distributed through the model link rather than a Git tag).
The checkpoint remains external to this repository; the repository ignores
local model assets under `models/local/`.

## Download and extract separately

Obtain the checkpoint from the official [v4.26 Google Drive entry](https://drive.google.com/file/d/1gViYvvQrtETBgU1w8axZSsr7YUuw31uy/view).
For example, with `gdown` already installed:

```sh
mkdir -p models/local/rife-v4.26
gdown 1gViYvvQrtETBgU1w8axZSsr7YUuw31uy \
  -O /tmp/RIFEv4.26_0921.zip
unzip /tmp/RIFEv4.26_0921.zip -d models/local/rife-v4.26/upstream
```

Keep the downloaded archive and extracted `flownet.pkl` outside Git. The
converter accepts either the `flownet.pkl` path itself or an extracted
directory containing exactly one `flownet.pkl`.

## Convert

Install Python, PyTorch, and NumPy in an offline conversion environment. For
example, in a disposable environment:

```sh
python3 -m venv /tmp/rife-convert
/tmp/rife-convert/bin/python -m pip install torch numpy
```

The converter records the exact Python, PyTorch, and NumPy versions used, as
well as the checkpoint, converter script, and output SHA-256 values, in a JSON
sidecar. It uses PyTorch's restricted `weights_only=True` loader and requires
the complete known v4.26 key and shape schema. The official checkpoint also
contains 30 `teacher.*` and 10 `caltime.*` training-only tensors; the converter
validates their complete names, shapes, dtype, and finite values before
excluding them from the 158-tensor inference container. The JSON sidecar lists
all 40 excluded keys and explains why. Any other missing or extra tensor is an
error.

```sh
python3 tools/rife/convert_checkpoint.py \
  --checkpoint models/local/rife-v4.26/upstream \
  --out models/local/rife-v4.26/rife-v4.26.fgweights
```

This writes `models/local/rife-v4.26/rife-v4.26.fgweights` and its JSON
sidecar. Both are generated local artifacts and remain ignored by Git. The
Metal backend searches upward from its working directory and executable for
that path, so the bundled test host can find the local weights when launched
from Finder. Set `FRAMEGEN_MODEL_WEIGHTS` to an absolute or
working-directory-relative path when storing them elsewhere. Conversion fails closed for unknown or
missing keys, shape or dtype mismatches, duplicate normalized names,
unrecognized checkpoint entries, and non-finite values.

The tensor values retain PyTorch's original dimension order, serialized as
contiguous little-endian float32. Conv2d and ConvTranspose2d weights therefore
retain their PyTorch layouts; consumers must interpret those dimensions
accordingly.

## Deterministic reference input and comparison

Generate the project-authored 64x64 pair and the 65x63 mod-64 edge-padding
pair with the Python standard library (no external image package or random
seed is involved):

```sh
python3 tools/rife/generate_reference_fixture.py
```

The pinned CPU reference runner loads the official archive's `IFNet_HDv3.py`
and the `model/warplayer.py` saved under ignored `models/local/`. It verifies
their SHA-256 values and the checkpoint SHA-256 before loading, uses PyTorch's
restricted `weights_only=True` checkpoint loader, then runs the five inference
stages with timestep 0.5:

```sh
models/local/rife-v4.26/venv/bin/python tools/rife/reference_inference.py
```

The output is written to the ignored
`models/local/rife-v4.26/reference-output.ppm`. To compare a P6 PPM emitted by
the standalone Metal host:

```sh
models/local/rife-v4.26/venv/bin/python tools/rife/compare_ppm.py \
  models/local/rife-v4.26/reference-output.ppm \
models/local/rife-v4.26/metal-output.ppm
```

To create the reference for the 65x63 edge-padding pair, pass its paths
explicitly. The reference pads by replicating the final row/column to a
multiple of 64, then crops the generated output back to 65x63:

```sh
models/local/rife-v4.26/venv/bin/python tools/rife/reference_inference.py \
  --frame-a tests/fixtures/rife-v4.26/frame_a_edge.ppm \
  --frame-b tests/fixtures/rife-v4.26/frame_b_edge.ppm \
  --out models/local/rife-v4.26/reference-output-edge.ppm
```

The comparator reports absolute error MAE, linear-interpolated p95, and max
over all 8-bit RGB samples; PSNR uses peak 255 and MSE over all RGB samples.
SSIM is the channel-averaged Wang form with 11x11 Gaussian windows (sigma
1.5), valid windows, population moments, and C1/C2 constants based on 255. For
identical images it prints JSON `null` for PSNR (mathematically infinite) and
SSIM 1.0. This comparison uses quantized PPM output; the native runtime may
also expose float output for more detailed numerical analysis.

Verified CPU reference output for the 64x64 fixture pair: SHA-256
`9e6cf18371ec2ffe7d580077e34c066389cc75c8ff5c9cee62769d62cf0bd689`. On the
Apple M3 Metal host, QUALITY matched the CPU output with MAE 0.0000814, p95 0,
max 1, PSNR 89.026 dB, and SSIM 0.999999982. The 65x63 edge-padded pair also
matched within max 1. BALANCED uses full-resolution float16 and has its own
measured numerical result. Exact fixture hashes, outputs, tolerance, and
benchmark links are recorded in [RIFE provenance](../../docs/provenance/rife-v4.26.md).
