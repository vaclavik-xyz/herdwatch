# Task Progress in Sidebar Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** While a Claude Code agent works through a task list, show `3/7 <current task>` for its pane in the herdr sidebar, and drop the label the moment the agent really stops.

**Architecture:** The daemon gains a "progress" path alongside the existing ⏳ "hold" path. For working claude panes it reads the session's task files from `~/.claude/tasks/session-<uuid[:8]>/` and asserts `working` + label via `herdr pane report-agent`. Because that assertion masks herdr's own detection, panes we decorate are re-checked each tick with `herdr agent explain --json` (live screen detection); when detection says the agent stopped, the label is released and the existing idle/hold flow takes over in the same tick.

**Tech Stack:** Python 3.12+ stdlib only (same as the rest of herdwatch), pytest.

**Spec:** `docs/superpowers/specs/2026-07-02-task-progress-design.md`

## Global Constraints

- Labels are capped at 32 characters (existing `custom_status` limit; becomes shared constant `LABEL_MAX_LEN` in `models.py`).
- Progress shows only when the task list has `total >= 2` and at least one task `in_progress`.
- Progress applies only to agents whose label is exactly `claude`.
- `[progress] enabled` defaults to `true`.
- On any ambiguity (explain failure, malformed files) prefer releasing over keeping the label — never mask a waiting pane.
- All commits: conventional format, no Co-Authored-By. After each commit, check `roborev show <sha>` and address findings.
- Run tests with `.venv/bin/python -m pytest` from the repo root.

---

### Task 1: Progress reader module + shared label-length constant

**Files:**
- Create: `src/herdwatch/progress.py`
- Create: `tests/test_progress.py`
- Modify: `src/herdwatch/models.py` (add `LABEL_MAX_LEN = 32`)
- Modify: `src/herdwatch/aggregate.py` (use the shared constant)

**Interfaces:**
- Consumes: nothing new.
- Produces: `Progress(done: int, total: int, active: str)` frozen dataclass; `read_progress(session_id: str, root: str = TASKS_ROOT) -> Progress | None`; `format_label(p: Progress) -> str`; `progress_label(session_id: str, root: str = TASKS_ROOT) -> str | None` (composition used by the daemon); `models.LABEL_MAX_LEN: int = 32`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_progress.py`:

```python
import json
from pathlib import Path

from herdwatch.progress import Progress, format_label, progress_label, read_progress

SESSION = "c00b128f-68c8-4643-82d6-2835c317517d"


def _write_tasks(root: Path, tasks: list[dict | str]) -> None:
    d = root / f"session-{SESSION[:8]}"
    d.mkdir(parents=True, exist_ok=True)
    for i, t in enumerate(tasks, start=1):
        body = t if isinstance(t, str) else json.dumps(t)
        (d / f"{i}.json").write_text(body)


def _task(status, subject="Do thing", active_form=None):
    t = {"subject": subject, "status": status}
    if active_form is not None:
        t["activeForm"] = active_form
    return t


def test_missing_dir_returns_none(tmp_path):
    assert read_progress(SESSION, root=str(tmp_path)) is None


def test_counts_and_active_task(tmp_path):
    _write_tasks(tmp_path, [
        _task("completed"),
        _task("in_progress", subject="Fix auth", active_form="Fixing auth"),
        _task("pending"),
    ])
    p = read_progress(SESSION, root=str(tmp_path))
    assert p == Progress(done=1, total=3, active="Fixing auth")


def test_active_falls_back_to_subject(tmp_path):
    _write_tasks(tmp_path, [_task("in_progress", subject="Fix auth"), _task("pending")])
    p = read_progress(SESSION, root=str(tmp_path))
    assert p is not None and p.active == "Fix auth"


