from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw

root = Path(__file__).parent / "run-02"
w, h = 1280, 720

def source(seq):
    raw = np.fromfile(root / f"source-sequence-{seq}.raw", dtype=np.uint8).reshape(h, w, 4)
    rgb = raw[..., [2, 1, 0]]
    return Image.fromarray(rgb, "RGB")

m = np.fromfile(root / "metafx-output-pair-3-4.rgba16f", dtype=np.float16).reshape(h, w, 4).astype(np.float32)
print("M rgba min/max", np.nanmin(m, axis=(0, 1)), np.nanmax(m, axis=(0, 1)), "finite", np.isfinite(m).all())
# Harness SDR range; clamp only for an 8-bit diagnostic view.
m_rgb = np.clip(m[..., :3], 0.0, 1.0)
m_image = Image.fromarray(np.round(m_rgb * 255).astype(np.uint8), "RGB")
m_image.save(root / "metafx-output-pair-3-4.png")
panels = [source(3), m_image, source(4)]
canvas = Image.new("RGB", (w * 3, h + 40), (20, 20, 20))
draw = ImageDraw.Draw(canvas)
for i, (label, panel) in enumerate(zip(("Previous source · seq 3", "MetalFX midpoint · constant depth", "Current source · seq 4"), panels)):
    canvas.paste(panel, (i * w, 40))
    draw.text((i * w + 12, 10), label, fill=(255, 255, 255))
canvas.save(root / "constant-depth-pair-3-4-visual.png")
