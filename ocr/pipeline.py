"""Orchestrator: one roll PDF -> reconciled RollResult.

rasterize -> (cover) -> summary -> per-crop card extraction (region tagged from
the strip header) -> assemble -> reconcile. The extractor is injected, so the
engine is swappable without touching this flow.
"""
from __future__ import annotations
import os
import tempfile

from . import schemas
from .extractors.base import Extractor
from .rasterize import rasterize
from .assemble import assemble, _supplement_no
from .reconcile import reconcile
from .types import CardRecord, SummaryFigures, RollContext, RollResult


def _region(header: str) -> str:
    h = (header or "").lower()
    if "addition" in h:
        return "additions"
    if "deletion" in h:
        return "deletions"
    return "main_roll"


def _to_card(d: dict, region: str, supp, page: int) -> CardRecord:
    def s(v):
        v = (v or "").strip() if isinstance(v, str) else v
        return v or None
    return CardRecord(
        serial_no=int(d["serial_no"]),
        epic_no=s(d.get("epic_no")),
        full_name=(d.get("full_name") or "").strip(),
        relation_type=s(d.get("relation_type")),
        relation_name=s(d.get("relation_name")),
        house_number=s(d.get("house_number")),
        age=d.get("age") if isinstance(d.get("age"), int) and d.get("age") else None,
        gender=s(d.get("gender")),
        marker_box=s(d.get("marker_box")),
        deleted_watermark=bool(d.get("deleted_watermark")),
        deletion_reason_code=s(d.get("deletion_reason_code")),
        region=region, supplement_no=supp, source_page=page,
    )


def run_roll(pdf_path: str, ctx: RollContext, extractor: Extractor,
             work_dir: str | None = None, verbose: bool = False,
             dpi: int = 300, max_voter_pages: int | None = None) -> RollResult:
    import time
    tmp = work_dir or tempfile.mkdtemp(prefix="ems_ocr_")
    crops = rasterize(pdf_path, tmp, dpi=dpi)
    voter = crops.voter
    if max_voter_pages:                       # dev loop: only the first N voter pages
        voter = [v for v in voter if v[0] < 3 + max_voter_pages]
    if verbose:
        note = f" (limited to first {max_voter_pages} pages)" if max_voter_pages else ""
        print(f"[rasterize] {crops.page_count} pages, dpi={dpi} -> {len(voter)} voter crops{note}")

    # summary (oracle) — extract first so a failure is loud early
    t0 = time.monotonic()
    sd = extractor.extract(crops.summary, schemas.SUMMARY_SCHEMA, schemas.SUMMARY_INSTRUCTION) or {}
    if verbose:
        print(f"[extract] summary ({time.monotonic() - t0:.1f}s)")
    summary = SummaryFigures(
        net_male=sd.get("net_male"), net_female=sd.get("net_female"),
        net_third_gender=sd.get("net_third_gender"), net_total=sd.get("net_total"),
        additions_total=sd.get("additions_total"), deletions_total=sd.get("deletions_total"),
        num_modifications=sd.get("num_modifications"),
    )

    # cover (geography/part metadata; enrich ctx; non-fatal best-effort)
    cover_data: dict = {}
    try:
        cover_data = extractor.extract(crops.cover, schemas.COVER_SCHEMA, schemas.COVER_INSTRUCTION) or {}
        if cover_data.get("ending_serial_no"):
            summary.ending_serial_no = cover_data["ending_serial_no"]
        if ctx.ac_no is None and cover_data.get("ac_no"):
            ctx.ac_no = cover_data["ac_no"]
        if ctx.part_no is None and cover_data.get("part_no"):
            ctx.part_no = cover_data["part_no"]
    except Exception as e:  # cover is not load-bearing for reconciliation
        if verbose:
            print(f"[cover] skipped: {e}")

    # voter cards — a failure on one crop skips it and continues (don't lose the whole roll)
    raw: list[CardRecord] = []
    failures: list[str] = []
    for (page, band, path) in voter:
        t0 = time.monotonic()
        try:
            pd = extractor.extract(path, schemas.CARD_PAGE_SCHEMA, schemas.CARD_PAGE_INSTRUCTION) or {}
        except Exception as e:  # noqa: BLE001 — one bad crop must not kill the roll
            failures.append(f"p{page}/{band}")
            if verbose:
                print(f"[extract] p{page}/{band} FAILED ({time.monotonic() - t0:.1f}s): {e}")
            continue
        header = pd.get("section_header", "")
        region = _region(header)
        supp = _supplement_no(header) if region in ("additions", "deletions") else None
        for d in pd.get("cards", []):
            try:
                raw.append(_to_card(d, region, supp, page))
            except (KeyError, ValueError, TypeError):
                continue  # skip malformed card; reconciliation will flag the gap
        if verbose:
            print(f"[extract] p{page}/{band} [{region}] -> {len(pd.get('cards', []))} cards "
                  f"({time.monotonic() - t0:.1f}s)")

    electors = assemble(raw)
    recon = reconcile(electors, summary)
    recon.detail["failed_crops"] = failures
    if failures and verbose:
        print(f"[warn] {len(failures)} crop(s) failed extraction: {failures}")
    return RollResult(context=ctx, electors=electors, summary=summary, recon=recon, cover=cover_data)
