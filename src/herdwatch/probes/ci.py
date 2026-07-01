from __future__ import annotations

import json
import subprocess
from typing import Callable

from ..cache import TTLCache
from ..models import PaneContext, Pending, WorktreeHead

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
        # check every checkout of the repo, not just cwd's: the agent's work
        # (and its PR CI) often lives on a branch in a linked worktree while
        # the pane cwd stays on the main checkout
        heads = ctx.worktree_heads or (WorktreeHead(ctx.head_sha, ctx.branch),)
        for head in heads:
            runs = self._cache.get_or(
                ("ci", ctx.cwd, head.head_sha, head.branch),
                lambda br=head.branch: self._run_gh(ctx.cwd, br))
            if not isinstance(runs, list):
                continue
            for run in runs:
                if not isinstance(run, dict):
                    continue
                if run.get("headSha") == head.head_sha and run.get("status") in _ACTIVE:
                    wf = run.get("workflowName") or "ci"
                    return Pending(label=f"CI: {wf}", priority=PRIORITY, source=self.name)
        return None
