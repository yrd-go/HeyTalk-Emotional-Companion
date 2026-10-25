#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Generate the Heytalk icon, G4 generation.

Designs
-------
K1 "reef knot" (default): two strands -- blue on the left, orange on the
    right -- each bending into an oval loop; the two tails cross below the
    loops in one over-under weave (orange passes over blue). Reads as a
    knot: two voices woven together.
K2 "figure-eight knot": two-tone lemniscate, blue lobe left / orange lobe
    right, with an over/under crossing at the centre (blue over orange).
F  "two-colour firework": alternating blue/orange tapered rays + star tips
    around a navy hub.
F1 "blue firework, white hex": all rays in the site blue, flat white hex core.
F2 "blue firework, white circle": same mark with a flat white circular core.
F3 "blue/orange firework, white circle": source colours with a white core.
F4 "blue/gold firework, white circle": gold replaces orange, white core.
G1 "blue burst, globe core": F2 with a simplified geodesic badge.
G2 "blue burst, source emblem": the supplied geodesic artwork inside the core.
G3 "blue burst, rotated source emblem": same artwork rotated 15 degrees.
H1 "blue/white sharp burst": white accent rays, no tip dots, rotated emblem.
A  "navy tile": H1 on a dark navy rounded-square app-icon field.
B  "black rays": H1 with black accent rays on a transparent field.

Why geometry/vector instead of an AI bitmap
-------------------------------------------
- Apple HIG: one filled, recognizable silhouette that still reads at 16 px;
  AI rasters turn to noise below ~87 px and cannot be tuned per size.
- Reproducible and parametric: colours / geometry are constants here; every
  size derives from one 4x supersampled master.
- Matches the published state-of-the-art pipeline (CLIPasso SIGGRAPH 2022,
  VectorFusion CVPR 2023, IconShop SIGGRAPH Asia 2023): AI may propose a
  concept, but the production asset is vector.

Usage
-----
  python scripts/make_icon.py --preview           # previews only, no overwrite
  python scripts/make_icon.py --apply K1|F|F1|F2|F3|F4|G1|G2|G3|H1|A|B
