"""PDF -> page-crop PNGs for extraction.

Observed roll layout (both sample rolls follow it):
  p1            = cover/metadata
  p2            = polling-station imagery (skipped)
  p3 .. p(N-1)  = voter cards (3x10 grid; includes any 'List of Additions' section)
  pN            = 'Summary of Electors' (the reconciliation oracle)

Voter pages are cut into top/bottom half crops (with overlap, so no card is lost
at the seam) for legibility after the model's image downscale. Cover/summary are
rendered full-page.
"""
from __future__ import annotations
import os
import re
import shutil
import subprocess
from dataclasses import dataclass


@dataclass
class Crops:
    cover: str
    summary: str
    voter: list          # list[(page:int, band:str, path:str)]
    page_count: int


def _bands(dpi: int):
    """Top/bottom half-page crop boxes (x,y,w,h) in pixels, scaled to the DPI.
    Bands overlap ~7% of page height so no card is lost at the seam."""
    w = round(595 / 72 * dpi)        # A4 width  in px
    h = round(842 / 72 * dpi)        # A4 height in px
    top = (0, 0, w, round(h * 0.55))            # header + ~first 5 rows + overlap
    by = round(h * 0.48)
    bot = (0, by, w, h - by)                    # ~last 5 rows + footer
    return top, bot


def _page_count(pdf: str) -> int:
    out = subprocess.run(["pdfinfo", pdf], capture_output=True, text=True, check=True).stdout
    m = re.search(r"^Pages:\s+(\d+)", out, re.MULTILINE)
    if not m:
        raise RuntimeError("could not read page count from pdfinfo")
    return int(m.group(1))


def _ppm(pdf: str, page: int, out_prefix: str, dpi: int = 300, crop=None) -> str:
    cmd = ["pdftoppm", "-png", "-r", str(dpi), "-f", str(page), "-l", str(page), "-singlefile"]
    if crop:
        x, y, w, h = crop
        cmd += ["-x", str(x), "-y", str(y), "-W", str(w), "-H", str(h)]
    cmd += [pdf, out_prefix]
    subprocess.run(cmd, check=True, capture_output=True)
    return out_prefix + ".png"


def rasterize(pdf: str, out_dir: str, dpi: int = 300) -> Crops:
    if shutil.which("pdftoppm") is None:
        raise RuntimeError("pdftoppm (poppler) not found on PATH")
    os.makedirs(out_dir, exist_ok=True)
    n = _page_count(pdf)
    if n < 3:
        raise RuntimeError(f"roll has only {n} pages; expected cover + voter + summary")

    cover = _ppm(pdf, 1, os.path.join(out_dir, "cover"), dpi)
    summary = _ppm(pdf, n, os.path.join(out_dir, "summary"), dpi)

    top_box, bot_box = _bands(dpi)
    voter = []
    for p in range(3, n):  # p2 is imagery, pN is summary
        pp = f"{p:02d}"
        voter.append((p, "top", _ppm(pdf, p, os.path.join(out_dir, f"vp-{pp}-top"), dpi, top_box)))
        voter.append((p, "bot", _ppm(pdf, p, os.path.join(out_dir, f"vp-{pp}-bot"), dpi, bot_box)))
    return Crops(cover=cover, summary=summary, voter=voter, page_count=n)
