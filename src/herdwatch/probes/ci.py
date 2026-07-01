from __future__ import annotations

import json
import subprocess
from typing import Callable

from ..cache import TTLCache
from ..models import PaneContext, Pending

PRIORITY = 20
_ACTIVE = {"queued", "in_progress"}


def default_run_gh(cwd: str, branch: str | None) -> list[dict]:
    args = ["gh", "run", "list", "--limit", "20",
            "--json", "status,headSha,workflowName"]
    if branch:
        args += ["--branch", branch]
    try:
        r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=15)
        if r.returncode != 0 or not r.stdout.strip():
            return []
        return json.loads(r.stdout)
    except Exception:
        return []


class CIProbe:
    name = "ci"

    def __init__(self, cache: TTLCache,
                 run_gh: Callable[[str, str | None], list[dict]] = default_run_gh) -> None:
        self._cache = cache
        self._run_gh = run_gh

    def check(self, ctx: PaneContext) -> Pending | None:
        if not (ctx.is_git_repo and ctx.has_github_remote and ctx.head_sha):
            return None
        runs = self._cache.get_or(("ci", ctx.cwd, ctx.head_sha),
                                  lambda: self._run_gh(ctx.cwd, ctx.branch))
        for run in runs:
            if run.get("headSha") == ctx.head_sha and run.get("status") in _ACTIVE:
                wf = run.get("workflowName") or "ci"
                return Pending(label=f"CI: {wf}", priority=PRIORITY, source=self.name)
        return None
