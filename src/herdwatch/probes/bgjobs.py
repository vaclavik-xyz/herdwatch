from __future__ import annotations

import subprocess
from typing import Callable

from ..models import PaneContext, Pending

PRIORITY = 10
_DEFAULT_IGNORE = frozenset({"claude", "codex", "node", "node_repl",
                             "caffeinate", "sh", "zsh", "bash"})


def _parse_etime(raw: str) -> float:
    # ps etime: [[dd-]hh:]mm:ss
    raw = raw.strip()
    if not raw:
        return 0.0
    days = 0
    if "-" in raw:
        d, raw = raw.split("-", 1)
        days = int(d)
    parts = [int(x) for x in raw.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, s = parts
    return days * 86400 + h * 3600 + m * 60 + s


def default_list_descendants(root_pid: int) -> list[dict]:
    try:
        r = subprocess.run(["ps", "-eo", "pid=,ppid=,pgid=,etime=,comm="],
                           capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return []
    except Exception:
        return []
    rows = []
    children: dict[int, list[dict]] = {}
    for line in r.stdout.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        pid, ppid, pgid, etime, comm = parts
        try:
            row = {"pid": int(pid), "ppid": int(ppid), "pgid": int(pgid),
                   "etime_s": _parse_etime(etime), "comm": comm.rsplit("/", 1)[-1]}
        except ValueError:
            continue
        rows.append(row)
        children.setdefault(row["ppid"], []).append(row)
    out, stack = [], [root_pid]
    seen = set()
    while stack:
        cur = stack.pop()
        for child in children.get(cur, []):
            if child["pid"] in seen:
                continue
            seen.add(child["pid"])
            out.append(child)
            stack.append(child["pid"])
    return out


class BgJobsProbe:
    name = "bgjobs"

    def __init__(self, process_info: Callable[[str], dict], min_age_s: float = 5.0,
                 list_descendants: Callable[[int], list[dict]] = default_list_descendants,
                 agent_names: frozenset = _DEFAULT_IGNORE) -> None:
        self._process_info = process_info
        self._min_age_s = min_age_s
        self._list_descendants = list_descendants
        self._ignore = agent_names

    def check(self, ctx: PaneContext) -> Pending | None:
        try:
            info = self._process_info(ctx.pane_id)
            shell_pid = info.get("shell_pid")
            fg_pgid = info.get("foreground_process_group_id")
            if not shell_pid:
                return None
            for p in self._list_descendants(shell_pid):
                if p.get("pgid") == fg_pgid:
                    continue
                if p.get("comm") in self._ignore:
                    continue
                if p.get("etime_s", 0) < self._min_age_s:
                    continue
                return Pending(label=p.get("comm", "job"), priority=PRIORITY, source=self.name)
            return None
        except Exception:
            return None
