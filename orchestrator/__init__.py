"""EMS batch orchestrator — chains download -> OCR -> DB load -> archive -> clear.

Per §20: batch state-by-state, but CLEAR LOCAL per-AC (a big state's PDFs dwarf
local disk). One `download_job` row per part is the unit of work; this package
extends it with post-download pipeline stages and drives each part through:

    downloaded -> extracted (OCR + reconcile) -> loaded (Postgres) -> archived -> done

Resumable at part granularity; a failure flags the part and moves on.
"""
