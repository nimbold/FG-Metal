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


def _mask_rect(mask, x0, y0, x1, y1):
    for y in range(max(0, y0), min(HEIGHT, y1)):
        for x in range(max(0, x0), min(WIDTH, x1)):
            mask[y * WIDTH + x] = 255


def _line(image, x0, y0, x1, y1, color, mask=None):
    dx, dy = abs(x1 - x0), abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    error = dx - dy
    while True:
        _set(image, x0, y0, color)
        if mask is not None and 0 <= x0 < WIDTH and 0 <= y0 < HEIGHT:
            mask[y0 * WIDTH + x0] = 255
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
    "M": ("101", "111", "111", "101", "101"), "N": ("101", "111", "111", "111", "101"),
    "O": ("111", "101", "101", "101", "111"), "R": ("110", "101", "110", "101", "101"),
    "S": ("111", "100", "111", "001", "111"), "T": ("111", "010", "010", "010", "010"),
    "U": ("101", "101", "101", "101", "111"), " ": ("000",) * 5,
}


def _text(image, mask, x, y, text, color, secondary_mask=None):
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
                        if secondary_mask is not None:
                            secondary_mask[py * WIDTH + px] = 255
        x += 4
    return x - origin


def _blend_rect(image, x0, y0, x1, y1, color, alpha):
    for y in range(max(0, y0), min(HEIGHT, y1)):
        for x in range(max(0, x0), min(WIDTH, x1)):
            offset = (y * WIDTH + x) * 3
            image[offset:offset + 3] = bytes(
                int(round(image[offset + channel] * (1.0 - alpha) + color[channel] * alpha))
                for channel in range(3))


def _rotate_world(image, masks, angle):
    """Apply an analytic camera-roll proxy to world pixels, keeping the HUD fixed."""
    source = bytes(image)
    rotated = _blank()
    source_masks = [bytes(mask) for mask in masks]
    rotated_masks = [bytearray(WIDTH * HEIGHT) for _ in source_masks]
    center_x, center_y = (WIDTH - 1) * 0.5, (HEIGHT - 1) * 0.5
    cosine, sine = math.cos(angle), math.sin(angle)
    for y in range(HEIGHT):
        dy = y - center_y
        for x in range(WIDTH):
            dx = x - center_x
            source_x = int(round(center_x + cosine * dx + sine * dy))
            source_y = int(round(center_y - sine * dx + cosine * dy))
            if not (0 <= source_x < WIDTH and 0 <= source_y < HEIGHT):
                continue
            target_offset = (y * WIDTH + x) * 3
            source_offset = (source_y * WIDTH + source_x) * 3
            rotated[target_offset:target_offset + 3] = source[source_offset:source_offset + 3]
            source_pixel = source_y * WIDTH + source_x
            target_pixel = y * WIDTH + x
            for source_mask, target_mask in zip(source_masks, rotated_masks):
                target_mask[target_pixel] = source_mask[source_pixel]
    image[:] = rotated
    for mask, rotated_mask in zip(masks, rotated_masks):
        mask[:] = rotated_mask