"""
from __future__ import annotations

import argparse
import base64
import io
import math
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(PROJECT_ROOT), str(PROJECT_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import colorsys

from PIL import Image, ImageChops, ImageDraw, ImageFont

OUT_DIR = PROJECT_ROOT / "assets" / "icon"
REDESIGN = OUT_DIR / "redesign"
EMBLEM_SOURCE = OUT_DIR / "source" / "geodesic-emblem.png"
FAVICON = PROJECT_ROOT / "web" / "public" / "favicon.svg"
SIZES = (256, 128, 64, 48, 32, 16)
SS = 4  # supersampling factor (anti-aliasing)

# Gradients run light (top-left) -> deep (bottom-right).
BLUE_STOPS = ((0.00, (79, 168, 255)), (0.45, (24, 119, 242)), (1.00, (14, 95, 214)))
ORANGE_STOPS = ((0.00, (255, 192, 138)), (0.45, (255, 138, 42)), (1.00, (232, 93, 4)))
NAVY = (10, 38, 84)
WHITE = (255, 255, 255)
WORDMARK_TEXT = (24, 28, 34)

SNOW_BLUE = (23, 116, 238)
SNOW_WHITE = (255, 255, 255)


def _lerp(c0, c1, t):
    return tuple(round(a + (b - a) * t) for a, b in zip(c0, c1))


def _grad(t, stops):
    if t <= stops[0][0]:
        return stops[0][1]
    if t >= stops[-1][0]:
        return stops[-1][1]
    for (t0, c0), (t1, c1) in zip(stops, stops[1:]):
        if t0 <= t <= t1:
            return _lerp(c0, c1, (t - t0) / (t1 - t0))
    return stops[-1][1]


def _bezier(pts, steps):
    n = len(pts) - 1
    out = []
    for i in range(steps + 1):
        t = i / steps
        x = y = 0.0
        for k, (px, py) in enumerate(pts):
            b = math.comb(n, k) * (t ** k) * ((1 - t) ** (n - k))
            x += b * px
            y += b * py
        out.append((x, y))
    return out


def _band_mask(side, pts, width):
    """Filled band along a polyline with round caps; coords normalized 0..1."""
    mask = Image.new("L", (side, side), 0)
    d = ImageDraw.Draw(mask)
    w = max(3, int(round(width * side)))
    xy = [(x * side, y * side) for x, y in pts]
    d.line(xy, fill=255, width=w, joint="curve")
    r = w // 2
    for x, y in (xy[0], xy[-1]):
        d.ellipse([x - r, y - r, x + r, y + r], fill=255)
    return mask


def _ellipse_pts(c, rx, ry, rot, steps=128):
    out = []
    for i in range(steps):
        t = 2.0 * math.pi * i / steps
        x0, y0 = rx * math.cos(t), ry * math.sin(t)
        x = c[0] + x0 * math.cos(rot) - y0 * math.sin(rot)
        y = c[1] + x0 * math.sin(rot) + y0 * math.cos(rot)
        out.append((x, y))
    return out


def _ellipse_ring_mask(side, center, rx, ry, width, rot=0.0, steps=160):
    """Filled ellipse ring (annulus) rotated by `rot` radians."""
    outer = _ellipse_pts(center, rx, ry, rot, steps)
    inner = _ellipse_pts(center, max(rx - width, 0.005),
                         max(ry - width, 0.005), rot, steps)
    mask = Image.new("L", (side, side), 0)
    d = ImageDraw.Draw(mask)
    d.polygon([(x * side, y * side) for x, y in outer], fill=255)
    d.polygon([(x * side, y * side) for x, y in inner], fill=0)
    return mask


def _wedge_mask(side, center, angle, half, radius, steps=40):
    """Pie wedge from `center` (used to erase the under strand)."""
    mask = Image.new("L", (side, side), 0)
    d = ImageDraw.Draw(mask)
    pts = [(center[0] * side, center[1] * side)]
    for i in range(steps + 1):
        a = angle - half + 2.0 * half * i / steps
        pts.append(((center[0] + radius * math.cos(a)) * side,
                    (center[1] + radius * math.sin(a)) * side))
    d.polygon(pts, fill=255)
    return mask


def _gradient_image(side, stops, reverse=False):
    """Diagonal linear gradient RGBA image (16px source, bilinear upscale)."""
    tiny = Image.new("RGBA", (16, 16))
    denom = 2 * 16 - 2
    for y in range(16):
        for x in range(16):
            t = (x + y) / denom
            if reverse:
                t = 1.0 - t
            tiny.putpixel((x, y), _grad(t, stops) + (255,))
    return tiny.resize((side, side), Image.BILINEAR)


def _paint(mask, gradient):
    empty = Image.new("RGBA", mask.size, (0, 0, 0, 0))
    return Image.composite(gradient, empty, mask)


def _add(base, mask):
    return ImageChops.add(base, mask)


# ---------------------------------------------------------------- builders

K1_BAND = 0.115


def _build_k1(side):
    """K1 reef knot: blue loop left, orange loop right, tails cross below."""
    blue = Image.new("L", (side, side), 0)
    orange = Image.new("L", (side, side), 0)

    blue = _add(blue, _ellipse_ring_mask(
        side, (0.385, 0.425), 0.130, 0.165, K1_BAND, rot=0.18))
    blue = _add(blue, _band_mask(side, _bezier(
        ((0.140, 0.085), (0.280, 0.165), (0.360, 0.220), (0.414, 0.258)), 60),
        K1_BAND))
    blue = _add(blue, _band_mask(side, _bezier(
        ((0.357, 0.587), (0.420, 0.675), (0.500, 0.730), (0.680, 0.910)), 60),
        K1_BAND))

    orange = _add(orange, _ellipse_ring_mask(
        side, (0.615, 0.425), 0.130, 0.165, K1_BAND, rot=-0.18))
    orange = _add(orange, _band_mask(side, _bezier(
        ((0.860, 0.085), (0.720, 0.165), (0.640, 0.220), (0.586, 0.258)), 60),
        K1_BAND))

    # Crossing tail of the orange strand: navy rim underneath = over/under.
    tail = _bezier(((0.646, 0.587), (0.580, 0.675), (0.500, 0.730),
                    (0.320, 0.910)), 60)
    rim = _band_mask(side, tail, K1_BAND + 0.050)
    body = _band_mask(side, tail, K1_BAND)

    grad_b = _gradient_image(side, BLUE_STOPS)
    grad_o = _gradient_image(side, ORANGE_STOPS)
    img = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    img = Image.alpha_composite(img, _paint(blue, grad_b))
    img = Image.alpha_composite(img, _paint(rim, Image.new("RGBA", (side, side),
                                                           NAVY + (255,))))
    img = Image.alpha_composite(img, _paint(orange, grad_o))
    img = Image.alpha_composite(img, _paint(body, grad_o))
    return img


def _build_k2(side):
    """K2 figure-eight knot: two-tone lemniscate, blue over orange."""
    blue = _ellipse_ring_mask(side, (0.385, 0.500), 0.190, 0.190, 0.120)
    orange = _ellipse_ring_mask(side, (0.615, 0.500), 0.190, 0.190, 0.120)
    notch = _wedge_mask(side, (0.615, 0.500), math.pi, 0.42, 0.22)
    orange = ImageChops.subtract(orange, notch)

    grad_b = _gradient_image(side, BLUE_STOPS)
    grad_o = _gradient_image(side, ORANGE_STOPS)
    img = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    img = Image.alpha_composite(img, _paint(orange, grad_o))

    # Soft shadow where blue crosses over orange.
    sh = Image.new("L", (side, side), 0)
    d = ImageDraw.Draw(sh)
    d.ellipse([(0.450 * side), (0.470 * side), (0.540 * side), (0.540 * side)],
              fill=110)
    shadow = Image.new("RGBA", (side, side), (8, 26, 58, 255))
    img = Image.alpha_composite(img, Image.composite(shadow, Image.new(
        "RGBA", (side, side), (0, 0, 0, 0)), sh))

    img = Image.alpha_composite(img, _paint(blue, grad_b))

    # Centre dimple: the twist of the knot.
    dot = Image.new("L", (side, side), 0)
    dd = ImageDraw.Draw(dot)
    r = 0.035 * side
    dd.ellipse([0.5 * side - r, 0.5 * side - r, 0.5 * side + r, 0.5 * side + r],
               fill=255)
    img = Image.alpha_composite(img, Image.composite(Image.new(
        "RGBA", (side, side), NAVY + (255,)), Image.new(
            "RGBA", (side, side), (0, 0, 0, 0)), dot))
    return img


F_RAY_LONG = 0.300
F_RAY_SHORT = 0.175
F_HUB = 0.095
F_BLUE = SNOW_BLUE
F_ORANGE = (255, 138, 42)
F_GOLD = (224, 166, 42)
TILE_NAVY = (13, 27, 61)


def _ray_mask(side, center, angle, length, base_half):
    """Tapered ray: flat base at the hub edge, pointed tip."""
    dirx, diry = math.cos(angle), math.sin(angle)
    px, py = -diry, dirx
    bx = (center[0] + F_HUB * dirx) * side
    by = (center[1] + F_HUB * diry) * side
    hx = bx + base_half * px * side
    hy = by + base_half * py * side
    tx = (center[0] + (F_HUB + length) * dirx) * side
    ty = (center[1] + (F_HUB + length) * diry) * side
    mask = Image.new("L", (side, side), 0)
    d = ImageDraw.Draw(mask)
    d.polygon([(bx - base_half * px * side, by - base_half * py * side),
               (bx + base_half * px * side, by + base_half * py * side),
               (tx, ty)], fill=255)
    return mask


def _emblem_tile(rotation=0.0):
    """Crop the supplied artwork and optionally rotate it without redrawing."""
    if not EMBLEM_SOURCE.exists():
        raise FileNotFoundError(f"missing emblem source: {EMBLEM_SOURCE}")
    image = Image.open(EMBLEM_SOURCE).convert("RGBA")
    bbox = image.getbbox()
    if bbox:
        image = image.crop(bbox)
    if rotation:
        image = image.rotate(rotation, resample=Image.BICUBIC, expand=True)
        bbox = image.getbbox()
        if bbox:
            image = image.crop(bbox)
    return image


def _build_f(side, mono=False, core_shape="circle", accent=F_ORANGE,
             white_core=False, core_pattern=None, emblem_rotation=0.0,
             draw_tips=True):
    """F firework with optional monochrome, white-core and badge variants."""
    img = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    center = (0.50, 0.52)
    n = 8
    for k in range(n):
        angle = -math.pi / 2 + 2.0 * math.pi * k / n + math.pi / n
        color = (F_BLUE if mono else
                 (24, 119, 242) if k % 2 == 0 else accent)
        mask = _ray_mask(side, center, angle, F_RAY_LONG, 0.048)
        img = Image.alpha_composite(img, Image.composite(Image.new(
            "RGBA", (side, side), color + (255,)), Image.new(
                "RGBA", (side, side), (0, 0, 0, 0)), mask))
        if not mono and draw_tips:
            tipx = (center[0] + (F_HUB + F_RAY_LONG) * math.cos(angle)) * side
            tipy = (center[1] + (F_HUB + F_RAY_LONG) * math.sin(angle)) * side
            r = 0.042 * side
            d = ImageDraw.Draw(img)
            d.ellipse([tipx - r, tipy - r, tipx + r, tipy + r],
                      fill=WHITE + (255,))
    for k in range(4):
        angle = -math.pi / 2 + 2.0 * math.pi * k / 4
        color = F_BLUE if mono else (accent if k % 2 == 0 else (24, 119, 242))
        mask = _ray_mask(side, center, angle, F_RAY_SHORT, 0.030)
        img = Image.alpha_composite(img, Image.composite(Image.new(
            "RGBA", (side, side), color + (255,)), Image.new(
                "RGBA", (side, side), (0, 0, 0, 0)), mask))
    if core_pattern == "emblem":
        core_r = F_HUB * side
        core = Image.new("L", (side, side), 0)
        d = ImageDraw.Draw(core)
        d.ellipse([(center[0] * side) - core_r, (center[1] * side) - core_r,
                   (center[0] * side) + core_r, (center[1] * side) + core_r],
                  fill=255)
        img = Image.alpha_composite(img, Image.composite(Image.new(
            "RGBA", (side, side), WHITE + (255,)), Image.new(
                "RGBA", (side, side), (0, 0, 0, 0)), core))
        emblem = _emblem_tile(emblem_rotation)
        span = 2.0 * core_r * 0.82
        scale = min(span / emblem.width, span / emblem.height)
        emblem = emblem.resize((max(1, round(emblem.width * scale)),
                                max(1, round(emblem.height * scale))),
                               Image.LANCZOS)
        pos = (round(center[0] * side - emblem.width / 2),
               round(center[1] * side - emblem.height / 2))
        img.alpha_composite(emblem, pos)
        return img
    if core_pattern == "globe":
        badge_r = 0.142 * side
        badge = Image.new("L", (side, side), 0)
        d = ImageDraw.Draw(badge)
        d.ellipse([(center[0] * side) - badge_r, (center[1] * side) - badge_r,
                   (center[0] * side) + badge_r, (center[1] * side) + badge_r],
                  fill=255)
        img = Image.alpha_composite(img, Image.composite(Image.new(
            "RGBA", (side, side), WHITE + (255,)), Image.new(
                "RGBA", (side, side), (0, 0, 0, 0)), badge))
        d = ImageDraw.Draw(img)
        cx, cy = center[0] * side, center[1] * side
        ring_r = 0.108 * side
        ring = [(cx + ring_r * math.cos(-math.pi / 2 + math.pi / 3 * k),
                 cy + ring_r * math.sin(-math.pi / 2 + math.pi / 3 * k))
                for k in range(6)]
        d.line(ring + [ring[0]], fill=F_BLUE + (255,),
               width=max(2, round(0.009 * side)), joint="curve")
        for point in ring:
            angle = math.atan2(point[1] - cy, point[0] - cx)
            d.line([(cx + 0.048 * side * math.cos(angle),
                     cy + 0.048 * side * math.sin(angle)), point],
                   fill=F_BLUE + (255,), width=max(2, round(0.011 * side)))
        core_r = 0.038 * side
        core_hex = [(cx + core_r * math.cos(-math.pi / 2 + math.pi / 3 * k),
                     cy + core_r * math.sin(-math.pi / 2 + math.pi / 3 * k))
                    for k in range(6)]
        d.polygon(core_hex, fill=WHITE + (255,))
        return img
    if mono or white_core:
        core = Image.new("L", (side, side), 0)
        d = ImageDraw.Draw(core)
        if core_shape == "hex":
            r = 0.098 * side
            cx, cy = center[0] * side, center[1] * side
            points = [(cx + r * math.cos(-math.pi / 2 + math.pi / 3 * k),
                       cy + r * math.sin(-math.pi / 2 + math.pi / 3 * k))
                      for k in range(6)]
            d.polygon(points, fill=255)
        else:
            r = 0.095 * side
            d.ellipse([(center[0] * side) - r, (center[1] * side) - r,
                       (center[0] * side) + r, (center[1] * side) + r],
                      fill=255)
        return Image.alpha_composite(img, Image.composite(Image.new(
            "RGBA", (side, side), WHITE + (255,)), Image.new(
                "RGBA", (side, side), (0, 0, 0, 0)), core))
    hub = Image.new("L", (side, side), 0)
    d = ImageDraw.Draw(hub)
    d.ellipse([(center[0] - F_HUB) * side, (center[1] - F_HUB) * side,
               (center[0] + F_HUB) * side, (center[1] + F_HUB) * side],
              fill=255)
    img = Image.alpha_composite(img, Image.composite(Image.new(
        "RGBA", (side, side), NAVY + (255,)), Image.new(
            "RGBA", (side, side), (0, 0, 0, 0)), hub))
    core = Image.new("L", (side, side), 0)
    d = ImageDraw.Draw(core)
    d.ellipse([(center[0] - 0.060) * side, (center[1] - 0.060) * side,
               (center[0] + 0.060) * side, (center[1] + 0.060) * side],
              fill=255)
    img = Image.alpha_composite(img, Image.composite(Image.new(
        "RGBA", (side, side), (24, 119, 242, 255)), Image.new(
            "RGBA", (side, side), (0, 0, 0, 0)), core))
    return img


def _build_tiled(side):
    """H1 on a dark navy rounded-square field for light UI surfaces."""
    mark = _build_f(side, accent=WHITE, draw_tips=False,
                    core_pattern="emblem", emblem_rotation=15.0)
    tile = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    d = ImageDraw.Draw(tile)
    d.rounded_rectangle([0, 0, side - 1, side - 1],
                        radius=round(0.22 * side), fill=TILE_NAVY + (255,))
    tile.alpha_composite(mark)
    return tile


K3_BLUE = (24, 119, 242)
K3_ORANGE = (255, 138, 42)


def _hue_stops(c0, c1, path, n):
    """RGB gradient stops interpolated through HSL hue (short/long arc)."""
    h0, l0, s0 = colorsys.rgb_to_hls(*(v / 255.0 for v in c0))
    h1, l1, s1 = colorsys.rgb_to_hls(*(v / 255.0 for v in c1))
    if path == "long":
        if h1 <= h0:
            h1 += 1.0
    elif h1 >= h0:
        h1 -= 1.0
    stops = []
    for i in range(n + 1):
        t = i / n
        h = h0 + (h1 - h0) * t
        l = l0 + (l1 - l0) * t
        s = s0 + (s1 - s0) * t
        r, g, b = colorsys.hls_to_rgb(h % 1.0, l, s)
        stops.append((t, (round(r * 255), round(g * 255), round(b * 255))))
    return stops


def _gradient_image_axis(side, stops, axis="y"):
    tiny = Image.new("RGBA", (16, 16))
    for y in range(16):
        for x in range(16):
            t = y / 15 if axis == "y" else x / 15
            tiny.putpixel((x, y), _grad(t, stops) + (255,))
    return tiny.resize((side, side), Image.BILINEAR)


def _knot_mask(side):
    """Knot-emoji silhouette as thick strokes, with an over/under crossing."""
    w = 0.110

    def seg(pts, width=w):
        return _band_mask(side, _bezier(pts, 50), width)

    m = Image.new("L", (side, side), 0)
    # Central strand: from the peak straight down behind the loops.
    m = _add(m, seg(((0.50, 0.130), (0.515, 0.360), (0.535, 0.500),
                     (0.550, 0.600))))
    # Peak + diagonals feeding into the loop tops.
    m = _add(m, seg(((0.24, 0.235), (0.50, 0.085), (0.76, 0.235))))
    # Two closed loop rings (rope circles), like the knot emoji.
    m = _add(m, _ellipse_ring_mask(side, (0.205, 0.430), 0.145, 0.185, w))
    m = _add(m, _ellipse_ring_mask(side, (0.795, 0.430), 0.145, 0.185, w))
    # Strand A (under at the crossing): left ring -> crossing -> right tail.
    m = _add(m, seg(((0.310, 0.560), (0.420, 0.615), (0.500, 0.635),
                     (0.580, 0.720), (0.630, 0.850))))
    # Strand B (over at the crossing): right ring -> crossing -> left tail.
    b_cross = ((0.690, 0.560), (0.580, 0.615), (0.500, 0.635),
               (0.420, 0.720), (0.370, 0.850))
    seam = _band_mask(side, _bezier(((0.475, 0.605), (0.500, 0.635),
                                     (0.525, 0.665)), 30), w + 0.040)
    over = seg(b_cross)

    # Tail flares + fray notches at both rope ends.
    flare = Image.new("L", (side, side), 0)
    df = ImageDraw.Draw(flare)
    for tx in (0.370, 0.630):
        df.polygon([(tx * side, 0.840 * side),
                    ((tx - 0.070) * side, 0.900 * side),
                    ((tx + 0.070) * side, 0.900 * side)], fill=255)
    notch = Image.new("L", (side, side), 0)
    dn = ImageDraw.Draw(notch)
    for tx in (0.370, 0.630):
        for off in (-0.030, 0.030):
            x0 = (tx + off) * side
            dn.rectangle([x0, 0.872 * side, x0 + 0.018 * side, 0.900 * side],
                         fill=255)
    return m, seam, over, flare, notch


def _build_k3(side, mode="sunset"):
    """K3: knot-emoji silhouette filled with a blue->orange gradient."""
    m, seam, over, flare, notch = _knot_mask(side)
    if mode == "vertical":
        grad = _gradient_image_axis(side, _hue_stops(K3_BLUE, K3_ORANGE, "long", 6))
    else:
        path = "long" if mode == "sunset" else "short"
        grad = _gradient_image(side, _hue_stops(K3_BLUE, K3_ORANGE, path, 6))
    img = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    img = Image.alpha_composite(img, _paint(m, grad))
    img = Image.alpha_composite(img, _paint(seam, Image.new("RGBA", (side, side),
                                                           NAVY + (255,))))
    img = Image.alpha_composite(img, _paint(over, grad))
    img = Image.alpha_composite(img, _paint(flare, grad))
    img = Image.composite(Image.new("RGBA", (side, side), (0, 0, 0, 0)),
                          img, notch)
    return img


def _ellipse_filled_mask(side, center, rx, ry, rot=0.0, steps=96):
    mask = Image.new("L", (side, side), 0)
    d = ImageDraw.Draw(mask)
    d.polygon([(x * side, y * side) for x, y in
               _ellipse_pts(center, rx, ry, rot, steps)], fill=255)
    return mask


def _hex_ring(side, angle_deg, rx, ry, width, center, ring_r):
    theta = math.radians(angle_deg)
    c = (center[0] + ring_r * math.cos(theta),
         center[1] + ring_r * math.sin(theta))
    return _ellipse_ring_mask(side, c, rx, ry, width,
                              rot=theta + math.pi / 2)


def _build_k4(side, mode="sunset"):
    """K4: hexagonal six-ring woven knot (ChatGPT-style hexafoil)."""
    center = (0.50, 0.52)
    ring_r, rx, ry, width = 0.270, 0.175, 0.120, 0.070
    under = Image.new("L", (side, side), 0)
    over = Image.new("L", (side, side), 0)
    for k in range(6):
        ring = _hex_ring(side, -90 + 60 * k, rx, ry, width, center, ring_r)
        if k % 2 == 0:
            over = _add(over, ring)
        else:
            under = _add(under, ring)
    # Navy seams at the six crossings (between adjacent petals).
    seams = Image.new("L", (side, side), 0)
    for k in range(6):
        phi = math.radians(-60 + 60 * k)
        c = (center[0] + 0.234 * math.cos(phi),
             center[1] + 0.234 * math.sin(phi))
        seams = _add(seams, _ellipse_filled_mask(
            side, c, 0.075, 0.030, rot=phi + math.pi / 2))
    if mode == "vertical":
        grad = _gradient_image_axis(side, _hue_stops(K3_BLUE, K3_ORANGE, "long", 6))
    else:
        path = "long" if mode == "sunset" else "short"
        grad = _gradient_image(side, _hue_stops(K3_BLUE, K3_ORANGE, path, 6))
    img = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    img = Image.alpha_composite(img, _paint(under, grad))
    img = Image.alpha_composite(img, _paint(
        seams, Image.new("RGBA", (side, side), NAVY + (255,))))
    img = Image.alpha_composite(img, _paint(over, grad))
    return img


def _build_k5(side, mode="vertical"):
    """K5: vertical braided rope (plait) -- mirrors the owner's reference."""
    y0, y1, period, w = 0.10, 0.86, 0.24, 0.105
    strands = []
    for phase in (0.0, 1.0 / 3, 2.0 / 3):
        pts = []
        n = 72
        for i in range(n + 1):
            t = i / n
            y = y0 + (y1 - y0) * t
            amp = 0.165 * (1.0 - 0.42 * t)
            x = 0.5 + amp * math.sin(
                2.0 * math.pi * t * (y1 - y0) / period + phase * 2.0 * math.pi)
            pts.append((x, y))
        strands.append(pts)

    # Navy rims first so every crossing reads over/under.
    img = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    navy = Image.new("RGBA", (side, side), NAVY + (255,))
    if mode == "vertical":
        grad = _gradient_image_axis(side, _hue_stops(K3_BLUE, K3_ORANGE, "long", 6))
    else:
        grad = _gradient_image(side, _hue_stops(K3_BLUE, K3_ORANGE, "long", 6))
    body = Image.new("L", (side, side), 0)
    rim = Image.new("L", (side, side), 0)
    for pts in strands:
        rim = _add(rim, _band_mask(side, pts, w + 0.050))
        body = _add(body, _band_mask(side, pts, w))
    # Folded rope arch across the top.
    top = [(0.357, y0), (0.500, 0.045), (0.643, y0)]
    rim = _add(rim, _band_mask(side, _bezier(top, 40), w + 0.050))
    body = _add(body, _band_mask(side, _bezier(top, 40), w))
    # Converging tail at the bottom.
    tail = Image.new("L", (side, side), 0)
    dt = ImageDraw.Draw(tail)
    dt.polygon([(0.430 * side, 0.860 * side), (0.590 * side, 0.860 * side),
                (0.510 * side, 0.935 * side)], fill=255)
    img = Image.alpha_composite(img, Image.composite(
        navy, Image.new("RGBA", (side, side), (0, 0, 0, 0)), rim))
    img = Image.alpha_composite(img, _paint(body, grad))
    img = Image.alpha_composite(img, Image.composite(
        navy, Image.new("RGBA", (side, side), (0, 0, 0, 0)), tail))
    img = Image.alpha_composite(img, _paint(tail, grad))
    return img


