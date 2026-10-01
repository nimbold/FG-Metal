#!/usr/bin/env python3
"""Convert a separately obtained Practical-RIFE v4.26 checkpoint to .fgweights.

This tool is intentionally offline: it never downloads a checkpoint or imports
the upstream model's Python implementation. Checkpoints are loaded with
PyTorch's restricted ``weights_only`` unpickler and checked against the known
v4.26 state-dict schema before serialization.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import struct
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import BinaryIO

import numpy as np
import torch


SCRIPT_VERSION = "1.0.0"
MODEL_NAME = "Practical-RIFE"
MODEL_VERSION = "4.26"
ARCHITECTURE = "IFNet_HDv3 encoded five-block model"
UPSTREAM_REPOSITORY = "https://github.com/hzwer/Practical-RIFE"
UPSTREAM_MODEL_LIST = "https://github.com/hzwer/Practical-RIFE#trained-model"
UPSTREAM_COMMIT = "ec6aba965312eb9fd08301436145c8ac47ec545a"
UPSTREAM_SOURCE_LICENSE = "MIT"
CHECKPOINT_SOURCE = (
    "https://drive.google.com/file/d/1gViYvvQrtETBgU1w8axZSsr7YUuw31uy/view"
)
CHECKPOINT_LICENSE = "MIT (the upstream model list states trained-model links use the project MIT license)"
MAGIC = b"FGRWGT1\0"
DTYPE_F32 = 1
EXPECTED_TENSOR_COUNT = 158
EXPECTED_CHECKPOINT_TENSOR_COUNT = 198
EXPECTED_EXCLUDED_TENSOR_COUNT = 40


class ConversionError(Exception):
    """An input checkpoint cannot be safely converted as Practical-RIFE v4.26."""


def expected_schema() -> dict[str, tuple[int, ...]]:
    """Return the complete v4.26 state-dict schema in canonical write order."""
    schema: dict[str, tuple[int, ...]] = {}

    # Encoder head: three Conv2d layers followed by ConvTranspose2d.
    encoder = (
        ("encode.cnn0", (16, 3, 3, 3), (16,)),
        ("encode.cnn1", (16, 16, 3, 3), (16,)),
        ("encode.cnn2", (16, 16, 3, 3), (16,)),
        ("encode.cnn3", (16, 4, 4, 4), (4,)),
    )
    for name, weight_shape, bias_shape in encoder:
        schema[f"{name}.weight"] = weight_shape
        schema[f"{name}.bias"] = bias_shape

    channels = (192, 128, 96, 64, 32)
    block_input_channels = (15, 28, 28, 28, 28)
    for block, (width, input_channels) in enumerate(
        zip(channels, block_input_channels, strict=True)
    ):
        prefix = f"block{block}"
        schema[f"{prefix}.conv0.0.0.weight"] = (width // 2, input_channels, 3, 3)
        schema[f"{prefix}.conv0.0.0.bias"] = (width // 2,)
        schema[f"{prefix}.conv0.1.0.weight"] = (width, width // 2, 3, 3)
        schema[f"{prefix}.conv0.1.0.bias"] = (width,)

        for layer in range(8):
            layer_prefix = f"{prefix}.convblock.{layer}"
            schema[f"{layer_prefix}.conv.weight"] = (width, width, 3, 3)
            schema[f"{layer_prefix}.conv.bias"] = (width,)
            schema[f"{layer_prefix}.beta"] = (1, width, 1, 1)

        # PyTorch ConvTranspose2d stores weights as [in, out, kernel_h, kernel_w].
        schema[f"{prefix}.lastconv.0.weight"] = (width, 52, 4, 4)
        schema[f"{prefix}.lastconv.0.bias"] = (52,)

    if len(schema) != EXPECTED_TENSOR_COUNT:
        raise AssertionError(f"internal schema error: expected {EXPECTED_TENSOR_COUNT} entries, got {len(schema)}")
    return schema


def expected_training_only_schema() -> dict[str, tuple[int, ...]]:
    """Return the exact teacher/caltime tensors present in the official checkpoint.

    These tensors are saved in flownet.pkl but are not read by the v4.26
    inference forward path. Keep this allowlist explicit so unrelated extras
    remain a hard error.
    """
    schema: dict[str, tuple[int, ...]] = {
        "teacher.conv0.0.0.weight": (32, 31, 3, 3),
        "teacher.conv0.0.0.bias": (32,),
        "teacher.conv0.1.0.weight": (64, 32, 3, 3),
        "teacher.conv0.1.0.bias": (64,),
    }
    for layer in range(8):
        prefix = f"teacher.convblock.{layer}"
        schema[f"{prefix}.conv.weight"] = (64, 64, 3, 3)
        schema[f"{prefix}.conv.bias"] = (64,)
        schema[f"{prefix}.beta"] = (1, 64, 1, 1)
    schema["teacher.lastconv.0.weight"] = (64, 52, 4, 4)
    schema["teacher.lastconv.0.bias"] = (52,)

    caltime_layers = (
        (0, (32, 17, 3, 3), (32,)),
        (2, (64, 32, 3, 3), (64,)),
        (4, (64, 64, 3, 3), (64,)),
        (6, (64, 64, 3, 3), (64,)),
        (8, (1, 64, 3, 3), (1,)),
    )
    for layer, weight_shape, bias_shape in caltime_layers:
        schema[f"caltime.{layer}.weight"] = weight_shape
        schema[f"caltime.{layer}.bias"] = bias_shape

    if len(schema) != EXPECTED_EXCLUDED_TENSOR_COUNT:
        raise AssertionError(
            f"internal training-only schema error: expected {EXPECTED_EXCLUDED_TENSOR_COUNT} entries, got {len(schema)}"
        )
    return schema


def resolve_checkpoint(path: Path) -> Path:
    """Accept flownet.pkl directly or a directory extracted from the official archive."""
    if path.is_file():
        if path.name != "flownet.pkl":
            raise ConversionError(f"checkpoint file must be named flownet.pkl: {path}")
        return path
    if not path.is_dir():
        raise ConversionError(f"checkpoint path does not exist or is not a file/directory: {path}")

    matches = sorted(candidate for candidate in path.rglob("flownet.pkl") if candidate.is_file())
    if len(matches) != 1:
        if not matches:
            raise ConversionError(f"no flownet.pkl found under extracted archive directory: {path}")
        found = ", ".join(str(item) for item in matches[:5])
        suffix = " ..." if len(matches) > 5 else ""
        raise ConversionError(f"expected exactly one flownet.pkl under {path}; found {len(matches)}: {found}{suffix}")
    return matches[0]


def load_state_dict(checkpoint_bytes: bytes) -> Mapping[str, object]:
    """Load only plain tensor state dictionaries; never fall back to unsafe pickle loading."""
    try:
        loaded = torch.load(
            io.BytesIO(checkpoint_bytes),
            map_location="cpu",
            weights_only=True,
        )
    except TypeError as exc:
        raise ConversionError(
            "installed PyTorch does not support torch.load(weights_only=True); use a supported PyTorch release"
        ) from exc
    except Exception as exc:
        raise ConversionError(f"PyTorch could not safely load the checkpoint: {exc}") from exc

    if not isinstance(loaded, Mapping):
        raise ConversionError(f"checkpoint root must be a state-dict mapping, got {type(loaded).__name__}")

    # Some common exporters wrap a state dict. Accept only the wrapper itself,
    # so optimizer/training state cannot be silently ignored.
    if "state_dict" in loaded and isinstance(loaded["state_dict"], Mapping):
        if set(loaded) != {"state_dict"}:
            extras = sorted(str(key) for key in loaded if key != "state_dict")
            raise ConversionError(f"unsupported checkpoint wrapper entries: {extras}")
        loaded = loaded["state_dict"]
    return loaded


def normalize_state(state: Mapping[str, object]) -> dict[str, torch.Tensor]:
    normalized: dict[str, torch.Tensor] = {}
    original_names: dict[str, str] = {}
    for raw_name, value in state.items():
        if not isinstance(raw_name, str):
            raise ConversionError(f"state-dict key must be text, got {type(raw_name).__name__}")
        name = raw_name[7:] if raw_name.startswith("module.") else raw_name
        if name.startswith("module."):
            raise ConversionError(f"unsupported repeated DataParallel prefix in key {raw_name!r}")
        if name in normalized:
            raise ConversionError(
                f"duplicate normalized state-dict key {name!r} from {original_names[name]!r} and {raw_name!r}"
            )
        if not isinstance(value, torch.Tensor):
            raise ConversionError(f"unsupported state-dict value for {raw_name!r}: expected a tensor")
        if value.device.type != "cpu":
            raise ConversionError(f"checkpoint tensor {raw_name!r} was not loaded onto CPU")
        if value.dtype != torch.float32:
            raise ConversionError(f"unsupported dtype for {raw_name!r}: expected float32, got {value.dtype}")
        normalized[name] = value
        original_names[name] = raw_name
    return normalized


def validate_and_convert(
    state: Mapping[str, object],
) -> tuple[list[tuple[str, np.ndarray]], list[str]]:
    tensors = normalize_state(state)
    schema = expected_schema()
    training_only_schema = expected_training_only_schema()
    actual_names = set(tensors)
    expected_names = set(schema)
    expected_training_only_names = set(training_only_schema)
    allowed_names = expected_names | expected_training_only_names

    missing = sorted(expected_names - actual_names)
    missing_training_only = sorted(expected_training_only_names - actual_names)
    unexpected = sorted(actual_names - allowed_names)
    if missing or missing_training_only or unexpected:
        sections = []
        if missing:
            sections.append(f"missing keys ({len(missing)}): {', '.join(missing[:8])}{' ...' if len(missing) > 8 else ''}")
        if missing_training_only:
            sections.append(
                f"missing expected training-only keys ({len(missing_training_only)}): "
                f"{', '.join(missing_training_only[:8])}{' ...' if len(missing_training_only) > 8 else ''}"
            )
        if unexpected:
            sections.append(f"unsupported keys ({len(unexpected)}): {', '.join(unexpected[:8])}{' ...' if len(unexpected) > 8 else ''}")
        raise ConversionError("checkpoint keys do not match Practical-RIFE v4.26: " + "; ".join(sections))

    if len(tensors) != EXPECTED_CHECKPOINT_TENSOR_COUNT:
        raise ConversionError(
            f"expected {EXPECTED_CHECKPOINT_TENSOR_COUNT} checkpoint tensors, got {len(tensors)}"
        )

    converted: list[tuple[str, np.ndarray]] = []
    for name, expected_shape in schema.items():
        tensor = tensors[name]
        actual_shape = tuple(int(dimension) for dimension in tensor.shape)
        if actual_shape != expected_shape:
            raise ConversionError(f"shape mismatch for {name!r}: expected {expected_shape}, got {actual_shape}")
        if tensor.ndim < 1 or tensor.ndim > 255:
            raise ConversionError(f"unsupported tensor rank for {name!r}: {tensor.ndim}")
        if any(dimension <= 0 or dimension > 0xFFFFFFFF for dimension in actual_shape):
            raise ConversionError(f"invalid or unrepresentable dimensions for {name!r}: {actual_shape}")

        array = np.asarray(tensor.detach().contiguous().numpy(), dtype="<f4", order="C")
        if not np.isfinite(array).all():
            raise ConversionError(f"non-finite values found in tensor {name!r}")
        encoded_name = name.encode("utf-8")
        if len(encoded_name) > 0xFFFF:
            raise ConversionError(f"tensor name is too long for container format: {name!r}")
        converted.append((name, array))

    excluded_keys: list[str] = []
    for name, expected_shape in training_only_schema.items():
        tensor = tensors[name]
        actual_shape = tuple(int(dimension) for dimension in tensor.shape)
        if actual_shape != expected_shape:
            raise ConversionError(
                f"shape mismatch for excluded training-only tensor {name!r}: "
                f"expected {expected_shape}, got {actual_shape}"
            )
        if not torch.isfinite(tensor).all().item():
            raise ConversionError(f"non-finite values found in excluded training-only tensor {name!r}")
        excluded_keys.append(name)

    if len(converted) != EXPECTED_TENSOR_COUNT:
        raise ConversionError(f"expected {EXPECTED_TENSOR_COUNT} tensors, got {len(converted)}")
    if len(excluded_keys) != EXPECTED_EXCLUDED_TENSOR_COUNT:
        raise ConversionError(
            f"expected {EXPECTED_EXCLUDED_TENSOR_COUNT} excluded training-only tensors, got {len(excluded_keys)}"
        )
    return converted, excluded_keys


def write_bytes(stream: BinaryIO, digest: "hashlib._Hash", data: bytes) -> None:
    stream.write(data)
    digest.update(data)


def write_container(path: Path, tensors: list[tuple[str, np.ndarray]]) -> str:
    if len(tensors) > 0xFFFFFFFF:
        raise ConversionError("tensor count cannot be represented in the container header")
    digest = hashlib.sha256()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as output:
        write_bytes(output, digest, MAGIC)
        write_bytes(output, digest, struct.pack("<I", len(tensors)))
        for name, array in tensors:
            encoded_name = name.encode("utf-8")
            shape = tuple(int(value) for value in array.shape)
            if len(shape) > 255 or len(encoded_name) > 0xFFFF:
                raise ConversionError(f"tensor metadata cannot be represented for {name!r}")
            write_bytes(output, digest, struct.pack("<HBB", len(encoded_name), len(shape), DTYPE_F32))
            if shape:
                write_bytes(output, digest, struct.pack("<" + "I" * len(shape), *shape))
            write_bytes(output, digest, encoded_name)
            write_bytes(output, digest, array.tobytes(order="C"))
        output.flush()
        os.fsync(output.fileno())
    return digest.hexdigest()


def atomic_json(path: Path, document: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def script_sha256() -> str:
    return hashlib.sha256(Path(__file__).resolve().read_bytes()).hexdigest()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert an official Practical-RIFE v4.26 flownet.pkl to the portable FGRWGT1 format."
    )
    parser.add_argument(
        "--checkpoint",
        required=True,
        type=Path,
        help="separately downloaded flownet.pkl or directory containing exactly one extracted flownet.pkl",
    )
    parser.add_argument("--out", required=True, type=Path, help="output path ending in .fgweights")
    parser.add_argument(
        "--checkpoint-source",
        default=CHECKPOINT_SOURCE,
        help="source URL or source description to record in the JSON sidecar",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.out.suffix != ".fgweights":
            raise ConversionError("output path must end in .fgweights")
        checkpoint = resolve_checkpoint(args.checkpoint)
        if checkpoint.resolve() == args.out.resolve():
            raise ConversionError("input checkpoint and output path must be different files")
        sidecar_path = Path(str(args.out) + ".json")
        if sidecar_path.resolve() == checkpoint.resolve():
            raise ConversionError("sidecar path would overwrite the input checkpoint")

        # Hash and load the same byte sequence so the sidecar identifies exactly
        # the checkpoint that was validated and converted.
        checkpoint_bytes = checkpoint.read_bytes()
        input_sha = hashlib.sha256(checkpoint_bytes).hexdigest()
        state = load_state_dict(checkpoint_bytes)
        tensors, excluded_keys = validate_and_convert(state)

        args.out.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=f".{args.out.name}.", suffix=".tmp", dir=args.out.parent)
        os.close(fd)
        temporary_output = Path(temporary_name)
        try:
            output_sha = write_container(temporary_output, tensors)
            os.replace(temporary_output, args.out)
        except BaseException:
            try:
                temporary_output.unlink()
            except FileNotFoundError:
                pass
            raise

        manifest: dict[str, object] = {
            "format": {"name": "FGRWGT1", "version": 1, "tensor_count": len(tensors)},
            "excluded_checkpoint_entries": {
                "count": len(excluded_keys),
                "keys": excluded_keys,
                "key_normalization": "listed after removal of one leading module. prefix when present",
                "rationale": (
                    "The official v4.26 checkpoint contains a teacher network and caltime layers used for "
                    "training/time-calibration support, but not read by the IFNet_HDv3 inference forward path. "
                    "Their exact names, shapes, float32 dtype, and finite values are validated before exclusion."
                ),
            },
            "model": {
                "name": MODEL_NAME,
                "architecture": ARCHITECTURE,
                "version": MODEL_VERSION,
                "upstream_repository": UPSTREAM_REPOSITORY,
                "upstream_commit": UPSTREAM_COMMIT,
                "upstream_model_list": UPSTREAM_MODEL_LIST,
                "source_license": UPSTREAM_SOURCE_LICENSE,
            },
            "checkpoint": {
                "source": args.checkpoint_source,
                "weight_license": CHECKPOINT_LICENSE,
                "input_path": str(checkpoint.resolve()),
                "input_sha256": input_sha,
            },
            "output": {
                "path": str(args.out.resolve()),
                "output_sha256": output_sha,
                "weight_layout": "original PyTorch tensor dimension order; contiguous little-endian float32",
            },
            "conversion": {
                "converter_script": str(Path(__file__).resolve()),
                "converter_script_version": SCRIPT_VERSION,
                "converter_script_sha256": script_sha256(),
                "python_version": platform.python_version(),
                "torch_version": str(torch.__version__),
                "numpy_version": str(np.__version__),
                "device": "CPU",
            },
        }
        atomic_json(sidecar_path, manifest)
        print(f"wrote {args.out} ({len(tensors)} tensors; sha256 {output_sha})")
        print(f"wrote {sidecar_path}")
        return 0
    except (ConversionError, OSError, RuntimeError, ValueError, struct.error) as exc:
        print(f"convert_checkpoint: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
