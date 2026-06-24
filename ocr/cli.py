"""CLI: run the OCR pipeline on one roll PDF and print the reconciliation result.

    python -m ocr.cli <roll.pdf> [--extractor qwen2.5vl] [--model qwen2.5vl:3b]
                       [--out result.json] [--workdir DIR] [-v]

Use --extractor mock to exercise the plumbing with no model/GPU.
"""
from __future__ import annotations
import argparse
import dataclasses
import json
import os
import re
import sys

from .extractors.base import get_extractor, available
from .pipeline import run_roll
from .types import RollContext

# {year}-EROLLGEN-{stateCd}-{acNo}-...-{LANG}-{partNo}-WI...
_FN = re.compile(r"EROLLGEN-([SU]\d{2})-(\d+)-.*?-([A-Z]{2,3})-(\d+)-WI", re.IGNORECASE)


def _context_from_filename(path: str) -> RollContext:
    name = os.path.basename(path)
    ctx = RollContext(source_pdf_filename=name)
    m = _FN.search(name)
    if m:
        ctx.state_cd = m.group(1).upper()
        ctx.ac_no = int(m.group(2))
        ctx.language_code = m.group(3).upper()
        ctx.part_no = int(m.group(4))
    if "DraftRoll" in name:
        ctx.roll_type = "SIR_DraftRoll"
    elif "FinalRoll" in name:
        ctx.roll_type = "SIR_FinalRoll" if "SIR" in name else "FinalRoll"
    return ctx


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="EMS OCR pipeline")
    ap.add_argument("pdf")
    ap.add_argument("--extractor", default="qwen2.5vl", help=f"one of: {available()}")
    ap.add_argument("--model", default=None, help="model id for the extractor (e.g. qwen2.5vl:3b)")
    ap.add_argument("--out", default=None, help="write full result JSON here")
    ap.add_argument("--sql-out", default=None, help="write loadable SQL (for db/schema.sql) here")
    ap.add_argument("--workdir", default=None, help="dir for page crops (default: temp)")
    ap.add_argument("--dpi", type=int, default=300, help="rasterization DPI; lower = faster, less legible (try 150)")
    ap.add_argument("--max-voter-pages", type=int, default=None,
                    help="only process the first N voter pages (fast dev loop)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    if not os.path.exists(args.pdf):
        print(f"no such file: {args.pdf}", file=sys.stderr)
        return 2

    kw = {"model": args.model} if args.model else {}
    try:
        extractor = get_extractor(args.extractor, **kw)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2

    ctx = _context_from_filename(args.pdf)
    result = run_roll(args.pdf, ctx, extractor, work_dir=args.workdir, verbose=args.verbose,
                      dpi=args.dpi, max_voter_pages=args.max_voter_pages)
    r = result.recon

    print("\n=== reconciliation ===")
    print(f"  roll            : {ctx.state_cd} AC{ctx.ac_no} part {ctx.part_no} [{ctx.language_code}]")
    print(f"  cards printed   : {r.cards_printed}")
    print(f"  live / net      : {r.live_cards} / {r.detail['summary']['net_total']}")
    print(f"  M / F / TG       : {r.live_male} / {r.live_female} / {r.live_third_gender}")
    print(f"  additions       : {r.additions}   deletions: {r.deletions}   modifications: {r.modifications}")
    print(f"  checks          : {r.detail['checks']}")
    print(f"  RECONCILES      : {r.reconciles}")

    if args.out:
        payload = {
            "context": dataclasses.asdict(ctx),
            "summary": dataclasses.asdict(result.summary),
            "recon": dataclasses.asdict(r),
            "electors": [dataclasses.asdict(e) for e in result.electors],
        }
        with open(args.out, "w") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        print(f"  wrote {args.out} ({len(result.electors)} electors)")

    if args.sql_out:
        from .load import to_sql
        with open(args.sql_out, "w") as fh:
            fh.write(to_sql(result))
        print(f"  wrote {args.sql_out} (load with: psql -v ON_ERROR_STOP=1 -f {args.sql_out})")

    return 0 if r.reconciles else 1


if __name__ == "__main__":
    raise SystemExit(main())
