#!/usr/bin/env python3
"""Generate claudit_icon.png — a blue->purple gradient shield with a magnifying glass
and a red alert. Pure Pillow, no network. Run: python3 scripts/gen-icon.py"""
import os
from PIL import Image, ImageDraw

S = 1024          # final size
SS = 2            # supersample for smooth edges
W = S * SS
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "claudit_icon.png")


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def gen():
    img = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # vertical blue -> purple gradient, masked to the shield silhouette
    top, bot = (74, 163, 255), (139, 92, 246)
    grad = Image.new("RGBA", (W, W))
    gd = ImageDraw.Draw(grad)
    for y in range(W):
        gd.line([(0, y), (W, y)], fill=lerp(top, bot, y / W) + (255,))
    sx = lambda p: (int(p[0] * SS), int(p[1] * SS))
    shield = [sx(p) for p in [(512, 150), (842, 250), (842, 520), (700, 762),
                              (512, 884), (324, 762), (182, 520), (182, 250)]]
    mask = Image.new("L", (W, W), 0)
    ImageDraw.Draw(mask).polygon(shield, fill=255)
    img.paste(grad, (0, 0), mask)

    # darker inner bevel ring on the shield edge
    d.line(shield + [shield[0]], fill=(20, 24, 60, 150), width=10 * SS, joint="curve")

    # magnifying glass: lens ring + handle
    cx, cy, r = 472 * SS, 452 * SS, 150 * SS
    ring = 34 * SS
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(238, 243, 255, 255), width=ring)
    # glass tint
    d.ellipse([cx - r + ring, cy - r + ring, cx + r - ring, cy + r - ring], fill=(210, 226, 255, 70))
    # handle
    hx, hy = cx + int(r * 0.72), cy + int(r * 0.72)
    d.line([(hx, hy), (690 * SS, 700 * SS)], fill=(238, 243, 255, 255), width=46 * SS)
    d.ellipse([(690 * SS) - 24 * SS, (700 * SS) - 24 * SS,
               (690 * SS) + 24 * SS, (700 * SS) + 24 * SS], fill=(238, 243, 255, 255))

    # red alert exclamation inside the lens
    ec = (239, 68, 68, 255)
    d.rounded_rectangle([cx - 16 * SS, cy - 70 * SS, cx + 16 * SS, cy + 28 * SS],
                        radius=14 * SS, fill=ec)
    d.ellipse([cx - 18 * SS, cy + 44 * SS, cx + 18 * SS, cy + 80 * SS], fill=ec)

    img = img.resize((S, S), Image.LANCZOS)
    img.save(OUT)
    img.resize((256, 256), Image.LANCZOS).save(OUT.replace(".png", "_256.png"))
    print("wrote", OUT)


if __name__ == "__main__":
    gen()
