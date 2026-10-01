"""Project-authored analytic scene used as rights-safe benchmark ground truth."""

from __future__ import annotations

import math

WIDTH = 128
HEIGHT = 72


def _blank():
    image = bytearray(WIDTH * HEIGHT * 3)
    for y in range(HEIGHT):
        for x in range(WIDTH):
            offset = (y * WIDTH + x) * 3
            horizon = 36
            if y < horizon:
                shade = 30 + y // 4
                image[offset:offset + 3] = bytes((shade, shade + 12, shade + 26))
            else:
                tile = ((x // 16) + (y // 12)) & 1
                image[offset:offset + 3] = bytes((25 + tile * 6, 40 + tile * 8, 26 + tile * 3))
    return image


def _set(image, x, y, color):
    if 0 <= x < WIDTH and 0 <= y < HEIGHT:
        offset = (y * WIDTH + x) * 3
        image[offset:offset + 3] = bytes(color)


def _rect(image, x0, y0, x1, y1, color):
    for y in range(max(0, y0), min(HEIGHT, y1)):
        for x in range(max(0, x0), min(WIDTH, x1)):
            _set(image, x, y, color)


def _line(image, x0, y0, x1, y1, color):
    dx, dy = abs(x1 - x0), abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    error = dx - dy
    while True:
        _set(image, x0, y0, color)
        if x0 == x1 and y0 == y1:
            break
        twice = 2 * error
        if twice > -dy:
            error -= dy
            x0 += sx
        if twice < dx:
            error += dx
            y0 += sy


def _disk(image, cx, cy, radius, color):
    for y in range(cy - radius, cy + radius + 1):
        for x in range(cx - radius, cx + radius + 1):
            if (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2:
                _set(image, x, y, color)


_FONT = {
    "0": ("111", "101", "101", "101", "111"), "1": ("010", "110", "010", "010", "111"),
    "2": ("111", "001", "111", "100", "111"), "3": ("111", "001", "111", "001", "111"),
    "4": ("101", "101", "111", "001", "001"), "5": ("111", "100", "111", "001", "111"),
    "6": ("111", "100", "111", "101", "111"), "7": ("111", "001", "010", "010", "010"),
    "8": ("111", "101", "111", "101", "111"), "9": ("111", "101", "111", "001", "111"),
    "A": ("010", "101", "111", "101", "101"), "C": ("111", "100", "100", "100", "111"),
    "E": ("111", "100", "110", "100", "111"), "H": ("101", "101", "111", "101", "101"),
    "I": ("111", "010", "010", "010", "111"), "L": ("100", "100", "100", "100", "111"),
    "O": ("111", "101", "101", "101", "111"), "R": ("110", "101", "110", "101", "101"),
    "S": ("111", "100", "111", "001", "111"), "T": ("111", "010", "010", "010", "010"),
    "U": ("101", "101", "101", "101", "111"), " ": ("000",) * 5,
}


def _text(image, mask, x, y, text, color):
    origin = x
    for char in text.upper():
        glyph = _FONT.get(char, _FONT[" "])
        for gy, row in enumerate(glyph):
            for gx, bit in enumerate(row):
                if bit == "1":
                    px, py = x + gx, y + gy
                    _set(image, px, py, color)
                    if 0 <= px < WIDTH and 0 <= py < HEIGHT:
                        mask[py * WIDTH + px] = 255
        x += 4
    return x - origin


def render(time_frame: float):
    """Render exact ground truth at a continuous high-rate frame coordinate."""
    image = _blank()
    hud = bytearray(WIDTH * HEIGHT)
    text_mask = bytearray(WIDTH * HEIGHT)
    occlusion = bytearray(WIDTH * HEIGHT)

    pan = int(round(time_frame * 1.2))
    # Thin skyline and distant foliage, translated by an analytic camera pan.
    for base_x in range(-16, WIDTH + 24, 24):
        x = (base_x - pan) % (WIDTH + 40) - 20
        _line(image, x, 38, x + 7, 24, (43, 86, 45))
        for leaf in range(4):
            lx = x + (leaf % 2) * 5 - 2
            ly = 23 + leaf * 3
            _disk(image, lx, ly, 3, (38 + leaf * 2, 102, 43 + leaf * 3))

    # Fence / thin geometry behind the moving character.
    for x in range(-10, WIDTH + 12, 9):
        sx = x - pan // 2
        _line(image, sx, 31, sx, 51, (139, 151, 125))
    for y in (34, 43, 50):
        _line(image, 0, y, WIDTH - 1, y + 1, (112, 130, 112))

    # Specular rail with a moving highlight.
    _line(image, 0, 54, WIDTH - 1, 54, (78, 87, 91))
    highlight_x = int((time_frame * 5.0) % WIDTH)
    _line(image, highlight_x - 5, 53, highlight_x + 5, 53, (230, 220, 164))

    # Third-person silhouette moving through the scene.
    character_x = 41 + int(round(math.sin(time_frame * 0.12) * 10 + time_frame * 0.35))
    _disk(image, character_x, 35, 4, (196, 146, 98))
    _rect(image, character_x - 4, 39, character_x + 5, 51, (62, 91, 156))
    _line(image, character_x - 2, 50, character_x - 5, 59, (34, 37, 44))
    _line(image, character_x + 3, 50, character_x + 6, 59, (34, 37, 44))
    _line(image, character_x + 3, 42, character_x + 10, 47, (43, 45, 51))
    _line(image, character_x + 10, 47, character_x + 19, 44, (151, 159, 163))

    # A translucent panel and a moving foreground occluder edge.
    panel_x = 87
    _rect(image, panel_x, 29, 111, 55, (48, 74, 60))
    for y in range(29, 55):
        for x in range(panel_x, 111):
            offset = (y * WIDTH + x) * 3
            old = image[offset:offset + 3]
            image[offset:offset + 3] = bytes((int(old[i] * 0.55 + (45, 120, 95)[i] * 0.45) for i in range(3)))
    edge_x = int(round(86 + (time_frame * 2.5) % 30))
    _rect(image, edge_x, 28, edge_x + 5, 56, (23, 32, 35))
    for y in range(28, 56):
        for x in range(max(0, edge_x - 3), min(WIDTH, edge_x + 8)):
            occlusion[y * WIDTH + x] = 255

    # Intermittent particles; deterministic time-space pattern.
    for particle in range(8):
        px = int((particle * 19 + time_frame * (2 + particle % 3) * 2) % WIDTH)
        py = 24 + ((particle * 13 + int(time_frame * 3)) % 37)
        _set(image, px, py, (232, 218, 144))

    # HUD backing, crosshair, weapon sight, minimap, health bar, subtitle,
    # and a rapidly changing counter. HUD/text masks intentionally overlap.
    _rect(image, 3, 3, 39, 16, (22, 29, 33))
    _rect(image, 91, 3, 124, 22, (20, 30, 36))
    _rect(image, 5, 62, 42, 69, (18, 22, 28))
    for y in range(3, 16):
        for x in range(3, 39): hud[y * WIDTH + x] = 255
    for y in range(3, 22):
        for x in range(91, 124): hud[y * WIDTH + x] = 255
    for y in range(62, 69):
        for x in range(5, 42): hud[y * WIDTH + x] = 255

    _text(image, text_mask, 7, 5, "HP", (244, 236, 205))
    _text(image, text_mask, 20, 5, f"{(93 - int(time_frame * 7)) % 100:02d}", (250, 225, 104))
    # Minimap grid and player marker.
    for x in (96, 104, 112, 120): _line(image, x, 5, x, 20, (58, 91, 87))
    for y in (8, 13, 18): _line(image, 93, y, 122, y, (58, 91, 87))
    _disk(image, 106 + int(math.sin(time_frame * 0.3) * 3), 12, 2, (230, 87, 72))
    # Health bar and crosshair / weapon sight geometry.
    _rect(image, 7, 12, 34, 14, (56, 61, 59))
    _rect(image, 7, 12, 7 + max(2, 26 - int(time_frame) % 8), 14, (77, 197, 105))
    cross_x, cross_y = WIDTH // 2, 38
    _line(image, cross_x - 4, cross_y, cross_x + 4, cross_y, (238, 239, 222))
    _line(image, cross_x, cross_y - 4, cross_x, cross_y + 4, (238, 239, 222))
    for y in range(cross_y - 5, cross_y + 6):
        for x in range(cross_x - 5, cross_x + 6): hud[y * WIDTH + x] = 255
    _text(image, text_mask, 8, 64, "CLEAR", (224, 228, 211))

    # A subtle vignette makes color and edge changes easier to inspect.
    return {
        "rgb": bytes(image),
        "masks": {
            "hud": bytes(hud),
            "text": bytes(text_mask),
            "scene": bytes(255 if hud[i] == 0 else 0 for i in range(WIDTH * HEIGHT)),
            "occlusion": bytes(occlusion),
        },
    }
