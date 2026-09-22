"""Pending Claude Code background tasks, read from the session transcript.

Claude Code ends its turn (herdr then shows the pane idle) while background
Bash commands, async subagents, Monitors, and Workflows keep running and
report back later through ``<task-notification>`` messages. None of that is
visible to herdr, so this probe reconstructs it from the transcript
``~/.claude/projects/<project>/<session_id>.jsonl``:

* a launch is a tool result whose structured ``toolUseResult`` carries
  ``backgroundTaskId`` (Bash), ``status == "async_launched"`` (Agent,
  Workflow), or ``taskId`` + ``timeoutMs`` (Monitor);
* a task ends with a ``<task-notification>`` whose ``<status>`` is terminal,
  or with a successful ``TaskStop``/``KillShell`` result.

The transcript format is an internal Claude Code detail, not a contract, so
parsing is deliberately tolerant: anything unrecognised is ignored and the
probe degrades to "nothing pending". Tasks never outlive their Claude
process, so launches older than the pane's current ``claude`` process (a
resumed session) are discarded, as are launches older than ``max_age_s``.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from ..models import PaneContext, Pending
from .bgjobs import _parse_etime

log = logging.getLogger(__name__)

PRIORITY = 35
PROJECTS_ROOT = os.path.expanduser("~/.claude/projects")
DEFAULT_MAX_AGE_S = 6 * 3600.0
# Launch timestamps and process start times come from different clocks
# (transcript writer vs. ps); allow a little slack before calling a task stale.
START_SLACK_S = 5.0
# Monitor notifications stream events while the watch is live; only these
# statuses mean "still running". Anything else ends the task.
RUNNING_STATUSES = frozenset({"running", "pending", "in_progress", "started"})
STOP_TOOLS = frozenset({"TaskStop", "KillShell", "KillBash"})
_NOTIFICATION = re.compile(r"<task-notification>(.*?)</task-notification>", re.S)
_TASK_ID = re.compile(r"<task-id>\s*([\w-]+)\s*</task-id>")
_STATUS = re.compile(r"<status>\s*(\w+)\s*</status>")
# Cheap substring pre-filter: only these lines can launch, stop, or end a task.
_INTERESTING = ('"toolUseResult"', "task-notification", '"tool_use"')
# Agents also background long-lived services (dev servers, file watchers)
# that never finish and that nobody waits on. Labelling those would leave a
# permanent, misleading "waiting" marker, so background Bash commands (or
# descriptions) matching one of these patterns are not treated as pending.
DEFAULT_SERVICE_PATTERNS = (
    r"\bdev[ -]?server\b",
    r"\b(next|vite|nuxt|astro|remix|wrangler|expo|storybook|turbo)\s+"
    r"(dev|start|preview|serve)\b",
    r"\b(npm|pnpm|yarn|bun)\s+(run\s+)?(dev|start|serve|preview|storybook)\b",
    r"\bhttp\.server\b",
    r"\b(uvicorn|gunicorn|flask\s+run|rails\s+s(erver)?|hugo\s+server)\b",
    r"\bdocker[ -]compose\s+up\b(?!.*\s--abort-on-container-exit)",
)


@dataclass
class Task:
    task_id: str
    kind: str
    label: str
    launched_at: float
    deadline: float | None = None
    command: str = ""


@dataclass
class _Transcript:
    """Incremental parse state for one transcript file."""
    inode: int = -1
    offset: int = 0
    tasks: dict[str, Task] = field(default_factory=dict)
    finished: set[str] = field(default_factory=set)
    # tool_use id -> (tool name, input) for launches whose result comes later
    tool_uses: dict[str, tuple[str, dict]] = field(default_factory=dict)


def _epoch(ts) -> float | None:
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _content_items(record: dict) -> list[dict]:
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    return [item for item in content if isinstance(item, dict)]


def _describe(tool: str, tool_input: dict, result: dict) -> str:
    for value in (
        tool_input.get("description"),
        result.get("description"),
        result.get("workflowName"),
        result.get("summary"),
    ):
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    return tool or "task"


class TranscriptTasks:
    """Track background task launches and completions across polls."""

    def __init__(self) -> None:
        self._states: dict[str, _Transcript] = {}

    def pending(self, path: str) -> list[Task]:
        state = self._states.get(path)
        try:
            st = os.stat(path)
        except OSError:
            self._states.pop(path, None)
            return []
        if state is None or state.inode != st.st_ino or st.st_size < state.offset:
            # new, replaced, or truncated file: parse from the start
            state = _Transcript(inode=st.st_ino)
            self._states[path] = state
        if st.st_size > state.offset:
            self._read(path, state)
        return [
            task for task_id, task in state.tasks.items()
            if task_id not in state.finished
        ]

    def _read(self, path: str, state: _Transcript) -> None:
        try:
            with open(path, "rb") as handle:
                handle.seek(state.offset)
                data = handle.read()
        except OSError:
            return
        # Only consume complete lines; a partially written tail is re-read
        # on the next poll.
        end = data.rfind(b"\n")
        if end < 0:
            return
        state.offset += end + 1
        for raw in data[: end + 1].splitlines():
            line = raw.decode("utf-8", "replace")
            if not any(marker in line for marker in _INTERESTING):
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                self._apply(record, state)

    def _apply(self, record: dict, state: _Transcript) -> None:
        kind = record.get("type")
        if kind == "queue-operation":
            if record.get("operation") == "enqueue":
                self._notifications(record.get("content"), state)
            return
        items = _content_items(record)
        if kind == "assistant":
            for item in items:
                if item.get("type") == "tool_use" and isinstance(item.get("id"), str):
                    tool_input = item.get("input")
                    state.tool_uses[item["id"]] = (
                        str(item.get("name") or ""),
                        tool_input if isinstance(tool_input, dict) else {},
                    )
            return
        if kind != "user":
            return
        self._notifications(record.get("message"), state)
        result = record.get("toolUseResult")
        if not isinstance(result, dict):
            return
        tool_use_id = next(
            (
                item.get("tool_use_id") for item in items
                if item.get("type") == "tool_result"
            ),
            None,
        )
        tool, tool_input = state.tool_uses.pop(tool_use_id, ("", {}))
        launched_at = _epoch(record.get("timestamp")) or time.time()
        self._launch_or_stop(tool, tool_input, result, launched_at, state)

    @staticmethod
    def _launch_or_stop(
        tool: str, tool_input: dict, result: dict, at: float, state: _Transcript
    ) -> None:
        if tool in STOP_TOOLS:
            task_id = result.get("task_id") or result.get("shell_id")
            if isinstance(task_id, str):
                state.finished.add(task_id)
            return
        task_id = None
        kind = ""
        deadline = None
        if isinstance(result.get("backgroundTaskId"), str):
            task_id, kind = result["backgroundTaskId"], "bash"
        elif result.get("status") == "async_launched":
            task_id = result.get("agentId") or result.get("taskId")
            kind = "agent" if result.get("agentId") else str(
                result.get("taskType") or "task"
            )
        elif isinstance(result.get("taskId"), str) and "timeoutMs" in result:
            task_id, kind = result["taskId"], "monitor"
            timeout_ms = result.get("timeoutMs")
            if not result.get("persistent") and isinstance(timeout_ms, (int, float)):
                deadline = at + timeout_ms / 1000.0
        if not isinstance(task_id, str) or not task_id:
            return
        command = tool_input.get("command")
        state.tasks[task_id] = Task(
            task_id=task_id,
            kind=kind,
            label=_describe(tool, tool_input, result),
            launched_at=at,
            deadline=deadline,
            command=command if isinstance(command, str) else "",
        )

    @staticmethod
    def _notifications(payload, state: _Transcript) -> None:
        for text in _strings(payload):
            if "<task-notification>" not in text:
                continue
            for block in _NOTIFICATION.findall(text):
                task_id = _TASK_ID.search(block)
                status = _STATUS.search(block)
                if task_id and status and status.group(1) not in RUNNING_STATUSES:
                    state.finished.add(task_id.group(1))


def default_find_transcript(session_id: str, root: str = PROJECTS_ROOT) -> str | None:
    if not re.fullmatch(r"[\w-]+", session_id):
        return None
    matches = glob.glob(os.path.join(glob.escape(root), "*", f"{session_id}.jsonl"))
    if not matches:
        return None
    return max(matches, key=lambda p: os.path.getmtime(p))


def default_process_started_at(pid: int) -> float | None:
    try:
        r = subprocess.run(
            ["ps", "-o", "etime=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return None
    raw = r.stdout.strip()
    if r.returncode != 0 or not raw:
        return None
    try:
        return time.time() - _parse_etime(raw)
    except ValueError:
        return None


def claude_pid(info: dict) -> int | None:
    """Pick the Claude Code process out of herdr's pane process info."""
    for proc in info.get("foreground_processes") or ():
        if not isinstance(proc, dict):
            continue
        argv0 = str(proc.get("argv0") or "")
        if os.path.basename(argv0) == "claude" and isinstance(proc.get("pid"), int):
            return proc["pid"]
    return None


