"""Surya extractor — the Indic path (final model for non-English rolls).

RapidOCR (the English workhorse) is 0% on Indic script; Surya (a multilingual
OCR-VLM, `surya-ocr` 0.20, needs a `llama.cpp`/`llama-server` backend) reads it.
Ported from scraper/data/_bench/allfields_eval/surya_telugu.py.

Structure mirrors the RapidOCR extractor exactly (column geometry + a label state
machine); only the OCR engine and the field-label strings differ. Latin/Arabic
fields (EPIC, serial, age, house digits) still come from RapidOCR — Surya supplies
the Indic-script name/relation/gender (the two-engine split for Indic).

⚠️ STATUS: ported + wired, but NOT yet verified on a real Indic roll (needs one +
the llama.cpp backend). The English RapidOCR path is the verified one. Treat Surya
output as unverified until measured on an Indic roll with native-checked truth.

All heavy deps (surya, torch, rapidocr, PIL) are imported lazily so `ocr/`'s core
stays stdlib-only and importing this module never pulls Surya in.
"""
from __future__ import annotations
import re

from .base import Extractor, register
from .rapidocr import _EPIC, _REASONS, _tokens as _rapid_tokens

# Telugu field labels -> canonical field. (Extend per script as more Indic rolls arrive.)
_TE_NAME = ("ఓటరు పేరు", "పేరు")
_TE_REL = [("భర్త", "Husband"), ("తండ్రి", "Father"), ("తల్లి", "Mother")]
_TE_HOUSE = ("ఇంటి సంఖ్య",)
_TE_AGE = ("వయస్సు",)
_TE_GENDER = {"స్త్రీ": "Female", "పురుషుడు": "Male", "మహిళ": "Female"}


def _surya_lines(image_path):
    """Surya full-page recognition -> [{x,y,text}] (one entry per detected block)."""
    from PIL import Image
    from surya.inference import SuryaInferenceManager
    from surya.recognition import RecognitionPredictor
    rec = RecognitionPredictor(SuryaInferenceManager())
    page = rec([Image.open(image_path).convert("RGB")], full_page=True)[0]
    out = []
    for blk in page.blocks:
        poly = blk.polygon or [[0, 0]]
        xs = [p[0] for p in poly]
        ys = [p[1] for p in poly]
        txt = re.sub(r"<[^>]+>", " ", blk.html or "").replace("&nbsp;", " ").strip()
        out.append({"x": min(xs), "y": min(ys), "text": txt})
    return out


@register("surya")
class SuryaExtractor(Extractor):
    """Indic reader. English/RapidOCR path is preferred; use this only for Indic language_code."""

    def __init__(self, dpi: int = 200, **_):
        self.dpi = dpi

    def extract(self, image_path: str, json_schema: dict, instruction: str):
        props = (json_schema or {}).get("properties", {})
        if "cards" not in props:
            # Summary/cover for Indic rolls: the numeric summary is Latin -> reuse RapidOCR's
            # positional parsers (imported lazily to avoid a hard dep at import time).
            from .rapidocr import _parse_summary, _parse_cover
            if "additions_total" in props:
                return _parse_summary(image_path)
            return _parse_cover(image_path)
        return self._parse_cards(image_path)

    def _parse_cards(self, image_path):
        # EPIC/serial/age/house digits are Latin -> RapidOCR; Indic name/relation/gender -> Surya.
        rapid = _rapid_tokens(image_path)
        surya = _surya_lines(image_path)
        if not rapid:
            return {"section_header": "", "cards": []}
        width = max(t["x2"] for t in rapid)
        cw = width / 3.0
        col = lambda t: min(2, int(t["x"] / cw))

        epics = []
        for t in rapid:
            m = _EPIC.search(t["text"].replace(" ", ""))
            if m and (t["x"] - col(t) * cw) > 0.5 * cw:
                epics.append({"col": col(t), "y": t["y"], "epic": m.group()})
        epics.sort(key=lambda d: (d["col"], d["y"]))
        if not epics:
            return {"section_header": "", "cards": []}
        per_col: dict = {}
        for e in epics:
            per_col.setdefault(e["col"], []).append(e["y"])
        deltas = []
        for cys in per_col.values():
            cys.sort()
            deltas += [b - a for a, b in zip(cys, cys[1:]) if b - a > 0]
        card_h = sorted(deltas)[len(deltas) // 2] if deltas else round(219 * self.dpi / 200)
        pad = int(0.15 * card_h)

        cards = []
        for i, e in enumerate(epics):
            c, ey = e["col"], e["y"]
            nxt = next((n["y"] for n in epics[i + 1:] if n["col"] == c), None)
            top, bot = ey - pad, (nxt - pad if nxt is not None else ey + card_h - pad)
            x0 = c * cw
            card = {"serial_no": None, "marker_box": "", "epic_no": e["epic"], "full_name": "",
                    "relation_type": "", "relation_name": "", "house_number": "", "age": None,
                    "gender": "", "deleted_watermark": False, "deletion_reason_code": ""}
            # Latin fields from RapidOCR (left half of column)
            for t in rapid:
                if not (top <= t["y"] < bot and x0 <= t["x"] < x0 + 0.5 * cw):
                    continue
                txt = t["text"].strip()
                if abs(t["y"] - ey) < 0.35 * card_h and re.fullmatch(r"[A-Za-z]?\s*\d{1,4}", txt):
                    if txt[:1].upper() in _REASONS:
                        card["deleted_watermark"] = True
                        card["deletion_reason_code"] = txt[:1].upper()
                    m = re.search(r"\d{1,4}", txt)
                    if m:
                        card["serial_no"] = int(m.group())
                mage = re.search(r"(\d{1,3})", txt)
                if "వయస" in t["text"] or (mage and 10 <= int(mage.group(1)) <= 120 and card["age"] is None and t["y"] > ey + 0.4 * card_h):
                    if mage:
                        card["age"] = int(mage.group(1))
            # Indic fields from Surya (left half of column)
            band = [s for s in surya if top <= s["y"] < bot and x0 <= s["x"] < x0 + 0.6 * cw]
            band.sort(key=lambda d: d["y"])
            for s in band:
                txt = s["text"]
                if any(k in txt for k in _TE_NAME) and not card["full_name"]:
                    card["full_name"] = _after_colon(txt)
                for key, canon in _TE_REL:
                    if key in txt:
                        card["relation_type"] = canon
                        card["relation_name"] = _after_colon(txt)
                        break
                if any(k in txt for k in _TE_HOUSE):
                    card["house_number"] = _after_colon(txt)
                for gk, gv in _TE_GENDER.items():
                    if gk in txt:
                        card["gender"] = gv
            if card["serial_no"] is not None:
                cards.append(card)
        return {"section_header": "", "cards": cards}


def _after_colon(t):
    return t.split(":", 1)[1].strip() if ":" in t else t.strip()
