from __future__ import annotations

from dataclasses import dataclass

# shared cap for pane labels reported to herdr (custom_status)
LABEL_MAX_LEN = 32


@dataclass(frozen=True)
class Pending:
    label: str
    priority: int
    source: str


@dataclass(frozen=True)
class WorktreeHead:
    head_sha: str
    branch: str | None


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
    # HEAD of every checkout of the repo (main + linked worktrees): agents
    # often work on a branch in a worktree while the pane cwd stays on the
    # main checkout, so probes must not assume cwd's HEAD is where work happens
    worktree_heads: tuple[WorktreeHead, ...] = ()
