from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .models import LABEL_MAX_LEN

# Claude Code persists each session's task list as one JSON file per task in
# ~/.claude/tasks/session-<first 8 chars of the session uuid>/<N>.json
TASKS_ROOT = os.path.expanduser("~/.claude/tasks")


@dataclass(frozen=True)
class Progress:
    done: int
    total: int
    active: str


def read_progress(session_id: str, root: str = TASKS_ROOT) -> Progress | None:
    """Read a Claude Code session's task list. Returns None unless the list
    has at least two tasks and one of them is in progress — the in_progress
    requirement filters both finished lists and stale lists left over from
    earlier requests in the same session."""
    d = Path(root) / f"session-{session_id[:8]}"
    tasks: list[dict] = []
    try:
        files = sorted((f for f in d.iterdir()
                        if f.suffix == ".json" and f.stem.isdigit()),
                       key=lambda f: int(f.stem))
    except OSError:
        return None
    for f in files:
        try:
            data = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            tasks.append(data)
    total = len(tasks)
    if total < 2:
        return None
    done = sum(1 for t in tasks if t.get("status") == "completed")
    active = next((t for t in tasks if t.get("status") == "in_progress"), None)
    if active is None:
        return None
    name = active.get("activeForm") or active.get("subject") or ""
    return Progress(done=done, total=total, active=str(name))


def format_label(p: Progress) -> str:
    label = f"{min(p.done + 1, p.total)}/{p.total} {p.active}".rstrip()
    if len(label) > LABEL_MAX_LEN:
        label = label[:LABEL_MAX_LEN - 1] + "…"
    return label


def progress_label(session_id: str, root: str = TASKS_ROOT) -> str | None:
    p = read_progress(session_id, root)
    return format_label(p) if p else None
