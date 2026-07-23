from __future__ import annotations

from dataclasses import dataclass

# Shared cap for display labels reported as Herdr metadata token values.
LABEL_MAX_LEN = 32


@dataclass(frozen=True)
class Pending:
    label: str
    priority: int
    source: str
    show_while_working: bool = False


@dataclass(frozen=True)
class WorktreeHead:
    head_sha: str
    branch: str | None


@dataclass(frozen=True)
class PanePeer:
    pane_id: str
    status: str
    head_sha: str | None
    branch: str | None
    progress: str | None = None
    herdwatch_hold: bool = False


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
    # Stable identity shared by linked worktrees of one local repository.
    repo_key: str | None = None
    # Other eligible panes backed by the same local repository. Probes that
    # represent exclusive work can use this to choose one owning pane.
    repo_peers: tuple[PanePeer, ...] = ()
