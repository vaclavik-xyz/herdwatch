from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import Callable

from .models import WorktreeHead


@dataclass(frozen=True)
class GitInfo:
    is_git_repo: bool
    head_sha: str | None
    branch: str | None
    has_github_remote: bool
    worktree_heads: tuple[WorktreeHead, ...] = ()
    repo_key: str | None = None


def _run_git(args: list[str], cwd: str) -> tuple[int, str]:
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                           text=True, timeout=5)
        return r.returncode, r.stdout.strip()
    except Exception:
        return 1, ""


def _parse_worktrees(porcelain: str) -> tuple[WorktreeHead, ...]:
    heads = []
    sha, branch = None, None
    for line in porcelain.splitlines() + [""]:
        if not line:  # blank line ends a worktree block
            if sha:
                heads.append(WorktreeHead(head_sha=sha, branch=branch))
            sha, branch = None, None
        elif line.startswith("HEAD "):
            sha = line[len("HEAD "):]
        elif line.startswith("branch refs/heads/"):
            branch = line[len("branch refs/heads/"):]
    return tuple(heads)


def enrich(cwd: str, run: Callable[[list[str], str], tuple[int, str]] = _run_git) -> GitInfo:
    rc, _ = run(["rev-parse", "--is-inside-work-tree"], cwd)
    if rc != 0:
        return GitInfo(False, None, None, False)
    _, head = run(["rev-parse", "HEAD"], cwd)
    _, branch = run(["branch", "--show-current"], cwd)
    _, remote = run(["remote", "get-url", "origin"], cwd)
    _, worktrees = run(["worktree", "list", "--porcelain"], cwd)
    _, common_dir = run(["rev-parse", "--git-common-dir"], cwd)
    repo_key = None
    if common_dir:
        repo_key = os.path.realpath(
            common_dir if os.path.isabs(common_dir)
            else os.path.join(cwd, common_dir)
        )
    return GitInfo(
        is_git_repo=True,
        head_sha=head or None,
        branch=branch or None,
        has_github_remote="github.com" in remote,
        worktree_heads=_parse_worktrees(worktrees),
        repo_key=repo_key,
    )
