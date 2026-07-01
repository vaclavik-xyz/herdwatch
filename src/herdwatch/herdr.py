from __future__ import annotations

import json
import subprocess
from typing import Callable


def _run(args: list[str]) -> tuple[int, str]:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=10)
        return r.returncode, r.stdout
    except Exception:
        return 1, ""


class HerdrClient:
    def __init__(self, herdr_bin: str = "herdr",
                 run: Callable[[list[str]], tuple[int, str]] = _run) -> None:
        self._bin = herdr_bin
        self._run = run

    def _json(self, args: list[str]) -> dict:
        rc, out = self._run(args)
        if rc != 0 or not out.strip():
            return {}
        try:
            return json.loads(out)
        except Exception:
            return {}

    def agent_list(self) -> list[dict]:
        data = self._json([self._bin, "agent", "list"])
        return (data.get("result") or {}).get("agents", [])

    def pane_process_info(self, pane_id: str) -> dict:
        data = self._json([self._bin, "pane", "process-info", "--pane", pane_id])
        return (data.get("result") or {}).get("process_info", {})

    def report_agent(self, pane_id: str, source: str, agent: str, state: str,
                     custom_status: str | None = None) -> bool:
        args = [self._bin, "pane", "report-agent", pane_id, "--source", source,
                "--agent", agent, "--state", state]
        if custom_status:
            args += ["--custom-status", custom_status]
        rc, _ = self._run(args)
        return rc == 0

    def release_agent(self, pane_id: str, source: str, agent: str) -> bool:
        rc, _ = self._run([self._bin, "pane", "release-agent", pane_id,
                           "--source", source, "--agent", agent])
        return rc == 0
