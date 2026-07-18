"""EMS batch orchestrator — per-AC: download -> OCR+reconcile -> load -> archive -> clear.

    python -m orchestrator.run --state Lakshadweep \
        --pg 'docker exec -i ems_pg psql -U postgres -d ems' \
        --archive-dir /path/to/archive [--acs 1,2] [--clear] [--download]

State-by-state, but disk is cleared per AC (a big state's PDFs dwarf local disk).
Resumable: each part records its pipeline_stage; a re-run skips parts already `done`.
Safety: a part is cleared from local disk ONLY after it is both loaded AND archived,
and only when --clear is given (default keeps files).
"""
from __future__ import annotations
import argparse
import os
import subprocess
import sys
import time

from .jobs import JobStore
from .db import PgLoader
from .archive import Archiver

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _log(msg):
    print(msg, flush=True)


def _ensure_local(args, job) -> str | None:
    """Return a readable local PDF path for this part, downloading if enabled."""
    fp = job["file_path"]
    if fp and os.path.exists(fp):
        return fp
    if not args.download:
        return None
    # Best-effort: the Node driver downloads a whole state (English-first, one language
    # per part). Per-AC pipelining is a scraper enhancement (add --ac to bin/download.js);
    # for now we invoke it once and re-check. Requires the ECI portal to be reachable.
    _log(f"    [download] invoking scraper for {args.state} (solver={args.solver})")
    cmd = ["node", os.path.join(_REPO, "scraper", "bin", "download.js"),
           "--state", args.state, "--solver", args.solver, "--lang", (job["language"] or "ENGLISH")]
    try:
        subprocess.run(cmd, cwd=os.path.join(_REPO, "scraper"), timeout=args.download_timeout, check=False)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        _log(f"    [download] failed: {e}")
    return fp if (fp and os.path.exists(fp)) else None


def _process_part(args, jobs, pg, archiver, extractor, job) -> str:
    """Drive one part through the pipeline. Returns the terminal stage reached."""
    from ocr.pipeline import run_roll
    from ocr.load import to_sql
    from ocr.cli import _context_from_filename

    jid = job["id"]
    tag = f"{job['state_cd']} AC{job['ac_no']} part {job['part_no']}"

    pdf = _ensure_local(args, job)
    if not pdf:
        jobs.set_stage(jid, "failed", error="PDF not present locally (use --download)")
        _log(f"  [{tag}] SKIP — no local PDF")
        return "failed"

    # 1. EXTRACT + RECONCILE
    try:
        ctx = _context_from_filename(pdf)
        if job["pdf_sha256"]:
            ctx.source_pdf_sha256 = job["pdf_sha256"]
        result = run_roll(pdf, ctx, extractor, dpi=args.dpi, verbose=False)
    except Exception as e:  # noqa: BLE001
        jobs.set_stage(jid, "failed", error=f"extract: {e}")
        _log(f"  [{tag}] EXTRACT FAILED: {e}")
        return "failed"
    recon = result.recon.reconciles
    n = len(result.electors)
    jobs.set_stage(jid, "extracted", reconciles=recon, elector_count=n, error="")
    flag = "reconciles" if recon else "NO-RECONCILE"
    _log(f"  [{tag}] extracted {n} electors [{flag}]")
    if args.require_reconcile and not recon:
        jobs.set_stage(jid, "failed", error="reconcile failed (--require-reconcile)")
        return "failed"

    # 2. LOAD into Postgres
    ok, err = pg.load(to_sql(result))
    if not ok:
        jobs.set_stage(jid, "failed", error=f"load: {err}")
        _log(f"  [{tag}] LOAD FAILED: {err}")
        return "failed"
    jobs.set_stage(jid, "loaded")

    # 3. ARCHIVE (must precede any local deletion)
    archive_uri = None
    if archiver.enabled:
        rel = f"{job['state_cd']}/{job['ac_no']}/{os.path.basename(pdf)}"
        try:
            archive_uri = archiver.put(rel, pdf)
        except Exception as e:  # noqa: BLE001
            jobs.set_stage(jid, "failed", error=f"archive: {e}")
            _log(f"  [{tag}] ARCHIVE FAILED: {e}")
            return "failed"
        jobs.set_stage(jid, "archived", archive_uri=archive_uri)

    # 4. CLEAR local (only when loaded AND archived)
    if args.clear:
        if not archiver.enabled:
            _log(f"  [{tag}] not cleared — archiving disabled (never delete un-archived PDFs)")
        else:
            try:
                os.remove(pdf)
            except OSError as e:
                _log(f"  [{tag}] clear warning: {e}")

    jobs.set_stage(jid, "done")
    return "done"


