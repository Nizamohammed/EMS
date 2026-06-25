"""Reconcile extracted electors against the page-N summary (the QC oracle).

Mirrors the v_roll_reconciliation view in db/schema.sql: live = non-deleted,
and live count + per-gender split must equal the printed net; additions/deletions
counts must match the summary rows. `reconciles` is total (never None).
"""
from __future__ import annotations
from .types import CardRecord, SummaryFigures, ReconResult


def reconcile(electors: list[CardRecord], s: SummaryFigures) -> ReconResult:
    live = [e for e in electors if e.status != "deleted"]
    deletions = sum(1 for e in electors if e.status == "deleted")
    additions = sum(1 for e in electors if e.status == "added")
    modifications = sum(1 for e in electors if e.status == "modified")
    live_m = sum(1 for e in live if e.gender == "Male")
    live_f = sum(1 for e in live if e.gender == "Female")
    live_tg = sum(1 for e in live if e.gender == "ThirdGender")

    checks = {
        "net_total": s.net_total is None or len(live) == s.net_total,
        "net_male": s.net_male is None or live_m == s.net_male,
        "net_female": s.net_female is None or live_f == s.net_female,
        "net_third_gender": s.net_third_gender is None or live_tg == s.net_third_gender,
        "additions": s.additions_total is None or additions == s.additions_total,
        "deletions": s.deletions_total is None or deletions == s.deletions_total,
        "has_summary": s.net_total is not None,
    }
    reconciles = all(checks.values())

    return ReconResult(
        cards_printed=len(electors),
        deletions=deletions,
        additions=additions,
        modifications=modifications,
        live_cards=len(live),
        live_male=live_m,
        live_female=live_f,
        live_third_gender=live_tg,
        reconciles=reconciles,
        detail={"checks": checks,
                "summary": {"net_total": s.net_total, "net_male": s.net_male,
                            "net_female": s.net_female, "net_third_gender": s.net_third_gender,
                            "additions_total": s.additions_total, "deletions_total": s.deletions_total,
                            "num_modifications": s.num_modifications}},
    )
