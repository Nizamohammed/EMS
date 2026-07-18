"""PgLoader — run a roll's generated SQL against Postgres.

The loader SQL (ocr.load.to_sql) is a psql script (uses \\gset), so it's fed to a
`psql` client over stdin. The client command is configurable so the same code runs
against a local server (`psql "postgresql://..."`), a remote one, or a container
(`docker exec -i ems_pg psql -U postgres -d ems`) without change.
"""
from __future__ import annotations
import shlex
import subprocess


class PgLoader:
    def __init__(self, psql_cmd: str, timeout: int = 600):
        # base client command; ON_ERROR_STOP is appended so any failing statement aborts the load
        self.base = shlex.split(psql_cmd) + ["-v", "ON_ERROR_STOP=1"]
        self.timeout = timeout

    def load(self, sql_text: str) -> tuple[bool, str]:
        """Run one roll's SQL (a single transaction). Returns (ok, stderr_tail)."""
        try:
            p = subprocess.run(self.base, input=sql_text, text=True,
                               capture_output=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            return False, f"psql timed out after {self.timeout}s"
        except FileNotFoundError as e:
            return False, f"psql client not found: {e}"
        if p.returncode != 0:
            tail = (p.stderr or p.stdout or "").strip().splitlines()
            return False, "\n".join(tail[-4:])
        return True, ""

    def check(self) -> tuple[bool, str]:
        """Cheap connectivity probe."""
        return self.load("select 1;")
