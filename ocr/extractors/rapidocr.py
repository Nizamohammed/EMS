"""RapidOCR extractor — the English workhorse (main extractor).

RapidOCR (PaddleOCR PP-OCRv5 on ONNX, CPU, free) reads printed roll text at
~99% char accuracy (EPIC/age/relation_type 100%). This extractor turns its
line output into the same structured dict the pipeline expects, so it drops into
`ocr/` behind the standard Extractor contract (schema/instruction are ignored —
extraction is deterministic, driven by the fixed roll layout).

Three page kinds, dispatched by the schema's shape:
  - voter card strip  -> per-card fields via column geometry + a label state machine
                         (ported from scraper/data/_bench/allfields_eval/parse_rapidocr.py,
                          generalized from single-column to the 3-column half-page).
                         DELETED cards are flagged from the reason letter {E,S,R,M,Q}
                         and (if a DeletedCardReader is attached) their occluded
                         name/relation_name/house_number are re-read by the
                         Donut+Pix2Struct(+Qwen) combine.
  - summary           -> the reconciliation oracle, read positionally (column
                         headers + row labels).
  - cover             -> best-effort labeled fields (not load-bearing for reconcile).

Heavy deps (rapidocr, PIL, torch via the combine) are imported lazily so `ocr/`'s
core stays stdlib-only.
"""
from __future__ import annotations
import re

from .base import Extractor, register

_EPIC = re.compile(r"[A-Z]{3}[0-9]{7}")
_REASONS = {"E", "S", "R", "M", "Q"}
_REL_LABELS = [("husband", "Husband"), ("wife", "Husband"), ("father", "Father"),
               ("mother", "Mother"), ("other", "Other")]
_ocr_singleton = None


def _ocr():
    global _ocr_singleton
    if _ocr_singleton is None:
        from rapidocr_onnxruntime import RapidOCR
        _ocr_singleton = RapidOCR()
    return _ocr_singleton


def _tokens(image_path):
    """RapidOCR -> [{x,y,x2,text,conf}] (x,y = top-left of the box)."""
    res, _ = _ocr()(image_path)
    out = []
    for box, text, conf in (res or []):
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        out.append({"x": min(xs), "y": min(ys), "x2": max(xs), "text": text.strip(), "conf": conf})
    return out


def _strip_label(t):
    if ":" in t:
        return t.split(":", 1)[1].strip()
    return re.sub(r"^\s*(house\s*numbe?r?|name|fathers?\s*name|mothers?\s*name|"
                  r"husbands?\s*name|wifes?\s*name|others?)\s*", "", t, flags=re.I).strip()


def _detect_section_header(toks):
    """Topmost 'Section No and Name ...' / 'List of Additions/Deletions ...' line.
    Whitespace-insensitive — RapidOCR often drops the spaces ('ListofAdditions1(..)')."""
    for t in sorted(toks, key=lambda d: d["y"]):
        low = t["text"].lower().replace(" ", "")
        if low.startswith("sectionnoand") or "listofaddition" in low or "listofdeletion" in low:
            return t["text"]
    return ""


