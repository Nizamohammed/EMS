"""Plain data structures passed between pipeline stages."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class CardRecord:
    """One voter card as read from a single crop (pre-assembly), enriched in place by assemble()."""
    serial_no: int
    epic_no: Optional[str] = None
    full_name: str = ""
    relation_type: Optional[str] = None      # Father | Husband | Mother | Other
    relation_name: Optional[str] = None
    house_number: Optional[str] = None        # free text
    age: Optional[int] = None
    gender: Optional[str] = None              # Male | Female | ThirdGender
    marker_box: Optional[str] = None          # raw content of the box by the serial
    deleted_watermark: bool = False
    deletion_reason_code: Optional[str] = None  # E|S|R|M|Q
    # provenance / region (set by the pipeline before assemble)
    region: Optional[str] = None              # main_roll | additions | deletions
    source_page: Optional[int] = None
    # filled by assemble()
    status: Optional[str] = None              # active | deleted | added | modified
    section_no: Optional[int] = None
    supplement_no: Optional[int] = None
    confidence: float = 1.0
    flags: list = field(default_factory=list)


@dataclass
class SummaryFigures:
    """Page-N 'Summary of Electors' — the reconciliation oracle."""
    net_male: Optional[int] = None
    net_female: Optional[int] = None
    net_third_gender: Optional[int] = None
    net_total: Optional[int] = None
    additions_total: Optional[int] = None
    deletions_total: Optional[int] = None
    num_modifications: Optional[int] = None
    ending_serial_no: Optional[int] = None


@dataclass
class RollContext:
    """Known-up-front facts about the roll (from the download manifest / filename)."""
    language_code: str = "ENG"
    state_cd: Optional[str] = None
    ac_no: Optional[int] = None
    part_no: Optional[int] = None
    roll_type: Optional[str] = None
    source_pdf_filename: Optional[str] = None
    source_pdf_sha256: Optional[str] = None


@dataclass
class ReconResult:
    cards_printed: int
    deletions: int
    additions: int
    modifications: int
    live_cards: int
    live_male: int
    live_female: int
    live_third_gender: int
    reconciles: bool
    detail: dict = field(default_factory=dict)


@dataclass
class RollResult:
    context: RollContext
    electors: list           # list[CardRecord]
    summary: SummaryFigures
    recon: ReconResult
    cover: dict = field(default_factory=dict)   # raw cover extraction (geography/part metadata)
