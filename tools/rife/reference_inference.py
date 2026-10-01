#!/usr/bin/env python3
"""Run the official Practical-RIFE v4.26 CPU reference on a PPM frame pair."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import sys
import types
from pathlib import Path

import numpy as np
import torch

from convert_checkpoint import (
    expected_schema,
    load_state_dict,
    normalize_state,
    validate_and_convert,
)
from ppm_utils import read_ppm, write_ppm


MODEL_VERSION = "4.26"
UPSTREAM_COMMIT = "ec6aba965312eb9fd08301436145c8ac47ec545a"
WARPPLAYER_COMMIT = "bbfd2ea90910789a860ea3e2b32a240cd577b75e"
EXPECTED_CHECKPOINT_SHA256 = "45c7f74156704769dc9f85cfcaf8552e1e926f9399dcfa3a553dee88fac6f53f"
EXPECTED_IFNET_SHA256 = "655b4c772b037967b86c2dd31c8fa3b5323b79dd9a0e0088708d89149bbc8a32"
EXPECTED_WARPLAYER_SHA256 = "feb3af9475b724749b023d1913c3b135431eff13d0045b8efb472a699058385f"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_hash(path: Path, expected: str, label: str) -> None:
    actual = sha256_file(path)
    if actual != expected:
        raise ValueError(f"{label} SHA-256 mismatch: expected {expected}, got {actual} ({path})")


def default_paths() -> dict[str, Path]:
    root = Path(__file__).resolve().parents[2]
    local = root / "models" / "local" / "rife-v4.26"
    fixture = root / "tests" / "fixtures" / "rife-v4.26"
    return {
        "checkpoint": local / "upstream" / "flownet.pkl",
        "ifnet": local / "upstream" / "IFNet_HDv3.py",
        "warplayer": local / "reference" / "model" / "warplayer.py",
        "frame_a": fixture / "frame_a.ppm",
        "frame_b": fixture / "frame_b.ppm",
        "out": local / "reference-output.ppm",
    }


def load_official_ifnet(ifnet_path: Path, warplayer_path: Path):
    # IFNet_HDv3.py imports `model.warplayer`. Bind that exact local file as a
    # package module, rather than accepting another installed `model` package.
    model_package = types.ModuleType("model")
    model_package.__path__ = [str(warplayer_path.parent)]  # type: ignore[attr-defined]
    sys.modules["model"] = model_package
    spec = importlib.util.spec_from_file_location("rife_v426_official_ifnet", ifnet_path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot import official IFNet source: {ifnet_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.device = torch.device("cpu")
    return module.IFNet


def ppm_tensor(path: Path) -> torch.Tensor:
    width, height, raw = read_ppm(path)
    image = np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3).copy()
    tensor = torch.from_numpy(image).permute(2, 0, 1).unsqueeze(0).to(dtype=torch.float32).div_(255.0)
    pad_right = (-width) % 64
    pad_bottom = (-height) % 64
    if pad_right or pad_bottom:
        tensor = torch.nn.functional.pad(
            tensor, (0, pad_right, 0, pad_bottom), mode="replicate")
    return tensor


def main() -> int:
    paths = default_paths()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", type=Path, default=paths["checkpoint"])
    parser.add_argument("--ifnet", type=Path, default=paths["ifnet"])
    parser.add_argument("--warplayer", type=Path, default=paths["warplayer"])
    parser.add_argument("--frame-a", type=Path, default=paths["frame_a"])
    parser.add_argument("--frame-b", type=Path, default=paths["frame_b"])
    parser.add_argument("--out", type=Path, default=paths["out"])
    args = parser.parse_args()

    try:
        require_hash(args.checkpoint, EXPECTED_CHECKPOINT_SHA256, "official checkpoint")
        require_hash(args.ifnet, EXPECTED_IFNET_SHA256, "official IFNet_HDv3.py")
        require_hash(args.warplayer, EXPECTED_WARPLAYER_SHA256, "pinned official warplayer.py")

        checkpoint_bytes = args.checkpoint.read_bytes()
        raw_state = load_state_dict(checkpoint_bytes)
        # Shares the converter's exact allowlist and shape checks; this safely
        # verifies all 158 inference tensors and all 40 known excluded tensors.
        _, excluded_keys = validate_and_convert(raw_state)
        state = normalize_state(raw_state)
        inference_names = set(expected_schema())
        inference_state = {name: state[name] for name in expected_schema()}
        if set(inference_state) != inference_names:
            raise ValueError("validated inference state does not match the v4.26 schema")

        torch.set_num_threads(1)
        torch.use_deterministic_algorithms(True)
        IFNet = load_official_ifnet(args.ifnet, args.warplayer)
        model = IFNet().cpu().eval()
        model.load_state_dict(inference_state, strict=True)

        width0, height0, _ = read_ppm(args.frame_a)
        width1, height1, _ = read_ppm(args.frame_b)
        if (width0, height0) != (width1, height1):
            raise ValueError("reference input frame dimensions must match")
        img0 = ppm_tensor(args.frame_a)
        img1 = ppm_tensor(args.frame_b)
        with torch.inference_mode():
            _, _, merged = model(
                torch.cat((img0, img1), dim=1),
                timestep=0.5,
                scale_list=[16, 8, 4, 2, 1],
            )
            result = merged[-1][0, :, :height0, :width0]
            result = result.clamp(0.0, 1.0).mul(255.0).round().to(torch.uint8)
            rgb = result.permute(1, 2, 0).contiguous().numpy().tobytes()

        write_ppm(args.out, width0, height0, rgb)
        print(f"wrote {args.out} ({width0}x{height0}, Practical-RIFE v{MODEL_VERSION}, t=0.5, stages=5, CPU)")
        print(f"checkpoint_sha256={EXPECTED_CHECKPOINT_SHA256}")
        print(f"ifnet_sha256={EXPECTED_IFNET_SHA256}")
        print(f"warplayer_sha256={EXPECTED_WARPLAYER_SHA256} (upstream {WARPPLAYER_COMMIT})")
        print(f"excluded_training_tensors={len(excluded_keys)}")
        print(f"output_sha256={sha256_file(args.out)}")
        print(f"torch_version={torch.__version__} numpy_version={np.__version__}")
        return 0
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        print(f"reference_inference: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
