from __future__ import annotations

import json
import subprocess
from typing import Callable

from ..aggregate import aggregate
from ..cache import TTLCache
from ..models import PaneContext, PanePeer, Pending, WorktreeHead

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
        self._owners: dict[tuple[str, str, str | None], str] = {}

    def _owner_for(
        self,
        ctx: PaneContext,
        head: WorktreeHead,
        label: str,
    ) -> str | None:
        peers = ctx.repo_peers or (
            PanePeer(
                ctx.pane_id,
                ctx.status,
                ctx.head_sha,
                ctx.branch,
            ),
        )
        peer_ids = {peer.pane_id for peer in peers}
        key = (ctx.repo_key or ctx.cwd, head.head_sha, head.branch)
        existing = self._owners.get(key)
        if existing not in peer_ids:
            existing = None
            self._owners.pop(key, None)

        sha_matches = [
            peer for peer in peers if peer.head_sha == head.head_sha
        ]
        branch_matches = [
            peer
            for peer in sha_matches
            if head.branch is not None and peer.branch == head.branch
        ]
        exact = branch_matches if head.branch is not None else sha_matches
        if len(exact) == 1:
            owner = exact[0].pane_id
        elif len(exact) > 1:
            if existing is not None and existing in {
                peer.pane_id for peer in exact
            }:
                owner = existing
            else:
                working = [
                    peer
                    for peer in exact
                    if peer.status == "working" and not peer.herdwatch_hold
                ]
                if len(working) != 1:
                    return None
                owner = working[0].pane_id
        elif existing is not None:
            owner = existing
        else:
            working = [
                peer
                for peer in peers
                if peer.status == "working" and not peer.herdwatch_hold
            ]
            if len(working) == 1:
                owner = working[0].pane_id
            else:
                expected_status = aggregate([Pending(
                    label,
                    PRIORITY,
                    self.name,
                    show_while_working=True,
                )])
                labeled = [
                    peer
                    for peer in peers
                    if peer.progress == expected_status
                ]
                if len(labeled) == 1:
                    owner = labeled[0].pane_id
                elif len(peers) == 1:
                    owner = peers[0].pane_id
                else:
                    return None

        self._owners[key] = owner
        return owner

    def check(self, ctx: PaneContext) -> Pending | None:
        if not (ctx.is_git_repo and ctx.has_github_remote and ctx.head_sha):
            return None
        # check every checkout of the repo, not just cwd's: the agent's work
        # (and its PR CI) often lives on a branch in a linked worktree while
        # the pane cwd stays on the main checkout
        heads = ctx.worktree_heads or (WorktreeHead(ctx.head_sha, ctx.branch),)
        for head in heads:
            key = (ctx.repo_key or ctx.cwd, head.head_sha, head.branch)
            runs = self._cache.get_or(
                ("ci", ctx.cwd, head.head_sha, head.branch),
                lambda br=head.branch: self._run_gh(ctx.cwd, br))
            if not isinstance(runs, list):
                continue
            active_run = None
            for run in runs:
                if not isinstance(run, dict):
                    continue
                if run.get("headSha") == head.head_sha and run.get("status") in _ACTIVE:
                    active_run = run
                    break
            if active_run is None:
                self._owners.pop(key, None)
                continue
            wf = active_run.get("workflowName") or "ci"
            label = f"CI: {wf}"
            if self._owner_for(ctx, head, label) == ctx.pane_id:
                return Pending(
                    label=label,
                    priority=PRIORITY,
                    source=self.name,
                    show_while_working=True,
                )
        return None
