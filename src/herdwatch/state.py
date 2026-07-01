# src/herdwatch/state.py
"""File-backed snapshot of the daemon's live managed-pane set.

The daemon runs as a separate process from `herdwatch status`, so it publishes
which panes it currently holds (and why) to a JSON state file. `status` reads
that file. The snapshot records the daemon's pid so a reader can tell a live
snapshot from one a dead daemon left behind.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

STATE_PATH = os.path.expanduser("~/.local/state/herdwatch/managed.json")


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    except OSError:
        return False
    return True


@dataclass
class ManagedSnapshot:
    pid: int
    updated_at: float
    panes: list[dict]


class StateStore:
    def __init__(self, path: str = STATE_PATH,
                 now: Callable[[], float] = time.time,
                 getpid: Callable[[], int] = os.getpid) -> None:
        self._path = Path(path)
        self._now = now
        self._getpid = getpid

    def write(self, panes: list[dict]) -> None:
        data = {"pid": self._getpid(), "updated_at": self._now(), "panes": panes}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(self._path)  # atomic swap

    def read(self) -> ManagedSnapshot | None:
        try:
            data = json.loads(self._path.read_text())
        except (OSError, ValueError):
            return None
        return ManagedSnapshot(
            pid=int(data.get("pid", 0)),
            updated_at=float(data.get("updated_at", 0.0)),
            panes=list(data.get("panes", [])),
        )