def _build_k6(side, mode="vertical"):
    """K6: pretzel knot -- top lobe, crossed arms, two bottom lobes."""
    w = 0.100
    if mode == "vertical":
        grad = _gradient_image_axis(side, _hue_stops(K3_BLUE, K3_ORANGE, "long", 6))
    else:
        grad = _gradient_image(side, _hue_stops(K3_BLUE, K3_ORANGE, "long", 6))
    img = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    navy = Image.new("RGBA", (side, side), NAVY + (255,))

    def seg(pts):
        return _band_mask(side, _bezier(pts, 60), w)

    # Top lobe ring.
    top_ring = _ellipse_ring_mask(side, (0.500, 0.235), 0.240, 0.160, w)
    img = Image.alpha_composite(img, _paint(top_ring, grad))
    # Strand R (under at the crossing): right of top -> bottom-right loop.
    r_path = seg(((0.640, 0.360), (0.570, 0.440), (0.500, 0.500),
                  (0.580, 0.580), (0.700, 0.680), (0.740, 0.760),
                  (0.600, 0.820), (0.520, 0.860)))
    img = Image.alpha_composite(img, _paint(r_path, grad))
    # Navy seam at the crossing.
    seam = _ellipse_filled_mask(side, (0.500, 0.500), 0.060, 0.030, rot=0.8)
    img = Image.alpha_composite(img, Image.composite(
        navy, Image.new("RGBA", (side, side), (0, 0, 0, 0)), seam))
    # Strand L (over): left of top -> bottom-left loop.
    l_path = seg(((0.360, 0.360), (0.430, 0.440), (0.500, 0.500),
                  (0.420, 0.580), (0.300, 0.680), (0.260, 0.760),
                  (0.400, 0.820), (0.480, 0.860)))
    img = Image.alpha_composite(img, _paint(l_path, grad))
    return img