def _parse_cards(image_path, reader=None, dpi=200):
    toks = _tokens(image_path)
    if not toks:
        return {"section_header": "", "cards": []}
    width = max(t["x2"] for t in toks)
    cw = width / 3.0                                   # three equal columns
    # card height = median vertical spacing between EPIC anchors within a column
    def col_of(t):
        return min(2, int(t["x"] / cw))

    epics = []
    for t in toks:
        m = _EPIC.search(t["text"].replace(" ", ""))
        if m:
            c = col_of(t)
            x_rel = t["x"] - c * cw
            if x_rel > 0.5 * cw:                       # EPIC sits in the right ~40% of its column
                epics.append({"col": c, "y": t["y"], "epic": m.group()})
    epics.sort(key=lambda d: (d["col"], d["y"]))
    if not epics:
        return {"section_header": _detect_section_header(toks), "cards": []}
    # card height = median spacing between consecutive EPIC anchors WITHIN a column
    # (global spacing is polluted by same-row EPICs across columns -> ~1px deltas)
    per_col: dict = {}
    for e in epics:
        per_col.setdefault(e["col"], []).append(e["y"])
    deltas = []
    for cys in per_col.values():
        cys.sort()
        deltas += [b - a for a, b in zip(cys, cys[1:]) if b - a > 0]
    card_h = (sorted(deltas)[len(deltas) // 2] if deltas else round(219 * dpi / 200))
    pad = int(0.15 * card_h)

    from PIL import Image
    img = None

    cards = []
    for i, e in enumerate(epics):
        c, ey = e["col"], e["y"]
        nxt = next((n["y"] for n in epics[i + 1:] if n["col"] == c), None)
        band_top = ey - pad
        band_bot = (nxt - pad) if nxt is not None else (ey + card_h - pad)
        x0 = c * cw
        left = [t for t in toks if band_top <= t["y"] < band_bot
                and x0 <= t["x"] < x0 + 0.5 * cw]      # left half of the column = the text fields
        left.sort(key=lambda d: d["y"])

        card = {"serial_no": None, "marker_box": "", "epic_no": e["epic"], "full_name": "",
                "relation_type": "", "relation_name": "", "house_number": "", "age": None,
                "gender": "", "deleted_watermark": False, "deletion_reason_code": ""}
        cur = None
        for t in left:
            txt = t["text"]
            low = txt.lower().lstrip()
            if ("as on" in low or low.startswith(("parliamentary", "section no", "assembly"))
                    or "#/#" in low or low.startswith("part no")):
                continue
            # serial (+ optional reason letter) on the EPIC's top line.
            # A GENUINE deletion reason is spatially SEPARATED from the serial
            # (rendered "S  905" -> letter + space + digits, or a standalone letter
            # token; see the bare-letter branch below). A letter FUSED to the digits
            # ("S06") is RapidOCR misreading a serial digit (9 looks like S) -> it is
            # NOT a deletion; strip it and keep only the digits (grid consensus will
            # fix the value). This prevents a misread serial from faking a deletion.
            if abs(t["y"] - ey) < 0.35 * card_h and re.fullmatch(r"[A-Za-z]?\s*\d{1,4}", txt):
                sep = re.match(r"([A-Za-z])\s+\d", txt)        # letter + SPACE + digits
                if sep and sep.group(1).upper() in _REASONS:
                    lead = sep.group(1).upper()
                    card["deleted_watermark"] = True
                    card["deletion_reason_code"] = lead
                    card["marker_box"] = lead
                m = re.search(r"\d{1,4}", txt)
                if m:
                    card["serial_no"] = int(m.group())
                continue
            # a bare reason letter token in the serial box area
            if abs(t["y"] - ey) < 0.35 * card_h and txt.strip().upper() in _REASONS \
                    and (t["x"] - x0) < 0.35 * cw:
                card["deleted_watermark"] = True
                card["deletion_reason_code"] = txt.strip().upper()
                card["marker_box"] = card["deletion_reason_code"]
                continue
            if low.startswith("name"):
                card["full_name"] = _strip_label(txt); cur = "name"; continue
            matched = False
            for key, canon in _REL_LABELS:
                if low.startswith(key):
                    card["relation_type"] = canon
                    card["relation_name"] = _strip_label(txt)
                    cur = "relation"; matched = True; break
            if matched:
                continue
            if low.startswith("house"):
                card["house_number"] = _strip_label(txt); cur = "house"; continue
            if low.startswith("age") or "gender" in low:
                mm = re.search(r"age\s*[:.]?\s*(\d{1,3})", low)
                if mm:
                    card["age"] = int(mm.group(1))
                g = re.search(r"(female|male|third\s*gender|transgender)", low, re.I)
                if g:
                    gv = g.group(1).lower()
                    card["gender"] = ("Female" if "female" in gv else
                                      "ThirdGender" if "third" in gv or "trans" in gv else "Male")
                cur = "age"; continue
            # continuation of a wrapped field
            if cur == "relation" and card["relation_name"]:
                card["relation_name"] += " " + txt
            elif cur == "house" and card["house_number"]:
                card["house_number"] += " " + txt
            elif cur == "name" and card["full_name"]:
                card["full_name"] += " " + txt
        card["_col"] = c
        card["_y"] = ey
        # DELETED card -> re-read the occluded fields with the specialist combine
        if card["deleted_watermark"] and reader is not None:
            if img is None:
                img = Image.open(image_path).convert("RGB")
            sub = img.crop((int(x0), max(0, int(band_top)), int(x0 + cw), int(band_bot)))
            try:
                fix = reader.read(sub)
                for f in ("full_name", "relation_type", "relation_name", "house_number"):
                    if fix.get(f):
                        card[f] = fix[f]
            except Exception:
                pass                                    # keep RapidOCR's read if the combine fails
        cards.append(card)

    # Assign serials from the row-major grid, correcting single-digit OCR errors.
    # The grid is rigid (cols 0,1,2 in a visual row = consecutive serials), so each
    # card independently implies a row base = serial - col; the majority base wins,
    # which fills serials RapidOCR dropped AND outvotes a misread digit (e.g. a
    # serial-box '9' read as '6' -> base 4 loses 2:1 to base 7 -> serial restored).
    cards.sort(key=lambda k: k["_y"])
    rows: list = []
    for c in cards:
        if rows and abs(c["_y"] - rows[-1][-1]["_y"]) < 0.5 * card_h:
            rows[-1].append(c)
        else:
            rows.append([c])
    from collections import Counter
    prev_base = None
    for row in rows:
        votes = Counter(c["serial_no"] - c["_col"] for c in row if c["serial_no"] is not None)
        if votes:
            top = votes.most_common()
            winners = [b for b, ct in top if ct == top[0][1]]
            base = (min(winners, key=lambda b: abs(b - (prev_base + 3)))
                    if len(winners) > 1 and prev_base is not None else winners[0])
        elif prev_base is not None:
            base = prev_base + 3                       # empty row -> continue the grid
        else:
            base = None
        if base is not None:
            for c in row:
                c["serial_no"] = base + c["_col"]
        prev_base = base
    out = []
    for c in cards:
        if c["serial_no"] is None:
            continue
        c.pop("_col", None)
        c.pop("_y", None)
        out.append(c)
    return {"section_header": _detect_section_header(toks), "cards": out}


def _nearest_col(x, headers):
    return min(headers, key=lambda h: abs(h[1] - x))[0]


def _parse_summary(image_path):
    toks = _tokens(image_path)
    # column headers: Male / Female / Third(Gender) / Total, by x
    headers = []
    for key, name in (("male", "net_male"), ("female", "net_female"),
                      ("third", "net_third_gender"), ("total", "net_total")):
        cand = [t for t in toks if t["text"].strip().lower().startswith(key)]
        if cand:
            headers.append((name, min(cand, key=lambda d: d["y"])["x"]))
    out = {"net_male": None, "net_female": None, "net_third_gender": None, "net_total": None,
           "additions_total": None, "deletions_total": None, "num_modifications": None}

    def row_values(label_substrings):
        rows = [t for t in toks if any(s in t["text"].lower() for s in label_substrings)]
        if not rows:
            return {}
        ry = rows[0]["y"]
        vals = {}
        for t in toks:
            if abs(t["y"] - ry) < 0.5 * 30 and re.fullmatch(r"\d{1,5}", t["text"].strip()):
                col = _nearest_col(t["x"], headers) if headers else None
                if col:
                    vals[col] = int(t["text"].strip())
        return vals

    if headers:
        net = row_values(["net elector", "i+ii", "i+ll", "after this revision"])
        for k in ("net_male", "net_female", "net_third_gender", "net_total"):
            out[k] = net.get(k)
        add = row_values(["list of additions"])
        out["additions_total"] = add.get("net_total")
        dele = row_values(["list of deletions"])
        out["deletions_total"] = dele.get("net_total")
    # modifications: a number near 'NUMBER OF MODIFICATIONS'
    mod = [t for t in toks if "modification" in t["text"].lower()]
    if mod:
        my = mod[0]["y"]
        nums = [int(t["text"]) for t in toks if abs(t["y"] - my) < 120 and re.fullmatch(r"\d{1,4}", t["text"].strip())]
        if nums:
            out["num_modifications"] = max(nums)
    return out


def _parse_cover(image_path):
    """Best-effort labeled cover fields (not load-bearing; reconcile uses the summary)."""
    toks = _tokens(image_path)
    text = {t["text"].lower(): t["text"] for t in toks}
    out = {}
    for t in toks:
        low = t["text"].lower()
        m = re.search(r"district\s*[:\-]?\s*(.+)", low)
        if m and not out.get("district_name"):
            out["district_name"] = _strip_label(t["text"])
    return out


@register("rapidocr")
class RapidOcrExtractor(Extractor):
    """Deterministic RapidOCR extraction; DELETED cards routed to the specialist combine."""

    def __init__(self, deleted_backend: str = "none", qwen_host: str | None = None,
                 model_dir: str | None = None, dpi: int = 200, **_):
        self.deleted_backend = deleted_backend
        self.qwen_host = qwen_host
        self.model_dir = model_dir
        self.dpi = dpi
        self._reader = None                             # lazy: only build the combine if a deleted card appears
        self._reader_failed = False                     # a build failure disables the combine, not the roll

    def _reader_for(self):
        if self.deleted_backend == "none" or self._reader_failed:
            return None
        if self._reader is None:
            import sys, os
            sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scraper"))
            try:
                from deleted_card_solver.combine import DeletedCardReader
                self._reader = DeletedCardReader(qwen_backend=self.deleted_backend,
                                                 qwen_host=self.qwen_host, model_dir=self.model_dir)
            except Exception as e:  # noqa: BLE001 — degrade to RapidOCR-only, never kill the roll
                self._reader_failed = True
                print(f"[warn] deleted-card reader unavailable ({e}); "
                      f"continuing RapidOCR-only (DELETED cards keep their occluded reads)")
                return None
        return self._reader

    def extract(self, image_path: str, json_schema: dict, instruction: str):
        props = (json_schema or {}).get("properties", {})
        if "cards" in props:
            return _parse_cards(image_path, reader=self._reader_for(), dpi=self.dpi)
        if "additions_total" in props and "cards" not in props:
            return _parse_summary(image_path)
        if "polling_station_no" in props or "ac_name" in props:
            return _parse_cover(image_path)
        return {}
