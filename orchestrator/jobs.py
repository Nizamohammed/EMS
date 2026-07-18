"""JobStore — the orchestrator's view of the scraper's manifest.db.

`download_job` (one row per part) already tracks the DOWNLOAD leg. This adds the
post-download pipeline columns (idempotent migration, so an existing manifest.db
upgrades in place) and the queries the run loop needs. SQLite via the stdlib.
"""
from __future__ import annotations
import sqlite3
import time

# Post-download pipeline columns added to download_job (applied idempotently).
_PIPELINE_COLUMNS = (
    ("pipeline_stage", "text"),        # pending | extracted | loaded | archived | done | failed
    ("reconciles", "integer"),         # 1 / 0 / null (QC oracle result)
    ("elector_count", "integer"),      # electors extracted
    ("pipeline_error", "text"),        # last pipeline error, if any
    ("archive_uri", "text"),           # where the verified PDF was archived
    ("pipeline_updated_at", "text"),
)

# A part is ready for the pipeline once its download is verified.
READY_STATUS = "verified"


class JobStore:
    def __init__(self, db_path: str):
        self.db = sqlite3.connect(db_path)
        self.db.row_factory = sqlite3.Row
        self._migrate()

    def _migrate(self):
        cur = self.db.cursor()
        existing = {r[1] for r in cur.execute("pragma table_info(download_job)")}
        for name, typ in _PIPELINE_COLUMNS:
            if name not in existing:
                cur.execute(f"alter table download_job add column {name} {typ}")
        self.db.commit()

    # ---- lookups -----------------------------------------------------------
    def state_cd_by_name(self, name: str):
        row = self.db.execute(
            "select state_cd from state where lower(state_name)=lower(?)", (name,)
        ).fetchone()
        return row["state_cd"] if row else (name if _looks_like_cd(name) else None)

    def acs_for_state(self, state_cd: str) -> list[int]:
        rows = self.db.execute(
            "select distinct ac_no from download_job where state_cd=? and status=? order by ac_no",
            (state_cd, READY_STATUS),
        ).fetchall()
        return [r["ac_no"] for r in rows]

    def parts_for_ac(self, state_cd: str, ac_no: int) -> list[sqlite3.Row]:
        return self.db.execute(
            "select * from download_job where state_cd=? and ac_no=? and status=? order by part_no",
            (state_cd, ac_no, READY_STATUS),
        ).fetchall()

    # ---- state transitions -------------------------------------------------
    def set_stage(self, job_id: int, stage: str, *, reconciles=None, elector_count=None,
                  error=None, archive_uri=None):
        sets = ["pipeline_stage=?", "pipeline_updated_at=?"]
        vals = [stage, _now()]
        if reconciles is not None:
            sets.append("reconciles=?"); vals.append(1 if reconciles else 0)
        if elector_count is not None:
            sets.append("elector_count=?"); vals.append(int(elector_count))
        # error is cleared on success (error=None passed explicitly won't clear; use "" to clear)
        if error is not None:
            sets.append("pipeline_error=?"); vals.append(error or None)
        if archive_uri is not None:
            sets.append("archive_uri=?"); vals.append(archive_uri)
        vals.append(job_id)
        self.db.execute(f"update download_job set {', '.join(sets)} where id=?", vals)
        self.db.commit()

    def stage_of(self, job_id: int):
        row = self.db.execute("select pipeline_stage from download_job where id=?", (job_id,)).fetchone()
        return row["pipeline_stage"] if row else None

    def state_summary(self, state_cd: str):
        rows = self.db.execute(
            "select coalesce(pipeline_stage,'(none)') as stage, count(*) n "
            "from download_job where state_cd=? and status=? group by pipeline_stage",
            (state_cd, READY_STATUS),
        ).fetchall()
        return {r["stage"]: r["n"] for r in rows}

    def close(self):
        self.db.close()


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _looks_like_cd(s: str) -> bool:
    return bool(s) and len(s) == 3 and s[0] in "SU" and s[1:].isdigit()