def _build_s(side):
    """S snowflake: crisp tapered blue spokes with needle tips, slender
    twigs (line body + needle tip), and a sharp white hexagonal ring with
    a small white hex centre. Flat two-tone, refined edges."""
    img = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cx = cy = side / 2.0
    R = side / 2.0

    def pt(r, a):
        return (cx + r * R * math.cos(a), cy + r * R * math.sin(a))

    for k in range(6):
        phi = math.radians(90.0) + 2.0 * math.pi * k / 6
        h0, h1 = 0.060 * R, 0.034 * R
        a0 = pt(0.13, phi - h0 / (0.13 * R))
        a1 = pt(0.13, phi + h0 / (0.13 * R))
        b1 = pt(0.73, phi + h1 / (0.73 * R))
        b0 = pt(0.73, phi - h1 / (0.73 * R))
        tip = pt(0.77, phi)
        d.polygon([a0, a1, b1, tip, b0], fill=SNOW_BLUE + (255,))
        tr0 = 0.010 * R
        d.ellipse([tip[0] - tr0, tip[1] - tr0, tip[0] + tr0, tip[1] + tr0],
                  fill=SNOW_BLUE + (255,))
        for s in (+1, -1):
            ang = phi + s * math.radians(46.0)
            q = pt(0.71, ang)
            d.line([pt(0.60, phi), q], fill=SNOW_BLUE + (255,),
                   width=round(0.034 * side), joint="curve")
            px_, py_ = -math.sin(ang), math.cos(ang)
            h = 0.017 * side
            e1 = (q[0] + h * px_, q[1] + h * py_)
            e2 = (q[0] - h * px_, q[1] - h * py_)
            tip2 = pt(0.745, ang)
            d.polygon([e1, e2, tip2], fill=SNOW_BLUE + (255,))
            tr1 = 0.008 * R
            d.ellipse([tip2[0] - tr1, tip2[1] - tr1,
                       tip2[0] + tr1, tip2[1] + tr1],
                      fill=SNOW_BLUE + (255,))
    outer_hex = [pt(0.100, math.radians(90.0 + 30.0 + 60.0 * k)) for k in range(6)]
    inner_hex = [pt(0.055, math.radians(90.0 + 30.0 + 60.0 * k)) for k in range(6)]
    d.polygon(outer_hex, fill=SNOW_WHITE + (255,))
    d.polygon(inner_hex, fill=(0, 0, 0, 0))
    core_hex = [pt(0.040, math.radians(90.0 + 30.0 + 60.0 * k)) for k in range(6)]
    d.polygon(core_hex, fill=SNOW_WHITE + (255,))
    return img

