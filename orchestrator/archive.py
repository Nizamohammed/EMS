"""Archiver — push a verified roll PDF to cold storage before local deletion.

PDFs are the only irreplaceable artifact (§9/§20), so a part is NEVER cleared from
local disk until it is both loaded AND archived. Backends are pluggable:
  local  -> copy under an archive dir, mirroring rolls/{stateCd}/{ac}/ (dev + a
            mounted network/OneDrive folder)
  none   -> no-op (returns None; the run loop then must not clear local)
Future: rclone (OneDrive), s3 — same put(rel_key, src) contract.
"""
from __future__ import annotations
import os
import shutil


class Archiver:
    def __init__(self, backend: str = "local", dest: str | None = None):
        self.backend = backend
        self.dest = dest
        if backend == "local" and not dest:
            raise ValueError("archive backend 'local' needs --archive-dir")

    @property
    def enabled(self) -> bool:
        return self.backend != "none"

    def put(self, rel_key: str, src_path: str) -> str | None:
        """Copy/upload src_path under rel_key; return the archive URI (or None if disabled)."""
        if self.backend == "none":
            return None
        if self.backend == "local":
            target = os.path.join(self.dest, rel_key)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            # atomic-ish: copy to a temp then rename
            tmp = target + ".part"
            shutil.copy2(src_path, tmp)
            os.replace(tmp, target)
            return "file://" + os.path.abspath(target)
        raise ValueError(f"unknown archive backend: {self.backend}")

    def exists(self, rel_key: str) -> bool:
        if self.backend == "local":
            return os.path.exists(os.path.join(self.dest, rel_key))
        return False
