"""Build the Crate Digger vinyl launcher icon (PNG + multi-size ICO).

Run from project root:
    python scripts/build_app_icon.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw

ASSETS = ROOT / "assets"
PNG_PATH = ASSETS / "crate-digger-256.png"
ICO_PATH = ASSETS / "crate-digger.ico"

# Crate Digger palette (ui/theme.py)
VINYL_BASE = "#12100D"
VINYL_EDGE = "#0A0908"
GROOVE_A = "#1C1914"
GROOVE_B = "#262118"
LABEL_FILL = "#C89028"
LABEL_RING = "#E0A832"
LABEL_SHADOW = "#7A5818"
ACCENT_ORANGE = "#D4652A"
SPINDLE = "#050504"
SHINE = (237, 229, 216, 38)  # warm cream, low alpha
CRATE_LINE = "#EDE5D8"


def _hex_rgb(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return tuple(int(color[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def draw_vinyl_icon(size: int) -> Image.Image:
    """Top-down vinyl record: dark grooves, amber label, orange accent."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    cx = cy = size / 2.0
    margin = max(1, int(size * 0.03))
    r_outer = size / 2.0 - margin

    # Drop shadow
    shadow_r = r_outer + max(1, size // 64)
    draw.ellipse(
        [cx - shadow_r, cy - shadow_r + size * 0.03, cx + shadow_r, cy + shadow_r + size * 0.03],
        fill=(0, 0, 0, 90),
    )

    # Vinyl body
    draw.ellipse(
        [cx - r_outer, cy - r_outer, cx + r_outer, cy + r_outer],
        fill=_hex_rgb(VINYL_BASE),
        outline=_hex_rgb(VINYL_EDGE),
        width=max(1, size // 80),
    )

    # Groove rings
    groove_count = max(6, int(size / 18))
    inner_ratio = 0.42
    for i in range(groove_count, 0, -1):
        t = i / groove_count
        r = r_outer * (inner_ratio + (1.0 - inner_ratio) * t)
        color = GROOVE_A if i % 2 else GROOVE_B
        width = max(1, int(size / 96))
        draw.ellipse(
            [cx - r, cy - r, cx + r, cy + r],
            outline=_hex_rgb(color),
            width=width,
        )

    # Specular shine arc across the vinyl
    shine = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    shine_draw = ImageDraw.Draw(shine)
    shine_w = max(2, int(size * 0.09))
    shine_r = r_outer * 0.92
    bbox = [cx - shine_r, cy - shine_r, cx + shine_r, cy + shine_r]
    shine_draw.arc(bbox, start=300, end=40, fill=SHINE, width=shine_w)
    img = Image.alpha_composite(img, shine)

    # Center label
    r_label = r_outer * 0.36
    draw = ImageDraw.Draw(img)
    draw.ellipse(
        [cx - r_label, cy - r_label, cx + r_label, cy + r_label],
        fill=_hex_rgb(LABEL_FILL),
        outline=_hex_rgb(LABEL_RING),
        width=max(1, size // 64),
    )

    # Label inner ring (pressed label edge)
    r_inner = r_label * 0.78
    draw.ellipse(
        [cx - r_inner, cy - r_inner, cx + r_inner, cy + r_inner],
        outline=_hex_rgb(LABEL_SHADOW),
        width=max(1, size // 128),
    )

    # Crate-slat lines on the label (visible from 32px+)
    if size >= 32:
        line_w = max(1, int(size / 48))
        span = r_label * 0.55
        offsets = (-0.14, 0.0, 0.14) if size >= 64 else (0.0,)
        for offset in offsets:
            y = cy + r_label * offset
            draw.line(
                [(cx - span, y), (cx + span, y)],
                fill=_hex_rgb(CRATE_LINE),
                width=line_w,
            )

    # Orange accent ring (MPC LED glow)
    r_accent = max(size * 0.055, 3.0)
    draw.ellipse(
        [cx - r_accent, cy - r_accent, cx + r_accent, cy + r_accent],
        outline=_hex_rgb(ACCENT_ORANGE),
        width=max(1, size // 72),
    )

    # Spindle hole
    r_hole = max(1.5, size * 0.035)
    draw.ellipse(
        [cx - r_hole, cy - r_hole, cx + r_hole, cy + r_hole],
        fill=_hex_rgb(SPINDLE),
    )

    # Tiny highlight on label (vinyl sticker sheen)
    if size >= 48:
        hx = cx - r_label * 0.22
        hy = cy - r_label * 0.28
        hr = max(1.0, size * 0.04)
        draw.ellipse(
            [hx - hr, hy - hr, hx + hr, hy + hr],
            fill=(255, 248, 235, 70),
        )

    return img


def build_icon_assets() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    sizes = [16, 24, 32, 48, 64, 128, 256]
    images: list[Image.Image] = []

    master = draw_vinyl_icon(256)
    master.save(PNG_PATH, format="PNG")

    for size in sizes:
        if size == 256:
            images.append(master.copy())
        else:
            images.append(draw_vinyl_icon(size))

    images[0].save(
        ICO_PATH,
        format="ICO",
        sizes=[(img.width, img.height) for img in images],
        append_images=images[1:],
    )

    print(f"Wrote {PNG_PATH}")
    print(f"Wrote {ICO_PATH} ({', '.join(str(s) for s in sizes)}px)")


if __name__ == "__main__":
    build_icon_assets()