def _build_s2(side):
    """S2: keep the S snowflake core (six shorter tapered blue spokes, sharp
    white hexagonal ring, small white hex centre) and wrap one simple,
    continuous ring of rounded petals around the outside; petals overlap
    about 7 degrees so the wreath reads as a single seamless ring."""
    ss = 4
    big = side * ss
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cx = cy = big / 2.0
    R = big / 2.0

    def pt(r, a):
        return (cx + r * R * math.cos(a), cy + r * R * math.sin(a))

    def hexring(outer_r, inner_r):
        outer = [pt(outer_r, math.radians(90.0 + 30.0 + 60.0 * k)) for k in range(6)]
        inner = [pt(inner_r, math.radians(90.0 + 30.0 + 60.0 * k)) for k in range(6)]
        d.polygon(outer, fill=SNOW_WHITE + (255,))
        d.polygon(inner, fill=(0, 0, 0, 0))

    # six tapered spokes with needle tips (same geometry as S)
    for k in range(6):
        phi = math.radians(90.0) + 2.0 * math.pi * k / 6
        h0, h1 = 0.060 * R, 0.034 * R
        a0 = pt(0.13, phi - h0 / (0.13 * R))
        a1 = pt(0.13, phi + h0 / (0.13 * R))
        b1 = pt(0.73, phi + h1 / (0.73 * R))
        b0 = pt(0.73, phi - h1 / (0.73 * R))
        tip = pt(0.77, phi)
        d.polygon([a0, a1, b1, tip, b0], fill=SNOW_BLUE + (255,))
        tr0 = 0.010 * R
        d.ellipse([tip[0] - tr0, tip[1] - tr0, tip[0] + tr0, tip[1] + tr0],
                  fill=SNOW_BLUE + (255,))

    hexring(0.100, 0.055)
    core_hex = [pt(0.040, math.radians(90.0 + 30.0 + 60.0 * k)) for k in range(6)]
    d.polygon(core_hex, fill=SNOW_WHITE + (255,))

                # wreath of six simple rounded petals OUTSIDE the snowflake: each
    # petal lies along the circle (long axis tangential), and adjacent
    # petals touch end-to-end exactly at the six spoke axes, just beyond
    # the spoke tips, so every petal keeps its own 60-degree sector.
    plen, pwid = 0.52, 0.095
    pside = int(round(2 * (plen + 0.06) * big))
    petal = Image.new("RGBA", (pside, pside), (0, 0, 0, 0))
    pd = ImageDraw.Draw(petal)
    pcx = pcy = pside / 2.0
    a_px, b_px = plen * big, pwid * big
    pd.ellipse([pcx - a_px, pcy - b_px, pcx + a_px, pcy + b_px],
               fill=SNOW_BLUE + (255,))
    for k in range(6):
        theta = math.radians(90.0 + 30.0) + 2.0 * math.pi * k / 6
        rot = theta + math.radians(90.0)
        center = pt(0.79, theta)
        spun = petal.rotate(-math.degrees(rot), expand=True, resample=Image.BICUBIC)
        img.alpha_composite(spun, (int(round(center[0] - spun.width / 2)),
                                   int(round(center[1] - spun.height / 2))))
    return img.resize((side, side), Image.LANCZOS)





