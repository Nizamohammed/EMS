"""Assemble raw per-crop cards into the final elector list.

Steps: normalize fields -> dedupe overlapping crops by serial (keep the most
complete read) -> infer status from region + marker box -> structural QA
(language-independent validators that set a per-card confidence + flags).
"""
from __future__ import annotations
import re
from .types import CardRecord

_EPIC_RE = re.compile(r"^[A-Z]{3}[0-9]{7}$")
_REASONS = {"E", "S", "R", "M", "Q"}
_GENDERS = {"Male", "Female", "ThirdGender"}


def _norm_relation(v):
    if not v:
        return None
    s = v.strip().lower()
    if s.startswith("father"):
        return "Father"
    if s.startswith("husband"):
        return "Husband"
    if s.startswith("mother"):
        return "Mother"
    return "Other"


def _norm_gender(v):
    if not v:
        return None
    s = v.strip().lower().replace(" ", "")
    if s in ("male", "m"):
        return "Male"
    if s in ("female", "f"):
        return "Female"
    if "third" in s or s == "tg" or s == "o":
        return "ThirdGender"
    return None


def _completeness(c: CardRecord) -> int:
    return sum(1 for v in (c.epic_no, c.full_name, c.relation_name, c.house_number, c.age, c.gender) if v)


def _supplement_no(header: str):
    m = re.search(r"additions?\s+(\d+)", (header or "").lower())
    return int(m.group(1)) if m else None


def _infer_status(c: CardRecord):
    mb = (c.marker_box or "").strip()
    if c.region == "main_roll":
        if c.deleted_watermark or c.deletion_reason_code or mb in _REASONS:
            reason = c.deletion_reason_code or (mb if mb in _REASONS else None)
            return "deleted", reason, None, None
        if "#" in mb:
            return "modified", None, None, c.supplement_no
        return "active", None, None, None
    if c.region == "additions":
        sect = int(mb) if mb.isdigit() else None      # box on an addition = section number
        return "added", None, sect, c.supplement_no
    if c.region == "deletions":
        return "deleted", (c.deletion_reason_code or (mb if mb in _REASONS else None)), None, c.supplement_no
    return "active", None, None, None


def _validate(c: CardRecord):
    flags = []
    if c.epic_no and not _EPIC_RE.match(c.epic_no):
        flags.append("epic_format")
    if c.age is not None and not (0 <= c.age <= 120):
        flags.append("age_range")
    if c.gender and c.gender not in _GENDERS:
        flags.append("gender_enum")
    if not c.full_name:
        flags.append("missing_name")
    # simple confidence: start at 1.0, dock per structural flag
    c.flags = flags
    c.confidence = round(max(0.0, 1.0 - 0.25 * len(flags)), 3)


def assemble(cards: list[CardRecord]) -> list[CardRecord]:
    # normalize + dedupe by serial (prefer the most complete read across overlapping crops)
    best: dict[int, CardRecord] = {}
    for c in cards:
        c.relation_type = _norm_relation(c.relation_type)
        c.gender = _norm_gender(c.gender)
        if c.epic_no:
            c.epic_no = c.epic_no.strip().upper().replace(" ", "")
        prev = best.get(c.serial_no)
        if prev is None or _completeness(c) > _completeness(prev):
            best[c.serial_no] = c

    out = []
    for sn in sorted(best):
        c = best[sn]
        status, reason, section_no, supp = _infer_status(c)
        c.status = status
        c.deletion_reason_code = reason
        if section_no is not None:
            c.section_no = section_no
        if supp is not None:
            c.supplement_no = supp
        _validate(c)
        out.append(c)
    return out
