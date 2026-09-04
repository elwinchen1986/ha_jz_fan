#!/usr/bin/env python3
"""Render brand icons from icon.svg into PNG files.

Uses headless Chrome to rasterize icon.svg (which references system CJK fonts
for the "京造" text) into:
- icon.png     (256x256)  required by HA brands
- icon@2x.png  (512x512)  hi-dpi

Run:  python3 generate_icon.py
"""
import os
import subprocess
import sys

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def render(svg_path, out_path, size):
    cmd = [
        CHROME,
        "--headless",
        "--disable-gpu",
        "--force-device-scale-factor=1",
        "--default-background-color=00000000",  # transparent background
        f"--screenshot={out_path}",
        f"--window-size={size},{size}",
        f"file://{svg_path}",
    ]
    subprocess.run(cmd, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"  Saved: {out_path} ({size}x{size}, {os.path.getsize(out_path)} bytes)")


def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    svg = os.path.join(script_dir, "icon.svg")

    if not os.path.exists(svg):
        sys.exit(f"icon.svg not found at {svg}")
    if not os.path.exists(CHROME):
        sys.exit("Google Chrome not found; install it or use another SVG rasterizer.")

    for size, name in [(256, "icon.png"), (512, "icon@2x.png")]:
        out = os.path.join(script_dir, name)
        print(f"Rendering {name} ({size}x{size})...")
        render(svg, out, size)

    print("Done!")


if __name__ == "__main__":
    main()