BUILDS = {"S": (_build_s, "S snowflake white core"), "S2": (_build_s2, "S2 snowflake + petal wreath"), "K6A": (lambda s: _build_k6(s, "vertical"), "K6A pretzel vertical"),
          "K6B": (lambda s: _build_k6(s, "sunset"), "K6B pretzel sunset"),
          "K1": (_build_k1, "K1 reef-knot"),
          "F": (_build_f, "F firework"),
          "F1": (lambda s: _build_f(s, mono=True, core_shape="hex"),
                 "F1 blue firework + white hex"),
          "F2": (lambda s: _build_f(s, mono=True, core_shape="circle"),
                 "F2 blue firework + white circle"),
          "F3": (lambda s: _build_f(s, accent=F_ORANGE, white_core=True),
                 "F3 orange + white circle"),
          "F4": (lambda s: _build_f(s, accent=F_GOLD, white_core=True),
                 "F4 gold + white circle"),
          "G1": (lambda s: _build_f(s, mono=True, core_pattern="globe"),
                 "G1 globe core"),
          "G2": (lambda s: _build_f(s, mono=True, core_pattern="emblem"),
                 "G2 source emblem"),
          "G3": (lambda s: _build_f(s, mono=True, core_pattern="emblem",
                                    emblem_rotation=15.0),
                 "G3 source emblem rotated"),
          "H1": (lambda s: _build_f(s, accent=WHITE, draw_tips=False,
                                    core_pattern="emblem", emblem_rotation=15.0),
                 "H1 blue/white sharp burst"),
          "A": (_build_tiled, "A navy tile"),
          "B": (lambda s: _build_f(s, accent=(0, 0, 0), draw_tips=False,
                                   core_pattern="emblem", emblem_rotation=15.0),
                 "B black rays")}