def test_first_in_progress_in_numeric_order(tmp_path):
    # 10 tasks so lexicographic order (1,10,2,...) would pick the wrong one
    tasks = [_task("completed") for _ in range(9)] + [_task("in_progress", subject="Ten")]
    d = tmp_path / f"session-{SESSION[:8]}"
    d.mkdir(parents=True)
    for i, t in enumerate(tasks, start=1):
        (d / f"{i}.json").write_text(json.dumps(t))
    (d / "2.json").write_text(json.dumps(_task("in_progress", subject="Two")))
    p = read_progress(SESSION, root=str(tmp_path))
    assert p is not None and p.active == "Two"


def test_none_when_no_in_progress(tmp_path):
    _write_tasks(tmp_path, [_task("completed"), _task("pending")])
    assert read_progress(SESSION, root=str(tmp_path)) is None


def test_none_when_all_completed(tmp_path):
    _write_tasks(tmp_path, [_task("completed"), _task("completed")])
    assert read_progress(SESSION, root=str(tmp_path)) is None


def test_none_when_single_task(tmp_path):
    _write_tasks(tmp_path, [_task("in_progress")])
    assert read_progress(SESSION, root=str(tmp_path)) is None


def test_malformed_and_non_dict_files_skipped(tmp_path):
    _write_tasks(tmp_path, [
        "not json{",
        json.dumps(["a", "list"]),
        _task("in_progress", subject="Real"),
        _task("pending"),
    ])
    p = read_progress(SESSION, root=str(tmp_path))
    assert p == Progress(done=0, total=2, active="Real")


def test_non_numeric_filenames_ignored(tmp_path):
    _write_tasks(tmp_path, [_task("in_progress"), _task("pending")])
    d = tmp_path / f"session-{SESSION[:8]}"
    (d / ".lock").write_text("")
    (d / "notes.json").write_text(json.dumps(_task("in_progress")))
    p = read_progress(SESSION, root=str(tmp_path))
    assert p is not None and p.total == 2


def test_format_label_counts_current_task():
    assert format_label(Progress(done=2, total=7, active="Fixing auth")) == "3/7 Fixing auth"


def test_format_label_clamps_done_overflow():
    # all-but-active completed: done+1 must not exceed total
    assert format_label(Progress(done=7, total=7, active="Last")) == "7/7 Last"


def test_format_label_truncates_to_32_with_ellipsis():
    label = format_label(Progress(done=0, total=2, active="A" * 60))
    assert len(label) == 32 and label.endswith("…") and label.startswith("1/2 A")


