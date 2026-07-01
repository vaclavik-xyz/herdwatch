from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable


@dataclass
class Marker:
    id: str
    pane_id: str
    label: str
    until: str | None
    pid: int | None
    expires_at: float | None


def _run_cmd(cmd: str) -> int:
    try:
        return subprocess.run(cmd, shell=True, timeout=10,
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode
    except Exception:
        return 1


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class MarkerStore:
    def __init__(self, dir: Path, now: Callable[[], float] = time.time,
                 run_cmd: Callable[[str], int] = _run_cmd,
                 pid_alive: Callable[[int], bool] = _pid_alive) -> None:
        self._dir = Path(dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._now = now
        self._run_cmd = run_cmd
        self._pid_alive = pid_alive

    def _path(self, marker_id: str) -> Path:
        return self._dir / f"{marker_id}.json"

    def add(self, pane_id: str, label: str, until: str | None = None,
            pid: int | None = None, ttl_s: float | None = None) -> Marker:
        expires_at = (self._now() + ttl_s) if ttl_s is not None else None
        m = Marker(uuid.uuid4().hex[:8], pane_id, label, until, pid, expires_at)
        self._path(m.id).write_text(json.dumps(asdict(m)))
        return m

    def all(self) -> list[Marker]:
        out = []
        for f in self._dir.glob("*.json"):
            try:
                out.append(Marker(**json.loads(f.read_text())))
            except Exception:
                continue
        return out

    def remove(self, marker_id: str) -> None:
        self._path(marker_id).unlink(missing_ok=True)

    def is_pending(self, m: Marker) -> bool:
        if m.expires_at is not None and self._now() >= m.expires_at:
            return False
        if m.pid is not None and not self._pid_alive(m.pid):
            return False
        if m.until is not None and self._run_cmd(m.until) == 0:
            return False
        return True

    def active_for_pane(self, pane_id: str) -> list[Marker]:
        active = []
        for m in self.all():
            if m.pane_id != pane_id:
                continue
            if self.is_pending(m):
                active.append(m)
            else:
                self.remove(m.id)
        return active