LABELS = {k: v[1] for k, v in BUILDS.items()}
ACCENTS = {"S": (23, 116, 238), "S2": (23, 116, 238), "K6A": (232, 93, 4), "K6B": (24, 119, 242),
           "K1": (24, 119, 242), "F": (232, 93, 4), "F1": (23, 116, 238),
           "F2": (23, 116, 238), "F3": (232, 93, 4), "F4": (224, 166, 42),
           "G1": (23, 116, 238), "G2": (23, 116, 238), "G3": (23, 116, 238),
           "H1": (23, 116, 238), "A": (13, 27, 61), "B": (25, 31, 42)}




# ---------------------------------------------------------------- previews

def _font(size, bold=True):
    for path in (r"C:\Windows\Fonts\segoeuib.ttf" if bold else "",
                 r"C:\Windows\Fonts\segoeui.ttf",
                 r"C:\Windows\Fonts\arialbd.ttf",
                 r"C:\Windows\Fonts\arial.ttf"):
        if not path:
            continue
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _pixel_stats(img16):
    blue = orange = other = 0
    px = img16.convert("RGBA").load()
    for y in range(img16.height):
        for x in range(img16.width):
            r, g, b, a = px[x, y]
            if a < 140:
                continue
            if b > r + 20:
                blue += 1
            elif r > b + 20:
                orange += 1
            else:
                other += 1
    return blue, orange, other


def _ascii16(img16):
    px = img16.convert("RGBA").load()
    rows = []
    for y in range(img16.height):
        row = []
        for x in range(img16.width):
            r, g, b, a = px[x, y]
            if a < 140:
                row.append(".")
            elif b > r + 20:
                row.append("B")
            elif r > b + 20:
                row.append("O")
            else:
                row.append("w")
        rows.append("".join(row))
    return rows


def _tile(img, cell, radius=30):
    tile = Image.new("RGBA", (cell, cell), (13, 27, 61, 255))
    d = ImageDraw.Draw(tile)
    d.rounded_rectangle([0, 0, cell - 1, cell - 1], radius=radius,
                        fill=(13, 27, 61, 255))
    glyph = img.resize((cell - 24, cell - 24), Image.LANCZOS)
    tile.alpha_composite(glyph, (12, 12))
    return tile