def test_progress_label_composes(tmp_path):
    _write_tasks(tmp_path, [_task("completed"), _task("in_progress", subject="Go")])
    assert progress_label(SESSION, root=str(tmp_path)) == "2/2 Go"
    assert progress_label("00000000-none", root=str(tmp_path)) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_progress.py -q`
Expected: collection ERROR — `ModuleNotFoundError: No module named 'herdwatch.progress'`

- [ ] **Step 3: Add the shared constant and the module**

In `src/herdwatch/models.py`, add after the imports (before `Pending`):

```python
# shared cap for pane labels reported to herdr (custom_status)
LABEL_MAX_LEN = 32
```

In `src/herdwatch/aggregate.py`, replace `MAX_LEN = 32` with an import so both label producers share one cap:

```python
from .models import LABEL_MAX_LEN as MAX_LEN, Pending
```

(keep the local name `MAX_LEN` so `aggregate.MAX_LEN` and existing tests keep working; drop the now-duplicate `from .models import Pending` line and the `MAX_LEN = 32` assignment)

Create `src/herdwatch/progress.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_progress.py tests/test_aggregate.py tests/test_models.py -q`
Expected: all PASS

- [ ] **Step 5: Run the full suite and commit**

Run: `.venv/bin/python -m pytest -q` — expected: all pass.

```bash
git add src/herdwatch/progress.py src/herdwatch/models.py src/herdwatch/aggregate.py tests/test_progress.py
git commit -m "feat(progress): read Claude Code task lists into a sidebar label"
```

Then check `roborev show <sha>` (review runs via post-commit hook; wait for it) and fix any findings before proceeding.

---

### Task 2: `[progress]` config section

**Files:**
- Modify: `src/herdwatch/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `Config.progress_enabled: bool = True`, parsed from `[progress] enabled`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py` (follow the file's existing style — it writes a TOML file to `tmp_path` and calls `load(str(path))`; reuse its existing helper if one exists):

```python
def test_progress_enabled_default_true(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("")
    assert load(str(p)).progress_enabled is True


def test_progress_can_be_disabled(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[progress]\nenabled = false\n")
    assert load(str(p)).progress_enabled is False


def test_progress_ignores_non_bool(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[progress]\nenabled = 'yes'\n")
    assert load(str(p)).progress_enabled is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_config.py -q`
Expected: FAIL — `AttributeError: 'Config' object has no attribute 'progress_enabled'`

- [ ] **Step 3: Implement**

In `src/herdwatch/config.py`, add a field to `Config` (after `bgjobs_ignore`):

```python
    progress_enabled: bool = True
```

In `load()`, before `panes = data.get("panes", {})`:

```python
    prog = data.get("progress", {})
    if isinstance(prog, dict) and isinstance(prog.get("enabled"), bool):
        cfg.progress_enabled = prog["enabled"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_config.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/herdwatch/config.py tests/test_config.py
git commit -m "feat(config): add [progress] enabled toggle"
```

Then check `roborev show <sha>` and fix any findings.

---

### Task 3: `HerdrClient.agent_explain`

**Files:**
- Modify: `src/herdwatch/herdr.py`
- Test: `tests/test_herdr.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `HerdrClient.agent_explain(pane_id: str) -> str | None` — the live screen-detected state (`"working"`, `"idle"`, ...) or None on failure. NOTE: `herdr agent explain --json` prints a **top-level** JSON object (no `{"result": ...}` wrapper); the state is its `state` key.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_herdr.py`:

```python
def test_agent_explain_returns_detected_state():
    payload = json.dumps({"agent": "claude", "state": "working", "evaluated_rules": []})
    calls = []
    client = HerdrClient(run=lambda args: calls.append(args) or (0, payload))
    assert client.agent_explain("w1:p1") == "working"
    assert calls[0] == ["herdr", "agent", "explain", "w1:p1", "--json"]


def test_agent_explain_none_on_error():
    assert HerdrClient(run=lambda a: (1, "")).agent_explain("w1:p1") is None


def test_agent_explain_none_on_missing_or_non_string_state():
    assert HerdrClient(run=lambda a: (0, json.dumps({"agent": "claude"}))).agent_explain("w1:p1") is None
    assert HerdrClient(run=lambda a: (0, json.dumps({"state": 5}))).agent_explain("w1:p1") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_herdr.py -q`
Expected: FAIL — `AttributeError: 'HerdrClient' object has no attribute 'agent_explain'`

- [ ] **Step 3: Implement**

Add to `HerdrClient` in `src/herdwatch/herdr.py` (after `pane_process_info`):

```python
    def agent_explain(self, pane_id: str) -> str | None:
        """Live screen-detected agent state, independent of reported
        sessions — the way to see the real state under our own assertion.
        `agent explain --json` prints a top-level object, not {"result":...}.
        """
        data = self._json([self._bin, "agent", "explain", pane_id, "--json"])
        state = data.get("state")
        return state if isinstance(state, str) else None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_herdr.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/herdwatch/herdr.py tests/test_herdr.py
git commit -m "feat(herdr): expose agent explain screen detection"
```

Then check `roborev show <sha>` and fix any findings.

---

### Task 4: `ManagedPane.kind` — bookkeeping and snapshot compatibility

**Files:**
- Modify: `src/herdwatch/daemon.py` (`ManagedPane`, `_rows`, `adopt`)
- Test: `tests/test_daemon.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `ManagedPane(custom_status: str, agent: str, kind: str = "hold")`; snapshot rows gain `"kind"`; `adopt()` defaults missing `kind` to `"hold"` (old state files keep working).

- [ ] **Step 1: Write the failing tests**

In `tests/test_daemon.py`, update the exact-row assertion in `test_tick_snapshots_managed_rows` (line ~233) to expect the new key:

```python
    assert snaps[-1] == [{"pane_id": "w1:p1", "agent": "claude",
                          "status": "⏳ review", "kind": "hold"}]
```

Add new tests after `test_adopt_ignores_rows_without_pane_id`:

```python
def test_adopt_defaults_kind_to_hold():
    d = Daemon(FakeClient([]), [], clock=lambda: 0.0, enrich=_ENRICH)
    d.adopt([{"pane_id": "w1:p1", "agent": "claude", "status": "⏳ review"}])
    assert d.managed["w1:p1"].kind == "hold"


def test_adopt_preserves_progress_kind():
    d = Daemon(FakeClient([]), [], clock=lambda: 0.0, enrich=_ENRICH)
    d.adopt([{"pane_id": "w1:p1", "agent": "claude",
              "status": "2/5 Fixing auth", "kind": "progress"}])
    assert d.managed["w1:p1"].kind == "progress"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_daemon.py -q`
Expected: FAIL — `ManagedPane` has no attribute/field `kind`, and the snapshot row assertion fails (no `"kind"` key).

- [ ] **Step 3: Implement**

In `src/herdwatch/daemon.py`:

```python
@dataclass
class ManagedPane:
    custom_status: str
    agent: str
    kind: str = "hold"  # "hold" = ⏳ wait assertion, "progress" = task-list label
```

In `_rows()`:

```python
    def _rows(self) -> list[dict]:
        return [{"pane_id": pid, "agent": mp.agent, "status": mp.custom_status,
                 "kind": mp.kind}
                for pid, mp in sorted(self.managed.items())]
```

In `adopt()`, extend the row unpacking:

```python
                self.managed[pane_id] = ManagedPane(row.get("status", ""),
                                                    row.get("agent", "agent"),
                                                    kind=row.get("kind", "hold"))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest -q`
Expected: all PASS (the whole suite — `status` CLI and state store treat rows as opaque dicts, so nothing else changes)

- [ ] **Step 5: Commit**

```bash
git add src/herdwatch/daemon.py tests/test_daemon.py
git commit -m "feat(daemon): tag managed panes with a hold/progress kind"
```

Then check `roborev show <sha>` and fix any findings.

---

### Task 5: Daemon progress path — assert, verify via explain, hand over

**Files:**
- Modify: `src/herdwatch/daemon.py` (constructor, `tick`, new `_progress_tick`, `build_daemon`)
- Modify: `docs/superpowers/specs/2026-07-02-task-progress-design.md` (session-resolution wording)
- Test: `tests/test_daemon.py`

**Interfaces:**
- Consumes: `progress_label(session_id) -> str | None` (Task 1), `Config.progress_enabled` (Task 2), `HerdrClient.agent_explain` (Task 3), `ManagedPane.kind` (Task 4).
- Produces: `Daemon(..., progress: Callable[[str], str | None] | None = None)`; working claude panes get `report_agent(pane, SOURCE, agent, "working", label)` with `kind="progress"` bookkeeping.

- [ ] **Step 1: Extend the fakes and write the failing tests**

In `tests/test_daemon.py`, extend `FakeClient` with explain support (add to `__init__` and as a method):

```python
class FakeClient:
    def __init__(self, agents, report_ok=True, release_ok=True, explain="idle"):
        self.agents = agents
        self.reports = []
        self.releases = []
        self.explains = []
        self._report_ok = report_ok
        self._release_ok = release_ok
        self.explain = explain  # state returned by agent_explain, or None
    def agent_list(self):
        return self.agents
    def agent_explain(self, pane_id):
        self.explains.append(pane_id)
        return self.explain
    def report_agent(self, pane_id, source, agent, state, custom_status=None):
        self.reports.append((pane_id, state, custom_status))
        return self._report_ok
    def release_agent(self, pane_id, source, agent):
        self.releases.append(pane_id)
        return self._release_ok
```

Add a helper next to `_agent` and the new tests at the end of the file:

```python
def _claude_agent(pane="w1:p1", status="working", session="c00b128f-68c8-4643-82d6-2835c317517d"):
    return {"pane_id": pane, "agent_status": status, "agent": "claude", "cwd": "/x",
            "agent_session": {"value": session}}


def test_progress_asserts_label_for_working_claude_pane():
    client = FakeClient([_claude_agent()])
    d = Daemon(client, [], clock=lambda: 0.0, enrich=_ENRICH,
               progress=lambda sid: "2/5 Fixing auth")
    d.tick()
    assert client.reports == [("w1:p1", "working", "2/5 Fixing auth")]
    assert d.managed["w1:p1"].kind == "progress"
    assert client.explains == []  # unmanaged pane: agent_status is the truth


def test_progress_reasserts_only_on_label_change():
    client = FakeClient([_claude_agent()], explain="working")
    d = Daemon(client, [], clock=lambda: 0.0, enrich=_ENRICH,
               progress=lambda sid: "2/5 Fixing auth")
    d.tick()
    d.tick()
    assert len(client.reports) == 1
    labels = iter(["3/5 Writing tests"])
    d._progress = lambda sid: next(labels)
    d.tick()
    assert client.reports[-1] == ("w1:p1", "working", "3/5 Writing tests")


def test_progress_released_when_detection_says_stopped():
    client = FakeClient([_claude_agent()], explain="working")
    d = Daemon(client, [], reprobe_interval_s=0, clock=lambda: 0.0, enrich=_ENRICH,
               progress=lambda sid: "2/5 Fixing auth")
    d.tick()
    client.explain = "idle"
    d.tick()
    assert client.releases == ["w1:p1"]
    assert "w1:p1" not in d.managed


def test_progress_hands_over_to_hold_in_same_tick():
    # agent stops with CI still running: release progress, assert ⏳ hold now
    client = FakeClient([_claude_agent()], explain="working")
    probe = StaticProbe(None)
    d = Daemon(client, [probe], reprobe_interval_s=0, clock=lambda: 0.0,
               enrich=_ENRICH, progress=lambda sid: "2/5 Fixing auth")
    d.tick()
    client.explain = "idle"
    probe.result = Pending("CI: ci", 20, "ci")
    d.tick()
    assert client.releases == ["w1:p1"]
    assert client.reports[-1] == ("w1:p1", "working", "⏳ CI: ci")
    assert d.managed["w1:p1"].kind == "hold"


def test_progress_released_when_no_active_task():
    client = FakeClient([_claude_agent()], explain="working")
    labels = iter(["2/5 Fixing auth", None])
    d = Daemon(client, [], clock=lambda: 0.0, enrich=_ENRICH,
               progress=lambda sid: next(labels))
    d.tick()
    d.tick()
    assert client.releases == ["w1:p1"]


def test_progress_released_when_explain_fails():
    client = FakeClient([_claude_agent()], explain="working")
    d = Daemon(client, [], clock=lambda: 0.0, enrich=_ENRICH,
               progress=lambda sid: "2/5 Fixing auth")
    d.tick()
    client.explain = None  # herdr hiccup: prefer releasing over masking
    d.tick()
    assert client.releases == ["w1:p1"]


def test_progress_skips_non_claude_agents():
    agent = {"pane_id": "w1:p1", "agent_status": "working", "agent": "codex",
             "cwd": "/x", "agent_session": {"value": "abc"}}
    client = FakeClient([agent])
    d = Daemon(client, [], clock=lambda: 0.0, enrich=_ENRICH,
               progress=lambda sid: "2/5 X")
    d.tick()
    assert client.reports == []


def test_progress_disabled_leaves_working_panes_alone():
    client = FakeClient([_claude_agent()])
    d = Daemon(client, [], clock=lambda: 0.0, enrich=_ENRICH, progress=None)
    d.tick()
    assert client.reports == []


def test_progress_reader_exception_is_contained():
    def boom(sid):
        raise RuntimeError("bad file")
    client = FakeClient([_claude_agent()])
    d = Daemon(client, [], clock=lambda: 0.0, enrich=_ENRICH, progress=boom)
    d.tick()  # must not raise
    assert client.reports == []


def test_hold_pane_not_probed_by_progress_path():
    # an idle pane held for CI must stay a hold even if its session has an
    # in_progress task (holds own the pane until their work clears)
    client = FakeClient([_claude_agent(status="idle")], explain="idle")
    probe = StaticProbe(Pending("CI: ci", 20, "ci"))
    d = Daemon(client, [probe], reprobe_interval_s=0, clock=lambda: 0.0,
               enrich=_ENRICH, progress=lambda sid: "2/5 X")
    d.tick()
    d.tick()
    assert d.managed["w1:p1"].kind == "hold"
    assert all(s == "⏳ CI: ci" for (_, _, s) in client.reports)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_daemon.py -q`
Expected: new tests FAIL — `Daemon.__init__() got an unexpected keyword argument 'progress'`

- [ ] **Step 3: Implement the progress path**

In `src/herdwatch/daemon.py`:

Constructor — add the parameter and store it (after `on_snapshot`):

```python
                 on_snapshot: Callable[[list[dict]], None] = lambda rows: None,
                 progress: Callable[[str], str | None] | None = None) -> None:
```

```python
        self._progress = progress
```

Add `_progress_tick` (below `_context`):

```python
    def _progress_tick(self, pane_id: str, agent: dict,
                       mp: ManagedPane | None, status: str) -> str | None:
        """Working-pane progress path. Returns the pane's true status when the
        idle/hold flow should continue with it, or None when the pane was
        handled here (it is genuinely working)."""
        if mp is not None:
            # our own assertion masks agent_status; screen detection does not
            status = self._client.agent_explain(pane_id) or "idle"
        if status != "working":
            if mp is not None:
                self._release(pane_id, "agent stopped")
            return status
        label = None
        if self._progress is not None and (agent.get("agent") or "") == "claude":
            session = (agent.get("agent_session") or {}).get("value")
            if session:
                try:
                    label = self._progress(session)
                except Exception:
                    log.warning("progress read failed; skipping", exc_info=True)
        if label:
            agent_name = agent.get("agent") or "agent"
            if (mp is not None and mp.custom_status == label
                    and pane_id not in self._adopted):
                pass  # already asserting this exact label
            elif self._client.report_agent(pane_id, SOURCE, agent_name, "working", label):
                self.managed[pane_id] = ManagedPane(label, agent_name, kind="progress")
                self._adopted.discard(pane_id)
                log.info("progress %s -> %s (%s)", pane_id, label, agent_name)
        elif mp is not None:
            self._release(pane_id, "no active task")
        if pane_id not in self.managed:
            # busy and unheld: forget the timer so a fresh idle edge probes
            # immediately (preserves the pre-progress behaviour)
            self._last_probe.pop(pane_id, None)
        return None
```

Rewire the top of the `tick()` pane loop — replace:

```python
            status = agent.get("agent_status") or "unknown"
            managed = pane_id in self.managed
            if not managed and status not in ("idle", "done"):
                # not ours and busy: forget its timer so a fresh idle edge
                # probes immediately
                self._last_probe.pop(pane_id, None)
                continue
```

with:

```python
            status = agent.get("agent_status") or "unknown"
            mp = self.managed.get(pane_id)
            if mp is None or mp.kind == "progress":
                fallthrough = self._progress_tick(pane_id, agent, mp, status)
                if fallthrough is None:
                    continue
                status = fallthrough
            managed = pane_id in self.managed
            if not managed and status not in ("idle", "done"):
                self._last_probe.pop(pane_id, None)
                continue
```

(hold-kind panes skip the progress path entirely and keep today's behaviour; a pane whose progress assertion was just released falls through with its *detected* status, so a ⏳ hold can take over in the same tick)

In `build_daemon`, import and wire the label provider:

```python
from .progress import progress_label
```

```python
    return Daemon(client, probes, reprobe_interval_s=config.reprobe_interval_s,
                  allow=config.allow, deny=config.deny,
                  on_snapshot=StateStore().write,
                  progress=progress_label if config.progress_enabled else None)
```

- [ ] **Step 4: Amend the spec's session-resolution wording**

In `docs/superpowers/specs/2026-07-02-task-progress-design.md`, replace the "Session resolution" paragraph:

```markdown
`agent list` entries carry `agent_session.value`. The daemon reads it
directly from the agent-list entry in the progress path (`PaneContext` is
only built for the idle probe flow, which has no use for it — YAGNI). Only
entries with `agent == "claude"` are considered for progress.
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest -q`
Expected: all PASS (existing hold tests unchanged: `FakeClient.explain` defaults to `"idle"` and holds never call it)

- [ ] **Step 6: Commit**

```bash
git add src/herdwatch/daemon.py tests/test_daemon.py docs/superpowers/specs/2026-07-02-task-progress-design.md
git commit -m "feat(daemon): show Claude task-list progress on working panes"
```

Then check `roborev show <sha>` and fix any findings.

---

### Task 6: Docs + live verification

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: the complete feature.
- Produces: user-facing docs; a verified running daemon.

- [ ] **Step 1: Document the feature in README**

In `README.md`, after the "How it works" section, add:

```markdown
## Task progress in the sidebar

While a Claude Code agent is actively working through a task list, herdwatch
shows how far along it is — `3/7 Fixing auth bug` — as the pane's status
label. It reads the session's task files (`~/.claude/tasks/`, matched via
herdr's `agent_session` id), so no per-agent setup is needed; other agents
are skipped. Because herdwatch asserts the label over herdr's own detection,
it re-checks the pane each tick with `herdr agent explain` (live screen
detection) and drops the label the moment the agent actually stops — a pane
waiting for your input is never masked. Disable with:

```toml
[progress]
enabled = false
```
```

Also extend the config example in the "Config" section with the `[progress]`
table (one line: `enabled = true` with a `# default` comment).

- [ ] **Step 2: Run the suite and commit**

Run: `.venv/bin/python -m pytest -q` — expected: all pass.

```bash
git add README.md
git commit -m "docs: describe task-progress sidebar labels"
```

Then check `roborev show <sha>` and fix any findings.

- [ ] **Step 3: Merge and restart the daemon**

Per repo convention (no squash):

```bash
git checkout main && git merge --no-ff <feature-branch>
launchctl kickstart -k gui/$(id -u)/dev.herdwatch.daemon
```

- [ ] **Step 4: Live verification**

- Start (or find) a Claude Code agent working through a task list in a herdr pane.
- Watch: `tail -f /tmp/herdwatch.err.log` — expect `progress <pane> -> N/M <task>` lines while it works.
- `herdwatch status` — the pane appears with the progress label.
- When the agent finishes or stops to ask a question, the label must disappear within ~2 poll intervals (`release <pane> (agent stopped)` in the log) and, if CI/review is pending, flip to a `⏳` hold.

---

## Self-review notes

- Spec coverage: reader + threshold rules (Task 1), config (Task 2), explain
  (Task 3), kind bookkeeping/adopt (Task 4), tick integration + same-tick
  handover + explain-failure release (Task 5), docs + live check (Task 6).
  The spec's `PaneContext.session_id` line is amended in Task 5 (YAGNI: the
  progress path reads the agent dict directly).
- Type consistency: `progress` callable is `(str) -> str | None` in Task 1
  (`progress_label`) and Task 5 (constructor param). `ManagedPane.kind`
  literals are `"hold"`/`"progress"` everywhere.
- No placeholders: every step carries the actual code/commands.
