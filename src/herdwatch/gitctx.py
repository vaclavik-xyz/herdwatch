from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class GitInfo:
    is_git_repo: bool
    head_sha: str | None
    branch: str | None
    has_github_remote: bool


def _run_git(args: list[str], cwd: str) -> tuple[int, str]:
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                           text=True, timeout=5)
        return r.returncode, r.stdout.strip()
    except Exception:
        return 1, ""


def enrich(cwd: str, run: Callable[[list[str], str], tuple[int, str]] = _run_git) -> GitInfo:
    rc, _ = run(["rev-parse", "--is-inside-work-tree"], cwd)
    if rc != 0:
        return GitInfo(False, None, None, False)
    _, head = run(["rev-parse", "HEAD"], cwd)
    _, branch = run(["branch", "--show-current"], cwd)
    _, remote = run(["remote", "get-url", "origin"], cwd)
    return GitInfo(
        is_git_repo=True,
        head_sha=head or None,
        branch=branch or None,
        has_github_remote="github.com" in remote,
    )