def _preview_sheet(masters):
    cell, gap, label_w, pad = 132, 14, 170, 24
    sizes = (16, 32, 48, 64, 128)
    width = pad + label_w + cell + gap + len(sizes) * cell + (len(sizes) - 1) * gap + pad
    height = len(masters) * (pad + cell + pad)
    sheet = Image.new("RGBA", (width, height), (246, 248, 250, 255))
    draw = ImageDraw.Draw(sheet)
    font = _font(24)
    for row, (key, master) in enumerate(masters.items()):
        label = LABELS.get(key, key)
        accent = ACCENTS.get(key, (24, 119, 242))
        y0 = pad + row * (cell + 2 * pad)
        draw.text((pad, y0 + cell // 2 - 14), label, font=font, fill=accent + (255,))
        x = pad + label_w
        sheet.alpha_composite(_tile(master, cell), (x, y0))
        x += cell + gap
        for size in sizes:
            draw.rectangle([x - 1, y0 - 1, x + cell, y0 + cell],
                           outline=(218, 224, 230, 255))
            small = master.resize((size, size), Image.LANCZOS)
            sheet.alpha_composite(Image.new("RGBA", small.size, (255, 255, 255, 255)), (x, y0))
            sheet.alpha_composite(small, (x, y0))
            x += cell + gap
    return sheet


def _zoom_sheet(masters, zf=16):
    pad, gap = 24, 24
    cell = 16 * zf
    width = pad * 2 + len(masters) * cell + (len(masters) - 1) * gap
    height = pad * 2 + cell
    sheet = Image.new("RGBA", (width, height), (255, 255, 255, 255))
    x = pad
    for master in masters.values():
        small = master.resize((16, 16), Image.LANCZOS).resize((cell, cell), Image.NEAREST)
        sheet.alpha_composite(small, (x, pad))
        x += cell + gap
    return sheet


# ------------------------------------------------------------- wordmarks

def _build_wordmark(with_icon, icon):
    text = "Heytalk"
    font = _font(40)
    bbox = font.getbbox(text)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    icon48 = icon.resize((48, 48), Image.LANCZOS)
    h = 64
    if with_icon:
        w = 48 + 12 + tw + 12
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        img.alpha_composite(icon48, (0, 8))
        d = ImageDraw.Draw(img)
        d.text((48 + 12, (h - th) // 2 - bbox[1]), text, font=font,
               fill=WORDMARK_TEXT + (255,))
    else:
        w = tw + 12
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.text((6, (h - th) // 2 - bbox[1]), text, font=font,
               fill=WORDMARK_TEXT + (255,))
    return img


def _write_favicon(icon256):
    buf = io.BytesIO()
    icon256.resize((256, 256), Image.LANCZOS).save(buf, format="PNG", optimize=True)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256" '
           'width="256" height="256"><image width="256" height="256" '
           f'href="data:image/png;base64,{b64}"/></svg>')
    FAVICON.write_text(svg, encoding="utf-8")


# ------------------------------------------------------------------- main

def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="Heytalk G4 icon generator")
    ap.add_argument("--preview", action="store_true",
                    help="write previews only (no production files touched)")
    ap.add_argument("--apply", choices=("K1", "F", "F1", "F2", "F3", "F4",
                                        "G1", "G2", "G3", "H1", "A", "B",
                                        "K6A", "K6B", "S", "S2"),
                    help="write the production icon set + favicon")
    args = ap.parse_args(argv)
    if not args.preview and not args.apply:
        ap.print_help()
        return 2

    REDESIGN.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    masters = {key: fn(max(SIZES)) for key, (fn, _) in BUILDS.items()}
    for key, (fn, label) in BUILDS.items():
        img = masters[key]
        img.resize((256, 256), Image.LANCZOS).save(
            REDESIGN / f"G4-{key.lower()}-256.png", format="PNG", optimize=True)
        img16 = img.resize((16, 16), Image.LANCZOS)
        img16.save(REDESIGN / f"G4-{key.lower()}-16.png", format="PNG", optimize=True)
        b, o, w = _pixel_stats(img16)
        print(f"{label:14s} 16px: blue={b:3d}  orange={o:3d}  neutral={w:3d}")

    sheet = _preview_sheet(masters)
    sheet.save(REDESIGN / "G4-compare.png", format="PNG", optimize=True)
    zoom = _zoom_sheet(masters)
    zoom.save(REDESIGN / "G4-zoom16.png", format="PNG", optimize=True)

    print("\nASCII at 16 px (B=blue O=orange .=transparent):")
    for key, (fn, label) in BUILDS.items():
        print(f"  {label}")
        for row in _ascii16(masters[key].resize((16, 16), Image.LANCZOS)):
            print("    " + row)

    if args.apply:
        key = args.apply.upper()
        master = masters[key]
        for size in SIZES:
            target = OUT_DIR / ("icon.png" if size == max(SIZES)
                                else f"icon-{size}.png")
            master.resize((size, size), Image.LANCZOS).save(
                target, format="PNG", optimize=True)
            print(f"  wrote {target.relative_to(PROJECT_ROOT)}")
        for name, with_icon in (("wordmark.png", True), ("wordmark-text.png", False)):
            mark = _build_wordmark(with_icon, master.resize((256, 256), Image.LANCZOS))
            path = OUT_DIR / name
            mark.save(path, format="PNG", optimize=True)
            print(f"  wrote {path.relative_to(PROJECT_ROOT)}")
        _write_favicon(master.resize((256, 256), Image.LANCZOS))
        print(f"  wrote {FAVICON.relative_to(PROJECT_ROOT)}")
        print(f"\nAPPLIED {BUILDS[key][1]}: /api/icon and favicon now serve it.")
    else:
        print("\npreview only -- production icon.png / favicon.svg untouched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