def format_label(tasks: list[Task]) -> str:
    if len(tasks) == 1:
        return f"bg: {tasks[0].label}"
    return f"bg: {len(tasks)} tasks"


class ClaudeTasksProbe:
    name = "claude_tasks"

    def __init__(
        self,
        process_info: Callable[[str], dict],
        *,
        max_age_s: float = DEFAULT_MAX_AGE_S,
        service_patterns=DEFAULT_SERVICE_PATTERNS,
        extra_service_patterns=(),
        find_transcript: Callable[[str], str | None] = default_find_transcript,
        process_started_at: Callable[[int], float | None] = default_process_started_at,
        clock: Callable[[], float] = time.time,
        tracker: TranscriptTasks | None = None,
    ) -> None:
        self._process_info = process_info
        self._max_age_s = max_age_s
        self._services = []
        for pattern in (*service_patterns, *extra_service_patterns):
            try:
                self._services.append(re.compile(pattern, re.I))
            except re.error:
                log.warning("claude_tasks: ignoring invalid pattern %r", pattern)
        self._find_transcript = find_transcript
        self._process_started_at = process_started_at
        self._clock = clock
        self._tracker = tracker or TranscriptTasks()
        self._paths: dict[str, str] = {}

    def _transcript(self, session_id: str) -> str | None:
        path = self._paths.get(session_id)
        if path and os.path.exists(path):
            return path
        path = self._find_transcript(session_id)
        if path:
            self._paths[session_id] = path
        else:
            self._paths.pop(session_id, None)
        return path

    def _agent_started_at(self, pane_id: str) -> float | None:
        try:
            pid = claude_pid(self._process_info(pane_id) or {})
        except Exception:
            return None
        return self._process_started_at(pid) if pid is not None else None

    def _is_service(self, task: Task) -> bool:
        if task.kind != "bash":
            return False
        return any(
            rx.search(text)
            for rx in self._services
            for text in (task.command, task.label)
            if text
        )

    def check(self, ctx: PaneContext) -> Pending | None:
        if ctx.agent != "claude" or not ctx.agent_session:
            return None
        path = self._transcript(ctx.agent_session)
        if not path:
            return None
        now = self._clock()
        tasks = [
            task for task in self._tracker.pending(path)
            if now - task.launched_at <= self._max_age_s
            and (task.deadline is None or now < task.deadline)
            and not self._is_service(task)
        ]
        if not tasks:
            return None
        started_at = self._agent_started_at(ctx.pane_id)
        if started_at is not None:
            tasks = [
                task for task in tasks
                if task.launched_at >= started_at - START_SLACK_S
            ]
        if not tasks:
            return None
        tasks.sort(key=lambda task: task.launched_at)
        return Pending(label=format_label(tasks), priority=PRIORITY, source=self.name)