def render(time_frame: float):
    """Render exact ground truth at a continuous high-rate frame coordinate."""
    image = _blank()
    hud = bytearray(WIDTH * HEIGHT)
    text_mask = bytearray(WIDTH * HEIGHT)
    occlusion = bytearray(WIDTH * HEIGHT)
    thin_geometry = bytearray(WIDTH * HEIGHT)
    critical_masks = {label: bytearray(WIDTH * HEIGHT) for label in (
        "crosshair", "crosshair_pixels", "static_text_pixels", "weapon_sight", "minimap", "health_bar",
        "subtitle", "hud_counter", "timer", "scrolling_text", "flashing_ui",
        "transparent_ui", "moving_menu")}

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
        _line(image, sx, 31, sx, 51, (139, 151, 125), thin_geometry)
    for y in (34, 43, 50):
        _line(image, 0, y, WIDTH - 1, y + 1, (112, 130, 112), thin_geometry)

    # Specular rail with a moving highlight.
    _line(image, 0, 54, WIDTH - 1, 54, (78, 87, 91), thin_geometry)
    highlight_x = int((time_frame * 5.0) % WIDTH)
    _line(image, highlight_x - 5, 53, highlight_x + 5, 53, (230, 220, 164), thin_geometry)

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

    # The world rolls quickly while all screen-space HUD layers remain fixed.
    # This gives the benchmark true high-angular-velocity edges rather than
    # labeling a plain translation as camera rotation.
    _rotate_world(image, (occlusion, thin_geometry), time_frame * 0.08)

    # HUD backing, crosshair, weapon sight, minimap, health bar, subtitle,
    # timer, scrolling text, flashing state, transparent UI, and a moving menu.
    # HUD/text masks intentionally overlap.
    _rect(image, 3, 3, 39, 16, (22, 29, 33))
    _rect(image, 91, 3, 124, 22, (20, 30, 36))
    _rect(image, 5, 62, 42, 69, (18, 22, 28))
    _rect(image, 47, 2, 83, 12, (20, 27, 31))
    for y in range(3, 16):
        for x in range(3, 39): hud[y * WIDTH + x] = 255
    for y in range(3, 22):
        for x in range(91, 124): hud[y * WIDTH + x] = 255
    for y in range(62, 69):
        for x in range(5, 42): hud[y * WIDTH + x] = 255
    for y in range(2, 12):
        for x in range(47, 83): hud[y * WIDTH + x] = 255

    _text(image, text_mask, 7, 5, "HP", (244, 236, 205))
    _text(image, text_mask, 20, 5, f"{(93 - int(time_frame * 7)) % 100:02d}", (250, 225, 104))
    _text(image, text_mask, 54, 4, f"{int(time_frame * 2) % 100:02d}", (238, 226, 183))
    # Minimap grid and player marker.
    for x in (96, 104, 112, 120): _line(image, x, 5, x, 20, (58, 91, 87))
    for y in (8, 13, 18): _line(image, 93, y, 122, y, (58, 91, 87))
    _disk(image, 106 + int(math.sin(time_frame * 0.3) * 3), 12, 2, (230, 87, 72))
    # Health bar and crosshair / weapon sight geometry.
    _rect(image, 7, 12, 34, 14, (56, 61, 59))
    _rect(image, 7, 12, 7 + max(2, 26 - int(time_frame) % 8), 14, (77, 197, 105))
    cross_x, cross_y = WIDTH // 2, 38
    _line(image, cross_x - 4, cross_y, cross_x + 4, cross_y,
          (238, 239, 222), critical_masks["crosshair_pixels"])
    _line(image, cross_x, cross_y - 4, cross_x, cross_y + 4,
          (238, 239, 222), critical_masks["crosshair_pixels"])
    for y in range(cross_y - 5, cross_y + 6):
        for x in range(cross_x - 5, cross_x + 6): hud[y * WIDTH + x] = 255
    _text(image, text_mask, 8, 64, "CLEAR", (224, 228, 211),
          critical_masks["static_text_pixels"])

    # Scroll through fixed screen coordinates, toggle a warning, and retain a
    # translucent screen-space panel so the temporal mask sees varied UI.
    scroll_x = 34 + int(round((time_frame * 4.0) % 52))
    _text(image, text_mask, scroll_x, 53, "ALERT", (246, 193, 92))
    _mask_rect(hud, 34, 52, 106, 60)
    _mask_rect(critical_masks["flashing_ui"], 47, 13, 61, 23)
    for y in range(13, 23):
        for x in range(47, 61): hud[y * WIDTH + x] = 255
    if int(time_frame * 2) % 2 == 0:
        _rect(image, 48, 14, 60, 22, (142, 43, 36))
        for y in range(14, 22):
            for x in range(48, 60): hud[y * WIDTH + x] = 255

    # A moving scene highlight passes underneath the translucent lower UI
    # panel. This exposes the composited-underlay ambiguity in automatic mode.
    underlay_x = 50 + int(round((time_frame * 3.0) % 30))
    _disk(image, underlay_x, 66, 2, (186, 137, 54))
    _blend_rect(image, 48, 61, 82, 71, (28, 102, 108), 0.52)
    _text(image, text_mask, 54, 63, "MENU", (223, 242, 227))
    for y in range(61, 71):
        for x in range(48, 82): hud[y * WIDTH + x] = 255

    menu_x = 62 + int(round((time_frame * 1.5) % 10))
    _blend_rect(image, menu_x, 13, menu_x + 20, 25, (32, 46, 72), 0.78)
    _text(image, text_mask, menu_x + 2, 16, "MENU", (242, 235, 210))
    for y in range(13, 25):
        for x in range(menu_x, menu_x + 20): hud[y * WIDTH + x] = 255

    # Keep gameplay-critical HUD components independently scorable. These
    # tight, overlapping ROIs let the regression tool detect damage moving
    # from a broad HUD region onto a crosshair or text element.
    _mask_rect(critical_masks["crosshair"], 59, 33, 70, 44)
    _mask_rect(critical_masks["weapon_sight"], 56, 30, 73, 47)
    _mask_rect(critical_masks["minimap"], 91, 3, 124, 22)
    _mask_rect(critical_masks["health_bar"], 7, 12, 34, 14)
    _mask_rect(critical_masks["subtitle"], 5, 62, 42, 69)
    _mask_rect(critical_masks["hud_counter"], 18, 3, 31, 11)
    _mask_rect(critical_masks["timer"], 47, 2, 83, 12)
    _mask_rect(critical_masks["scrolling_text"], 34, 52, 106, 60)
    _mask_rect(critical_masks["transparent_ui"], 48, 61, 82, 71)
    _mask_rect(critical_masks["moving_menu"], 62, 13, 92, 25)

    # A subtle vignette makes color and edge changes easier to inspect.
    return {
        "rgb": bytes(image),
        "masks": {
            "hud": bytes(hud),
            "text": bytes(text_mask),
            # Keep the moving scenery beneath the translucent lower HUD panel
            # in the scene ROI as well. This intentionally overlaps HUD so
            # composited-underlay regressions remain visible in scene metrics.
            "scene": bytes(
                255 if hud[i] == 0 or (
                    48 <= i % WIDTH < 82 and 61 <= i // WIDTH < 71
                ) else 0
                for i in range(WIDTH * HEIGHT)
            ),
            "occlusion": bytes(occlusion),
            "thin_geometry": bytes(thin_geometry),
            **{label: bytes(mask) for label, mask in critical_masks.items()},
        },
    }
