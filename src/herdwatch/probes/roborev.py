from __future__ import annotations

import json
import subprocess
from typing import Callable

from ..cache import TTLCache
from ..models import PaneContext, Pending

PRIORITY = 30
_ACTIVE = {"queued", "running"}


def default_run_status() -> dict:
    try:
        r = subprocess.run(["roborev", "status", "--json"], capture_output=True,
                           text=True, timeout=5)
        if r.returncode != 0 or not r.stdout.strip():
            return {}
        return json.loads(r.stdout)
    except Exception:
        return {}


def default_run_list(cwd: str) -> list[dict]:
    try:
        r = subprocess.run(["roborev", "list", "--repo", cwd, "--limit", "20", "--json"],
                           cwd=cwd, capture_output=True, text=True, timeout=10)
        if r.returncode != 0 or not r.stdout.strip():
            return []
        return json.loads(r.stdout)
    except Exception:
        return []


class RoborevProbe:
    name = "roborev"

    def __init__(self, cache: TTLCache,
                 run_status: Callable[[], dict] = default_run_status,
                 run_list: Callable[[str], list[dict]] = default_run_list) -> None:
        self._cache = cache
        self._run_status = run_status
        self._run_list = run_list

    def check(self, ctx: PaneContext) -> Pending | None:
        if not (ctx.is_git_repo and ctx.head_sha):
            return None
        status = self._cache.get_or(("roborev-status",), self._run_status)
        daemon = status.get("daemon", {})
        if (daemon.get("queued_jobs", 0) + daemon.get("running_jobs", 0)) == 0:
            return None
        jobs = self._cache.get_or(("roborev-list", ctx.cwd),
                                  lambda: self._run_list(ctx.cwd))
        for job in jobs:
            if not isinstance(job, dict):
                continue
            if job.get("git_ref") == ctx.head_sha and job.get("status") in _ACTIVE:
                return Pending(label="review", priority=PRIORITY, source=self.name)
        return None