def run(args) -> int:
    jobs = JobStore(args.manifest)
    state_cd = jobs.state_cd_by_name(args.state)
    if not state_cd:
        _log(f"unknown state '{args.state}'"); jobs.close(); return 2

    pg = PgLoader(args.pg)
    ok, err = pg.check()
    if not ok:
        _log(f"Postgres not reachable via `{args.pg}`: {err}"); jobs.close(); return 2

    archiver = Archiver(args.archive_backend,
                        dest=args.archive_dir if args.archive_backend == "local" else None)
    if args.clear and not archiver.enabled:
        _log("refusing --clear with --archive-backend none (would delete the only copy)"); jobs.close(); return 2

    from ocr.extractors.base import get_extractor
    extractor = get_extractor("rapidocr", deleted_backend=args.deleted_backend,
                              qwen_host=args.qwen_host, dpi=args.dpi)

    acs = jobs.acs_for_state(state_cd)
    if args.acs:
        want = {int(x) for x in args.acs.split(",")}
        acs = [a for a in acs if a in want]
    if not acs:
        _log(f"no verified parts for {state_cd}"); jobs.close(); return 1

    _log(f"=== orchestrating {state_cd} — {len(acs)} AC(s): {acs} ===")
    t0 = time.monotonic()
    tally = {"done": 0, "failed": 0, "skipped": 0}
    for ac in acs:
        parts = jobs.parts_for_ac(state_cd, ac)
        _log(f"-- AC {ac}: {len(parts)} part(s) --")
        for job in parts:
            if jobs.stage_of(job["id"]) == "done" and not args.force:
                tally["skipped"] += 1
                _log(f"  [{state_cd} AC{ac} part {job['part_no']}] already done — skip")
                continue
            stage = _process_part(args, jobs, pg, archiver, extractor, job)
            tally["done" if stage == "done" else "failed"] += 1

    dt = time.monotonic() - t0
    _log(f"=== finished in {dt:.0f}s — done={tally['done']} failed={tally['failed']} "
         f"skipped={tally['skipped']} ===")
    _log(f"stage summary for {state_cd}: {jobs.state_summary(state_cd)}")
    jobs.close()
    return 0 if tally["failed"] == 0 else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="EMS batch orchestrator")
    ap.add_argument("--state", required=True, help="state name (e.g. Lakshadweep) or code (U06)")
    ap.add_argument("--acs", default=None, help="comma-separated AC numbers (default: all verified)")
    ap.add_argument("--manifest", default=os.path.join(_REPO, "scraper", "manifest.db"))
    ap.add_argument("--pg", required=True,
                    help="psql client command, e.g. 'psql postgresql://u:p@host/db' or "
                         "'docker exec -i ems_pg psql -U postgres -d ems'")
    ap.add_argument("--archive-backend", default="local", choices=["local", "none"])
    ap.add_argument("--archive-dir", default=None, help="destination for --archive-backend local")
    ap.add_argument("--deleted-backend", default="none", choices=["none", "dp", "local", "http"])
    ap.add_argument("--qwen-host", default=None)
    ap.add_argument("--dpi", type=int, default=200)
    ap.add_argument("--clear", action="store_true", help="delete each part's local PDF after load+archive")
    ap.add_argument("--require-reconcile", action="store_true",
                    help="do not load parts that fail the reconcile QC (default: load + flag)")
    ap.add_argument("--force", action="store_true", help="re-run parts already marked done")
    ap.add_argument("--download", action="store_true", help="invoke the Node scraper for missing parts")
    ap.add_argument("--solver", default="trocr", help="captcha solver for --download (manual|trocr)")
    ap.add_argument("--download-timeout", type=int, default=3600)
    args = ap.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
