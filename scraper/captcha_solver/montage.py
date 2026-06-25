#!/usr/bin/env python3
"""Stitch harvested captchas into labeled grids for cheap bulk hand-labeling.

Each cell shows one captcha at near-full size with its index above it, so a
whole grid can be transcribed in a single vision read (cuts labeling cost ~15x
vs reading one image at a time). The transcriptions feed labels.csv.

Usage:
  python3 captcha_solver/montage.py --start 0 --count 200 --per 15
"""
import argparse
import os
import sys
from PIL import Image, ImageDraw, ImageFont

RAW = os.path.join(os.path.dirname(__file__), "..", "data", "captchas", "raw")
OUT = os.path.join(os.path.dirname(__file__), "..", "data", "captchas", "montages")

CELL_W = 470          # per-captcha width (kept large — these are adversarial)
HEADER = 28           # space for the index label
PAD = 12
COLS = 2


def load_font(size=20):
    for p in ("/System/Library/Fonts/Supplemental/Arial.ttf",
              "/System/Library/Fonts/Helvetica.ttc"):
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            pass
    return ImageFont.load_default()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--count", type=int, default=200)
    ap.add_argument("--per", type=int, default=15, help="captchas per montage")
    args = ap.parse_args()

    files = sorted(f for f in os.listdir(RAW) if f.endswith(".png"))
    files = files[args.start:args.start + args.count]
    if not files:
        print("no captchas in", RAW)
        sys.exit(1)
    os.makedirs(OUT, exist_ok=True)
    font = load_font()

    rows_per = (args.per + COLS - 1) // COLS
    made = 0
    for batch_i in range(0, len(files), args.per):
        batch = files[batch_i:batch_i + args.per]
        cell_h = 0
        thumbs = []
        for fn in batch:
            im = Image.open(os.path.join(RAW, fn)).convert("RGB")
            scale = CELL_W / im.width
            im = im.resize((CELL_W, int(im.height * scale)))
            thumbs.append((fn, im))
            cell_h = max(cell_h, im.height)
        cell_h += HEADER
        W = COLS * CELL_W + (COLS + 1) * PAD
        H = rows_per * cell_h + (rows_per + 1) * PAD
        canvas = Image.new("RGB", (W, H), "white")
        draw = ImageDraw.Draw(canvas)
        for k, (fn, im) in enumerate(thumbs):
            r, c = divmod(k, COLS)
            x = PAD + c * (CELL_W + PAD)
            y = PAD + r * (cell_h + PAD)
            idx = args.start + batch_i + k
            draw.text((x, y), f"[{idx}] {fn}", fill="black", font=font)
            canvas.paste(im, (x, y + HEADER))
            draw.rectangle([x, y + HEADER, x + CELL_W, y + HEADER + im.height], outline="#ccc")
        name = f"montage_{args.start + batch_i:04d}.png"
        canvas.save(os.path.join(OUT, name))
        made += 1
        print(f"{name}: indices {args.start + batch_i}..{args.start + batch_i + len(batch) - 1} ({len(batch)} captchas)")
    print(f"\n{made} montage(s) -> {OUT}")


if __name__ == "__main__":
    main()
