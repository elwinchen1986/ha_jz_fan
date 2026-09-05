#!/usr/bin/env python3
"""Render brand icons using Pillow (precise, no browser needed).

Draws a rounded blue square that fills the whole canvas, a three-blade fan
centered in the upper area, and the "京造" text below. Outputs:
- icon.png     (256x256)  required by HA brands
- icon@2x.png  (512x512)  hi-dpi

Everything is drawn at a large supersampled size then downscaled for smooth
edges. Run:  python3 generate_icon.py
"""
import math
import os

from PIL import Image, ImageDraw, ImageFont

# Work at 1024 then downscale for anti-aliasing
SS = 1024

BG_TOP = (46, 127, 224)     # #2E7FE0
BG_BOTTOM = (22, 87, 200)   # #1657C8
BLADE = (245, 250, 255)     # near white
HUB_FILL = (22, 87, 200)    # #1657C8
WHITE = (255, 255, 255)


def _vertical_gradient(size, top, bottom):
    grad = Image.new("RGB", (1, size), 0)
    for y in range(size):
        t = y / (size - 1)
        r = int(top[0] + (bottom[0] - top[0]) * t)
        g = int(top[1] + (bottom[1] - top[1]) * t)
        b = int(top[2] + (bottom[2] - top[2]) * t)
        grad.putpixel((0, y), (r, g, b))
    return grad.resize((size, size))


def _rounded_mask(size, radius):
    mask = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return mask


def _blade_path(cx, cy, length, width):
    """Return polygon points for one teardrop-ish blade pointing up."""
    pts = []
    steps = 24
    # outer curve (right side going up)
    for i in range(steps + 1):
        t = i / steps
        # widen then taper
        w = width * math.sin(math.pi * t) * (1 - 0.15 * t)
        x = cx + w
        y = cy - length * t
        pts.append((x, y))
    # tip rounding handled implicitly; come back along left side
    for i in range(steps, -1, -1):
        t = i / steps
        w = width * math.sin(math.pi * t) * (1 - 0.15 * t)
        x = cx - w
        y = cy - length * t
        pts.append((x, y))
    return pts


def _find_cjk_font(px):
    candidates = [
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/Library/Fonts/Songti.ttc",
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, px)
            except Exception:
                continue
    return ImageFont.load_default()


def render_master():
    img = Image.new("RGBA", (SS, SS), (0, 0, 0, 0))

    # Background: gradient clipped to rounded rect
    grad = _vertical_gradient(SS, BG_TOP, BG_BOTTOM).convert("RGBA")
    mask = _rounded_mask(SS, radius=int(SS * 0.22))
    img.paste(grad, (0, 0), mask)

    draw = ImageDraw.Draw(img)

    # Fan center (upper-center)
    fcx, fcy = SS * 0.5, SS * 0.42
    length = SS * 0.30
    width = SS * 0.085

    for angle in (0, 120, 240):
        base = _blade_path(0, 0, length, width)
        rad = math.radians(angle)
        cosr, sinr = math.cos(rad), math.sin(rad)
        rotated = [
            (fcx + x * cosr - y * sinr, fcy + x * sinr + y * cosr)
            for (x, y) in base
        ]
        draw.polygon(rotated, fill=BLADE)

    # Hub
    hr = SS * 0.072
    draw.ellipse([fcx - hr, fcy - hr, fcx + hr, fcy + hr], fill=HUB_FILL)
    draw.ellipse([fcx - hr, fcy - hr, fcx + hr, fcy + hr],
                 outline=WHITE, width=int(SS * 0.016))
    ir = SS * 0.024
    draw.ellipse([fcx - ir, fcy - ir, fcx + ir, fcy + ir], fill=WHITE)

    # Text "京造"
    font = _find_cjk_font(int(SS * 0.22))
    text = "京造"
    tb = draw.textbbox((0, 0), text, font=font)
    tw, th = tb[2] - tb[0], tb[3] - tb[1]
    tx = (SS - tw) / 2 - tb[0]
    ty = SS * 0.72 - th / 2 - tb[1]
    draw.text((tx, ty), text, font=font, fill=WHITE)

    return img


def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    master = render_master()
    for size, name in [(256, "icon.png"), (512, "icon@2x.png")]:
        out = os.path.join(script_dir, name)
        master.resize((size, size), Image.LANCZOS).save(out)
        print(f"  Saved: {out} ({size}x{size}, {os.path.getsize(out)} bytes)")
    print("Done!")


if __name__ == "__main__":
    main()