from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Pending:
    label: str
    priority: int
    source: str


@dataclass(frozen=True)
class PaneContext:
    pane_id: str
    agent: str
    cwd: str
    status: str
    head_sha: str | None
    branch: str | None
    is_git_repo: bool
    has_github_remote: bool
