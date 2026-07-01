# herdwatch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A standalone daemon that keeps a herdr pane shown as `working` + a `custom_status` label while background work (CI, roborev review, background jobs, manual markers) is still pending after the agent went idle.

**Architecture:** Python package `herdwatch`. A polling daemon reads `herdr agent list`, and for panes shown `idle`/`done` runs a set of probes; if any is pending it asserts `working` + `custom_status` via `herdr pane report-agent --source herdwatch`, and releases when all clear. Probes are pure, dependency-injected units. v1 triggers by polling (`poll_interval_s` ~4s) with throttled per-pane re-probing (`reprobe_interval_s` ~15s); event subscription to `pane.agent_status_changed` is a documented follow-up.

**Tech Stack:** Python ≥3.11 (stdlib only: `tomllib`, `subprocess`, `socket`, `dataclasses`, `argparse`), pytest. External CLIs invoked as subprocesses: `herdr`, `gh`, `roborev`. launchd for process supervision.

## Global Constraints

- Python ≥3.11, stdlib only (no third-party runtime deps). Test dep: `pytest`.
- `src/` layout; package import root `herdwatch`; console script `herdwatch = "herdwatch.cli:main"`.
- herdr report source string is exactly `"herdwatch"` (verbatim) everywhere.
- `custom_status` sent to herdr MUST be ≤32 characters (herdr truncates; we truncate first).
- Waiting is reported as semantic state `working` only — never `blocked` in v1.
- All external-tool probes degrade to "not pending" (return `None`) on any error, missing tool, or unparseable output — a broken tool must never pin a pane.
- Commit messages: conventional commits, English, no `Co-Authored-By`.
- Marker state dir: `~/.local/state/herdwatch/markers/`. Config: `~/.config/herdwatch/config.toml`.

---

## File Structure

```
herdwatch/
  pyproject.toml
  README.md
  src/herdwatch/
    __init__.py          # __version__
    models.py            # Pending, PaneContext
    aggregate.py         # aggregate(list[Pending]) -> str | None
    cache.py             # TTLCache
    gitctx.py            # GitInfo, enrich(cwd)
    config.py            # Config, load()
    markers.py           # Marker, MarkerStore
    probes/
      __init__.py
      base.py            # Probe protocol
      marker.py          # MarkerProbe
      ci.py              # CIProbe, default_run_gh
      roborev.py         # RoborevProbe, default_run_status/default_run_list
      bgjobs.py          # BgJobsProbe, default_list_descendants
    herdr.py             # HerdrClient
    daemon.py            # Daemon, ManagedPane, build_daemon, SOURCE
    cli.py               # argparse entrypoints
  tests/
    test_aggregate.py test_cache.py test_gitctx.py test_config.py
    test_markers.py test_probe_marker.py test_probe_ci.py test_probe_roborev.py
    test_probe_bgjobs.py test_herdr.py test_daemon.py test_cli.py
  deploy/dev.herdwatch.daemon.plist
```

---

### Task 1: Project scaffold

**Files:**
- Create: `pyproject.toml`, `src/herdwatch/__init__.py`, `tests/test_smoke.py`, `.gitignore`

**Interfaces:**
- Produces: package `herdwatch` importable; `herdwatch.__version__: str`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_smoke.py
import herdwatch

def test_version_present():
    assert isinstance(herdwatch.__version__, str)
    assert herdwatch.__version__
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/admin/projects/herdwatch && python -m pytest tests/test_smoke.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'herdwatch'`

- [ ] **Step 3: Write minimal implementation**

```toml
# pyproject.toml
[project]
name = "herdwatch"
version = "0.1.0"
description = "Show herdr agents as busy while their background work (CI, review, jobs) is still pending"
requires-python = ">=3.11"

[project.scripts]
herdwatch = "herdwatch.cli:main"

[project.optional-dependencies]
dev = ["pytest"]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]
```

```python
# src/herdwatch/__init__.py
__version__ = "0.1.0"
```

```
# .gitignore
__pycache__/
*.pyc
.venv/
*.egg-info/
.pytest_cache/
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_smoke.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml src/herdwatch/__init__.py tests/test_smoke.py .gitignore
git commit -m "chore: scaffold herdwatch package"
```

---

### Task 2: Core models

**Files:**
- Create: `src/herdwatch/models.py`, `tests/test_models.py`

**Interfaces:**
- Produces:
  - `Pending(label: str, priority: int, source: str)` — frozen dataclass.
  - `PaneContext(pane_id: str, agent: str, cwd: str, status: str, head_sha: str | None, branch: str | None, is_git_repo: bool, has_github_remote: bool)` — frozen dataclass.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_models.py
from herdwatch.models import Pending, PaneContext

def test_pending_fields():
    p = Pending(label="CI: ci", priority=20, source="ci")
    assert (p.label, p.priority, p.source) == ("CI: ci", 20, "ci")

def test_pane_context_fields():
    c = PaneContext(pane_id="w1:p1", agent="claude", cwd="/x", status="idle",
                    head_sha="abc", branch="main", is_git_repo=True, has_github_remote=True)
    assert c.pane_id == "w1:p1" and c.is_git_repo is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'herdwatch.models'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/herdwatch/models.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_models.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/herdwatch/models.py tests/test_models.py
git commit -m "feat: add Pending and PaneContext models"
```

---

### Task 3: Aggregate pending results into a custom_status

**Files:**
- Create: `src/herdwatch/aggregate.py`, `tests/test_aggregate.py`

**Interfaces:**
- Consumes: `Pending` from `herdwatch.models`.
- Produces: `aggregate(pendings: list[Pending]) -> str | None` — returns a `custom_status` string prefixed with `⏳ `, highest-priority label first, `+N` suffix when more than one, truncated to 32 chars; `None` if the list is empty.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_aggregate.py
from herdwatch.aggregate import aggregate
from herdwatch.models import Pending

def test_empty_is_none():
    assert aggregate([]) is None

def test_highest_priority_wins():
    out = aggregate([Pending("CI: ci", 20, "ci"), Pending("review", 30, "roborev")])
    assert out == "⏳ review"

def test_multiple_shows_plus_count():
    out = aggregate([Pending("review", 30, "roborev"), Pending("CI: ci", 20, "ci")])
    assert out == "⏳ review +1"

def test_truncated_to_32_chars():
    out = aggregate([Pending("x" * 50, 10, "marker")])
    assert len(out) <= 32
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_aggregate.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'herdwatch.aggregate'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/herdwatch/aggregate.py
from __future__ import annotations

from .models import Pending

HOURGLASS = "⏳"  # ⏳
MAX_LEN = 32


def aggregate(pendings: list[Pending]) -> str | None:
    if not pendings:
        return None
    ordered = sorted(pendings, key=lambda p: p.priority, reverse=True)
    top = ordered[0]
    extra = len(ordered) - 1
    label = f"{HOURGLASS} {top.label}"
    if extra > 0:
        label = f"{label} +{extra}"
    if len(label) > MAX_LEN:
        label = label[:MAX_LEN]
    return label
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_aggregate.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/herdwatch/aggregate.py tests/test_aggregate.py
git commit -m "feat: aggregate pending probes into a custom_status label"
```

---

### Task 4: TTL cache (shared by ci/roborev probes)

**Files:**
- Create: `src/herdwatch/cache.py`, `tests/test_cache.py`

**Interfaces:**
- Produces: `TTLCache(ttl_s: float, clock: Callable[[], float] = time.time)` with `get_or(key: Hashable, fn: Callable[[], Any]) -> Any` — calls `fn` and caches its result per `key` until `ttl_s` elapses; within TTL returns the cached value without calling `fn`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cache.py
from herdwatch.cache import TTLCache

def test_caches_within_ttl():
    now = [1000.0]
    calls = []
    c = TTLCache(ttl_s=10, clock=lambda: now[0])
    def fn():
        calls.append(1)
        return "v"
    assert c.get_or("k", fn) == "v"
    assert c.get_or("k", fn) == "v"
    assert len(calls) == 1  # second call served from cache

def test_recomputes_after_ttl():
    now = [1000.0]
    calls = []
    c = TTLCache(ttl_s=10, clock=lambda: now[0])
    c.get_or("k", lambda: calls.append(1))
    now[0] = 1011.0
    c.get_or("k", lambda: calls.append(1))
    assert len(calls) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cache.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'herdwatch.cache'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/herdwatch/cache.py
from __future__ import annotations

import time
from typing import Any, Callable, Hashable


class TTLCache:
    def __init__(self, ttl_s: float, clock: Callable[[], float] = time.time) -> None:
        self._ttl = ttl_s
        self._clock = clock
        self._store: dict[Hashable, tuple[float, Any]] = {}

    def get_or(self, key: Hashable, fn: Callable[[], Any]) -> Any:
        now = self._clock()
        hit = self._store.get(key)
        if hit is not None and (now - hit[0]) < self._ttl:
            return hit[1]
        value = fn()
        self._store[key] = (now, value)
        return value
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_cache.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/herdwatch/cache.py tests/test_cache.py
git commit -m "feat: add TTLCache for probe result reuse"
```

---

### Task 5: Git context enrichment

**Files:**
- Create: `src/herdwatch/gitctx.py`, `tests/test_gitctx.py`

**Interfaces:**
- Produces:
  - `GitInfo(is_git_repo: bool, head_sha: str | None, branch: str | None, has_github_remote: bool)` — frozen dataclass.
  - `enrich(cwd: str, run: Callable[[list[str], str], tuple[int, str]] = _run_git) -> GitInfo` — `run(args, cwd)` returns `(returncode, stdout)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_gitctx.py
import subprocess
from pathlib import Path
from herdwatch.gitctx import enrich

def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def test_non_repo(tmp_path):
    info = enrich(str(tmp_path))
    assert info.is_git_repo is False
    assert info.head_sha is None

def test_repo_with_github_remote(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    _git(tmp_path, "remote", "add", "origin", "https://github.com/x/y.git")
    (tmp_path / "f").write_text("x")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")
    info = enrich(str(tmp_path))
    assert info.is_git_repo is True
    assert info.has_github_remote is True
    assert info.head_sha and len(info.head_sha) == 40
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_gitctx.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'herdwatch.gitctx'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/herdwatch/gitctx.py
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class GitInfo:
    is_git_repo: bool
    head_sha: str | None
    branch: str | None
    has_github_remote: bool


def _run_git(args: list[str], cwd: str) -> tuple[int, str]:
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                           text=True, timeout=5)
        return r.returncode, r.stdout.strip()
    except Exception:
        return 1, ""


def enrich(cwd: str, run: Callable[[list[str], str], tuple[int, str]] = _run_git) -> GitInfo:
    rc, _ = run(["rev-parse", "--is-inside-work-tree"], cwd)
    if rc != 0:
        return GitInfo(False, None, None, False)
    _, head = run(["rev-parse", "HEAD"], cwd)
    _, branch = run(["branch", "--show-current"], cwd)
    _, remote = run(["remote", "get-url", "origin"], cwd)
    return GitInfo(
        is_git_repo=True,
        head_sha=head or None,
        branch=branch or None,
        has_github_remote="github.com" in remote,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_gitctx.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/herdwatch/gitctx.py tests/test_gitctx.py
git commit -m "feat: add git context enrichment"
```

---

### Task 6: Config loading

**Files:**
- Create: `src/herdwatch/config.py`, `tests/test_config.py`

**Interfaces:**
- Produces:
  - `Config(poll_interval_s: float, reprobe_interval_s: float, socket_path: str, probes: dict[str, bool], ci_cache_ttl_s: float, bgjobs_min_age_s: float, allow: list[str], deny: list[str])` — dataclass.
  - `load(path: str | None = None) -> Config` — reads TOML from `path` or `~/.config/herdwatch/config.toml`; missing file returns all defaults; unknown keys ignored.
  - Defaults: `poll_interval_s=4.0`, `reprobe_interval_s=15.0`, `socket_path=""`, `probes={"roborev":True,"ci":True,"bgjobs":True,"marker":True}`, `ci_cache_ttl_s=10.0`, `bgjobs_min_age_s=5.0`, `allow=[]`, `deny=[]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config.py
from herdwatch.config import load

def test_defaults_when_missing(tmp_path):
    cfg = load(str(tmp_path / "nope.toml"))
    assert cfg.poll_interval_s == 4.0
    assert cfg.probes == {"roborev": True, "ci": True, "bgjobs": True, "marker": True}

def test_override(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[daemon]\npoll_interval_s = 2\n[probes]\nbgjobs = false\n')
    cfg = load(str(p))
    assert cfg.poll_interval_s == 2
    assert cfg.probes["bgjobs"] is False
    assert cfg.probes["ci"] is True  # untouched default
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'herdwatch.config'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/herdwatch/config.py
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PATH = os.path.expanduser("~/.config/herdwatch/config.toml")
_DEFAULT_PROBES = {"roborev": True, "ci": True, "bgjobs": True, "marker": True}


@dataclass
class Config:
    poll_interval_s: float = 4.0
    reprobe_interval_s: float = 15.0
    socket_path: str = ""
    probes: dict = field(default_factory=lambda: dict(_DEFAULT_PROBES))
    ci_cache_ttl_s: float = 10.0
    bgjobs_min_age_s: float = 5.0
    allow: list = field(default_factory=list)
    deny: list = field(default_factory=list)


def load(path: str | None = None) -> Config:
    p = Path(path or DEFAULT_PATH)
    cfg = Config()
    if not p.exists():
        return cfg
    data = tomllib.loads(p.read_text())
    daemon = data.get("daemon", {})
    cfg.poll_interval_s = float(daemon.get("poll_interval_s", cfg.poll_interval_s))
    cfg.reprobe_interval_s = float(daemon.get("reprobe_interval_s", cfg.reprobe_interval_s))
    cfg.socket_path = str(daemon.get("socket_path", cfg.socket_path))
    for name in _DEFAULT_PROBES:
        cfg.probes[name] = bool(data.get("probes", {}).get(name, cfg.probes[name]))
    cfg.ci_cache_ttl_s = float(data.get("probes", {}).get("ci", {}).get("cache_ttl_s", cfg.ci_cache_ttl_s)) \
        if isinstance(data.get("probes", {}).get("ci"), dict) else cfg.ci_cache_ttl_s
    bg = data.get("probes", {}).get("bgjobs")
    if isinstance(bg, dict):
        cfg.bgjobs_min_age_s = float(bg.get("min_age_s", cfg.bgjobs_min_age_s))
    panes = data.get("panes", {})
    cfg.allow = list(panes.get("allow", []))
    cfg.deny = list(panes.get("deny", []))
    return cfg
```

Note: `[probes]` accepts a bool `ci = true/false` to enable/disable. Per-probe sub-tables (`[probes.ci] cache_ttl_s`, `[probes.bgjobs] min_age_s`) are read only when present as tables; the enable flag and the sub-table are independent keys in the spec's example and both are honored above.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_config.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/herdwatch/config.py tests/test_config.py
git commit -m "feat: add config loading with defaults"
```

---

### Task 7: Marker store

**Files:**
- Create: `src/herdwatch/markers.py`, `tests/test_markers.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `Marker(id: str, pane_id: str, label: str, until: str | None, pid: int | None, expires_at: float | None)` — dataclass.
  - `MarkerStore(dir: Path, now=time.time, run_cmd=..., pid_alive=...)` with:
    - `add(pane_id: str, label: str, until: str | None = None, pid: int | None = None, ttl_s: float | None = None) -> Marker`
    - `all() -> list[Marker]`
    - `remove(marker_id: str) -> None`
    - `is_pending(m: Marker) -> bool`
    - `active_for_pane(pane_id: str) -> list[Marker]` — returns pending markers for the pane; prunes non-pending ones from disk.
  - `run_cmd(cmd: str) -> int` returns a shell exit code; `pid_alive(pid: int) -> bool`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_markers.py
from herdwatch.markers import MarkerStore

def _store(tmp_path, **kw):
    return MarkerStore(tmp_path, now=kw.get("now", lambda: 1000.0),
                       run_cmd=kw.get("run_cmd", lambda c: 1),
                       pid_alive=kw.get("pid_alive", lambda p: True))

def test_plain_marker_is_pending_until_removed(tmp_path):
    s = _store(tmp_path)
    m = s.add("w1:p1", "deploy")
    assert s.is_pending(m) is True
    s.remove(m.id)
    assert s.all() == []

def test_ttl_expiry(tmp_path):
    now = [1000.0]
    s = _store(tmp_path, now=lambda: now[0])
    m = s.add("w1:p1", "x", ttl_s=10)
    assert s.is_pending(m) is True
    now[0] = 1011.0
    assert s.is_pending(m) is False

def test_until_pending_while_cmd_fails(tmp_path):
    rc = [1]
    s = _store(tmp_path, run_cmd=lambda c: rc[0])
    m = s.add("w1:p1", "x", until="check.sh")
    assert s.is_pending(m) is True   # cmd non-zero -> still waiting
    rc[0] = 0
    assert s.is_pending(m) is False  # cmd success -> done

def test_active_for_pane_prunes(tmp_path):
    now = [1000.0]
    s = _store(tmp_path, now=lambda: now[0])
    s.add("w1:p1", "x", ttl_s=5)
    now[0] = 1010.0
    assert s.active_for_pane("w1:p1") == []
    assert s.all() == []  # pruned from disk
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_markers.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'herdwatch.markers'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/herdwatch/markers.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_markers.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/herdwatch/markers.py tests/test_markers.py
git commit -m "feat: add marker store with ttl/pid/until conditions"
```

---

### Task 8: Probe protocol + marker probe

**Files:**
- Create: `src/herdwatch/probes/__init__.py`, `src/herdwatch/probes/base.py`, `src/herdwatch/probes/marker.py`, `tests/test_probe_marker.py`

**Interfaces:**
- Consumes: `Pending`, `PaneContext` (models); `MarkerStore` (markers).
- Produces:
  - `Probe` — Protocol with `name: str` and `check(self, ctx: PaneContext) -> Pending | None`.
  - `MarkerProbe(store: MarkerStore)` with `name = "marker"`, priority 40.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_probe_marker.py
from herdwatch.models import PaneContext
from herdwatch.markers import MarkerStore
from herdwatch.probes.marker import MarkerProbe

def _ctx(pane="w1:p1"):
    return PaneContext(pane, "claude", "/x", "idle", "sha", "main", True, True)

def test_no_marker_none(tmp_path):
    assert MarkerProbe(MarkerStore(tmp_path, run_cmd=lambda c: 1)).check(_ctx()) is None

def test_marker_pending(tmp_path):
    store = MarkerStore(tmp_path, run_cmd=lambda c: 1)
    store.add("w1:p1", "deploy")
    p = MarkerProbe(store).check(_ctx())
    assert p is not None and p.label == "deploy" and p.source == "marker" and p.priority == 40
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_probe_marker.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'herdwatch.probes'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/herdwatch/probes/__init__.py
```

```python
# src/herdwatch/probes/base.py
from __future__ import annotations

from typing import Protocol

from ..models import PaneContext, Pending


class Probe(Protocol):
    name: str

    def check(self, ctx: PaneContext) -> Pending | None: ...
```

```python
# src/herdwatch/probes/marker.py
from __future__ import annotations

from ..markers import MarkerStore
from ..models import PaneContext, Pending

PRIORITY = 40


class MarkerProbe:
    name = "marker"

    def __init__(self, store: MarkerStore) -> None:
        self._store = store

    def check(self, ctx: PaneContext) -> Pending | None:
        active = self._store.active_for_pane(ctx.pane_id)
        if not active:
            return None
        return Pending(label=active[0].label, priority=PRIORITY, source=self.name)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_probe_marker.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/herdwatch/probes/ tests/test_probe_marker.py
git commit -m "feat: add probe protocol and marker probe"
```

---

### Task 9: CI probe (GitHub Actions via gh)

**Files:**
- Create: `src/herdwatch/probes/ci.py`, `tests/test_probe_ci.py`

**Interfaces:**
- Consumes: `Pending`, `PaneContext`; `TTLCache`.
- Produces:
  - `default_run_gh(cwd: str, branch: str | None) -> list[dict]` — runs `gh run list` and returns parsed runs (`[]` on any error).
  - `CIProbe(cache: TTLCache, run_gh=default_run_gh)` with `name = "ci"`, priority 20. Pending when a run whose `headSha == ctx.head_sha` has `status in {"queued","in_progress"}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_probe_ci.py
from herdwatch.cache import TTLCache
from herdwatch.models import PaneContext
from herdwatch.probes.ci import CIProbe

def _ctx(**kw):
    d = dict(pane_id="w1:p1", agent="claude", cwd="/x", status="idle",
             head_sha="abc", branch="main", is_git_repo=True, has_github_remote=True)
    d.update(kw)
    return PaneContext(**d)

def _cache():
    return TTLCache(ttl_s=10, clock=lambda: 0.0)

def test_skip_when_no_github_remote():
    probe = CIProbe(_cache(), run_gh=lambda cwd, br: [{"headSha": "abc", "status": "in_progress", "workflowName": "ci"}])
    assert probe.check(_ctx(has_github_remote=False)) is None

def test_pending_when_run_in_progress_for_head():
    probe = CIProbe(_cache(), run_gh=lambda cwd, br: [{"headSha": "abc", "status": "in_progress", "workflowName": "lint"}])
    p = probe.check(_ctx())
    assert p is not None and p.label == "CI: lint" and p.priority == 20 and p.source == "ci"

def test_none_when_only_completed():
    probe = CIProbe(_cache(), run_gh=lambda cwd, br: [{"headSha": "abc", "status": "completed", "workflowName": "ci"}])
    assert probe.check(_ctx()) is None

def test_none_when_run_is_for_other_sha():
    probe = CIProbe(_cache(), run_gh=lambda cwd, br: [{"headSha": "zzz", "status": "queued", "workflowName": "ci"}])
    assert probe.check(_ctx()) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_probe_ci.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'herdwatch.probes.ci'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/herdwatch/probes/ci.py
from __future__ import annotations

import json
import subprocess
from typing import Callable

from ..cache import TTLCache
from ..models import PaneContext, Pending

PRIORITY = 20
_ACTIVE = {"queued", "in_progress"}


def default_run_gh(cwd: str, branch: str | None) -> list[dict]:
    args = ["gh", "run", "list", "--limit", "20",
            "--json", "status,headSha,workflowName"]
    if branch:
        args += ["--branch", branch]
    try:
        r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=15)
        if r.returncode != 0 or not r.stdout.strip():
            return []
        return json.loads(r.stdout)
    except Exception:
        return []


class CIProbe:
    name = "ci"

    def __init__(self, cache: TTLCache,
                 run_gh: Callable[[str, str | None], list[dict]] = default_run_gh) -> None:
        self._cache = cache
        self._run_gh = run_gh

    def check(self, ctx: PaneContext) -> Pending | None:
        if not (ctx.is_git_repo and ctx.has_github_remote and ctx.head_sha):
            return None
        runs = self._cache.get_or(("ci", ctx.cwd, ctx.head_sha),
                                  lambda: self._run_gh(ctx.cwd, ctx.branch))
        for run in runs:
            if run.get("headSha") == ctx.head_sha and run.get("status") in _ACTIVE:
                wf = run.get("workflowName") or "ci"
                return Pending(label=f"CI: {wf}", priority=PRIORITY, source=self.name)
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_probe_ci.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/herdwatch/probes/ci.py tests/test_probe_ci.py
git commit -m "feat: add CI probe for in-flight GitHub Actions runs"
```

---

### Task 10: roborev probe

**Files:**
- Create: `src/herdwatch/probes/roborev.py`, `tests/test_probe_roborev.py`

**Interfaces:**
- Consumes: `Pending`, `PaneContext`; `TTLCache`.
- Produces:
  - `default_run_status() -> dict` — parsed `roborev status --json` (`{}` on error).
  - `default_run_list(cwd: str) -> list[dict]` — parsed `roborev list --repo <cwd> --limit 20 --json` (`[]` on error).
  - `RoborevProbe(cache: TTLCache, run_status=default_run_status, run_list=default_run_list)` with `name = "roborev"`, priority 30. Cheap gate: if `daemon.queued_jobs + daemon.running_jobs == 0`, return None. Else Pending when a job with `git_ref == ctx.head_sha` has `status in {"queued","running"}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_probe_roborev.py
from herdwatch.cache import TTLCache
from herdwatch.models import PaneContext
from herdwatch.probes.roborev import RoborevProbe

def _ctx(**kw):
    d = dict(pane_id="w1:p1", agent="claude", cwd="/x", status="idle",
             head_sha="abc", branch="main", is_git_repo=True, has_github_remote=True)
    d.update(kw)
    return PaneContext(**d)

def _cache():
    return TTLCache(ttl_s=10, clock=lambda: 0.0)

_BUSY = {"daemon": {"queued_jobs": 1, "running_jobs": 0}}
_IDLE = {"daemon": {"queued_jobs": 0, "running_jobs": 0}}

def test_gate_skips_when_queue_empty():
    probe = RoborevProbe(_cache(), run_status=lambda: _IDLE,
                         run_list=lambda cwd: [{"git_ref": "abc", "status": "running"}])
    assert probe.check(_ctx()) is None

def test_pending_when_job_running_for_head():
    probe = RoborevProbe(_cache(), run_status=lambda: _BUSY,
                         run_list=lambda cwd: [{"git_ref": "abc", "status": "queued"}])
    p = probe.check(_ctx())
    assert p is not None and p.label == "review" and p.priority == 30 and p.source == "roborev"

def test_none_when_job_done():
    probe = RoborevProbe(_cache(), run_status=lambda: _BUSY,
                         run_list=lambda cwd: [{"git_ref": "abc", "status": "done"}])
    assert probe.check(_ctx()) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_probe_roborev.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'herdwatch.probes.roborev'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/herdwatch/probes/roborev.py
from __future__ import annotations

import json
import subprocess
from typing import Callable

from ..cache import TTLCache
from ..models import PaneContext, Pending

PRIORITY = 30
_ACTIVE = {"queued", "running"}


def default_run_status() -> dict:
    try:
        r = subprocess.run(["roborev", "status", "--json"], capture_output=True,
                           text=True, timeout=5)
        if r.returncode != 0 or not r.stdout.strip():
            return {}
        return json.loads(r.stdout)
    except Exception:
        return {}


def default_run_list(cwd: str) -> list[dict]:
    try:
        r = subprocess.run(["roborev", "list", "--repo", cwd, "--limit", "20", "--json"],
                           cwd=cwd, capture_output=True, text=True, timeout=10)
        if r.returncode != 0 or not r.stdout.strip():
            return []
        return json.loads(r.stdout)
    except Exception:
        return []


class RoborevProbe:
    name = "roborev"

    def __init__(self, cache: TTLCache,
                 run_status: Callable[[], dict] = default_run_status,
                 run_list: Callable[[str], list[dict]] = default_run_list) -> None:
        self._cache = cache
        self._run_status = run_status
        self._run_list = run_list

    def check(self, ctx: PaneContext) -> Pending | None:
        if not (ctx.is_git_repo and ctx.head_sha):
            return None
        status = self._cache.get_or(("roborev-status",), self._run_status)
        daemon = status.get("daemon", {})
        if (daemon.get("queued_jobs", 0) + daemon.get("running_jobs", 0)) == 0:
            return None
        jobs = self._cache.get_or(("roborev-list", ctx.cwd),
                                  lambda: self._run_list(ctx.cwd))
        for job in jobs:
            if job.get("git_ref") == ctx.head_sha and job.get("status") in _ACTIVE:
                return Pending(label="review", priority=PRIORITY, source=self.name)
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_probe_roborev.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/herdwatch/probes/roborev.py tests/test_probe_roborev.py
git commit -m "feat: add roborev probe for in-flight reviews of HEAD"
```

---

### Task 11: Background-jobs probe

**Files:**
- Create: `src/herdwatch/probes/bgjobs.py`, `tests/test_probe_bgjobs.py`

**Interfaces:**
- Consumes: `Pending`, `PaneContext`.
- Produces:
  - `default_list_descendants(root_pid: int) -> list[dict]` — each `{"pid": int, "pgid": int, "etime_s": float, "comm": str}` for descendants of `root_pid` (`[]` on error).
  - `BgJobsProbe(process_info: Callable[[str], dict], min_age_s: float = 5.0, list_descendants=default_list_descendants, agent_names=frozenset({"claude","codex","node","caffeinate"}))` with `name = "bgjobs"`, priority 10. `process_info(pane_id)` returns herdr's `process_info` dict (`shell_pid`, `foreground_process_group_id`). Pending when a descendant of `shell_pid` is not in the foreground process group, not a known agent/shell name, and older than `min_age_s`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_probe_bgjobs.py
from herdwatch.models import PaneContext
from herdwatch.probes.bgjobs import BgJobsProbe

def _ctx():
    return PaneContext("w1:p1", "claude", "/x", "idle", "sha", "main", True, True)

_INFO = {"shell_pid": 100, "foreground_process_group_id": 100}

def test_none_when_no_shell_pid():
    probe = BgJobsProbe(process_info=lambda pid: {}, list_descendants=lambda root: [])
    assert probe.check(_ctx()) is None

def test_ignores_foreground_group():
    desc = [{"pid": 101, "pgid": 100, "etime_s": 60, "comm": "npm"}]
    probe = BgJobsProbe(process_info=lambda pid: _INFO, list_descendants=lambda root: desc)
    assert probe.check(_ctx()) is None  # pgid == foreground group

def test_pending_for_backgrounded_job():
    desc = [{"pid": 202, "pgid": 202, "etime_s": 60, "comm": "pytest"}]
    probe = BgJobsProbe(process_info=lambda pid: _INFO, list_descendants=lambda root: desc)
    p = probe.check(_ctx())
    assert p is not None and p.label == "pytest" and p.priority == 10 and p.source == "bgjobs"

def test_ignores_young_jobs():
    desc = [{"pid": 202, "pgid": 202, "etime_s": 2, "comm": "pytest"}]
    probe = BgJobsProbe(process_info=lambda pid: _INFO, list_descendants=lambda root: desc, min_age_s=5)
    assert probe.check(_ctx()) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_probe_bgjobs.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'herdwatch.probes.bgjobs'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/herdwatch/probes/bgjobs.py
from __future__ import annotations

import subprocess
from typing import Callable

from ..models import PaneContext, Pending

PRIORITY = 10
_DEFAULT_IGNORE = frozenset({"claude", "codex", "node", "caffeinate", "sh", "zsh", "bash"})


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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_probe_bgjobs.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/herdwatch/probes/bgjobs.py tests/test_probe_bgjobs.py
git commit -m "feat: add best-effort background-jobs probe"
```

---

### Task 12: herdr client

**Files:**
- Create: `src/herdwatch/herdr.py`, `tests/test_herdr.py`

**Interfaces:**
- Produces: `HerdrClient(herdr_bin: str = "herdr", run=_run)` where `run(args: list[str]) -> tuple[int, str]`:
  - `agent_list() -> list[dict]` — parses `herdr agent list` → `result.agents`.
  - `pane_process_info(pane_id: str) -> dict` — parses `herdr pane process-info --pane <id>` → `result.process_info` (`{}` on error).
  - `report_agent(pane_id, source, agent, state, custom_status=None) -> None`.
  - `release_agent(pane_id, source, agent) -> None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_herdr.py
import json
from herdwatch.herdr import HerdrClient

def test_agent_list_parses_agents():
    payload = json.dumps({"result": {"agents": [{"pane_id": "w1:p1", "agent_status": "idle"}]}})
    client = HerdrClient(run=lambda args: (0, payload))
    agents = client.agent_list()
    assert agents == [{"pane_id": "w1:p1", "agent_status": "idle"}]

def test_report_agent_builds_command():
    calls = []
    client = HerdrClient(run=lambda args: calls.append(args) or (0, ""))
    client.report_agent("w1:p1", "herdwatch", "claude", "working", "⏳ CI")
    assert calls[0] == ["herdr", "pane", "report-agent", "w1:p1", "--source", "herdwatch",
                        "--agent", "claude", "--state", "working", "--custom-status", "⏳ CI"]

def test_release_agent_builds_command():
    calls = []
    client = HerdrClient(run=lambda args: calls.append(args) or (0, ""))
    client.release_agent("w1:p1", "herdwatch", "claude")
    assert calls[0] == ["herdr", "pane", "release-agent", "w1:p1", "--source", "herdwatch", "--agent", "claude"]

def test_agent_list_empty_on_error():
    assert HerdrClient(run=lambda args: (1, "")).agent_list() == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_herdr.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'herdwatch.herdr'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/herdwatch/herdr.py
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
        return data.get("result", {}).get("agents", [])

    def pane_process_info(self, pane_id: str) -> dict:
        data = self._json([self._bin, "pane", "process-info", "--pane", pane_id])
        return data.get("result", {}).get("process_info", {})

    def report_agent(self, pane_id: str, source: str, agent: str, state: str,
                     custom_status: str | None = None) -> None:
        args = [self._bin, "pane", "report-agent", pane_id, "--source", source,
                "--agent", agent, "--state", state]
        if custom_status:
            args += ["--custom-status", custom_status]
        self._run(args)

    def release_agent(self, pane_id: str, source: str, agent: str) -> None:
        self._run([self._bin, "pane", "release-agent", pane_id,
                   "--source", source, "--agent", agent])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_herdr.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/herdwatch/herdr.py tests/test_herdr.py
git commit -m "feat: add herdr CLI client"
```

---

### Task 13: Daemon state machine

**Files:**
- Create: `src/herdwatch/daemon.py`, `tests/test_daemon.py`

**Interfaces:**
- Consumes: `HerdrClient` (duck-typed: `agent_list`, `report_agent`, `release_agent`); probes (`check(ctx)`); `aggregate`; `gitctx.enrich`.
- Produces:
  - `SOURCE = "herdwatch"`.
  - `ManagedPane(custom_status: str, last_probe: float, agent: str)` — dataclass.
  - `Daemon(client, probes, reprobe_interval_s=15.0, clock=time.time, enrich=gitctx.enrich)` with `tick() -> None` (one polling iteration) and `managed: dict[str, ManagedPane]`.
  - `build_daemon(config, client=None) -> Daemon` factory wiring real probes.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_daemon.py
from herdwatch.daemon import Daemon, SOURCE
from herdwatch.gitctx import GitInfo
from herdwatch.models import Pending

class FakeClient:
    def __init__(self, agents):
        self.agents = agents
        self.reports = []
        self.releases = []
    def agent_list(self):
        return self.agents
    def report_agent(self, pane_id, source, agent, state, custom_status=None):
        self.reports.append((pane_id, state, custom_status))
    def release_agent(self, pane_id, source, agent):
        self.releases.append(pane_id)

class StaticProbe:
    name = "static"
    def __init__(self, result):
        self.result = result
    def check(self, ctx):
        return self.result

_ENRICH = lambda cwd: GitInfo(True, "sha", "main", True)

def _agent(pane="w1:p1", status="idle"):
    return {"pane_id": pane, "agent_status": status, "agent": "claude", "cwd": "/x"}

def test_asserts_working_when_pending():
    client = FakeClient([_agent(status="idle")])
    d = Daemon(client, [StaticProbe(Pending("review", 30, "roborev"))],
               clock=lambda: 0.0, enrich=_ENRICH)
    d.tick()
    assert client.reports == [("w1:p1", "working", "⏳ review")]
    assert "w1:p1" in d.managed

def test_ignores_working_pane_not_managed():
    client = FakeClient([_agent(status="working")])
    d = Daemon(client, [StaticProbe(Pending("x", 10, "marker"))], clock=lambda: 0.0, enrich=_ENRICH)
    d.tick()
    assert client.reports == []

def test_releases_when_cleared():
    client = FakeClient([_agent(status="idle")])
    probe = StaticProbe(Pending("review", 30, "roborev"))
    d = Daemon(client, [probe], clock=lambda: 0.0, enrich=_ENRICH)
    d.tick()
    probe.result = None
    d.tick()
    assert client.releases == ["w1:p1"]
    assert "w1:p1" not in d.managed

def test_reasserts_only_on_label_change():
    client = FakeClient([_agent(status="idle")])
    probe = StaticProbe(Pending("review", 30, "roborev"))
    d = Daemon(client, [probe], reprobe_interval_s=0, clock=lambda: 0.0, enrich=_ENRICH)
    d.tick()
    d.tick()
    assert len(client.reports) == 1  # unchanged label -> no duplicate report

def test_drops_vanished_pane():
    client = FakeClient([_agent(status="idle")])
    d = Daemon(client, [StaticProbe(Pending("x", 10, "marker"))], clock=lambda: 0.0, enrich=_ENRICH)
    d.tick()
    client.agents = []
    d.tick()
    assert d.managed == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_daemon.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'herdwatch.daemon'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/herdwatch/daemon.py
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Callable

from . import gitctx
from .aggregate import aggregate
from .cache import TTLCache
from .config import Config
from .herdr import HerdrClient
from .markers import MarkerStore
from .models import PaneContext
from .probes.bgjobs import BgJobsProbe
from .probes.ci import CIProbe
from .probes.marker import MarkerProbe
from .probes.roborev import RoborevProbe

SOURCE = "herdwatch"
MARKER_DIR = os.path.expanduser("~/.local/state/herdwatch/markers")


@dataclass
class ManagedPane:
    custom_status: str
    last_probe: float
    agent: str


class Daemon:
    def __init__(self, client, probes, reprobe_interval_s: float = 15.0,
                 clock: Callable[[], float] = time.time,
                 enrich: Callable[[str], gitctx.GitInfo] = gitctx.enrich) -> None:
        self._client = client
        self._probes = probes
        self._reprobe = reprobe_interval_s
        self._clock = clock
        self._enrich = enrich
        self.managed: dict[str, ManagedPane] = {}

    def _context(self, agent: dict) -> PaneContext:
        cwd = agent.get("cwd") or agent.get("foreground_cwd") or ""
        gi = self._enrich(cwd)
        return PaneContext(
            pane_id=agent["pane_id"],
            agent=agent.get("agent") or "agent",
            cwd=cwd,
            status=agent.get("agent_status") or "unknown",
            head_sha=gi.head_sha,
            branch=gi.branch,
            is_git_repo=gi.is_git_repo,
            has_github_remote=gi.has_github_remote,
        )

    def tick(self) -> None:
        seen = set()
        for agent in self._client.agent_list():
            pane_id = agent.get("pane_id")
            if not pane_id:
                continue
            seen.add(pane_id)
            status = agent.get("agent_status") or "unknown"
            managed = pane_id in self.managed
            if not managed and status not in ("idle", "done"):
                continue
            now = self._clock()
            if managed and (now - self.managed[pane_id].last_probe) < self._reprobe:
                continue
            ctx = self._context(agent)
            pendings = [r for r in (p.check(ctx) for p in self._probes) if r]
            label = aggregate(pendings)
            if label:
                agent_name = ctx.agent
                if not managed or self.managed[pane_id].custom_status != label:
                    self._client.report_agent(pane_id, SOURCE, agent_name, "working", label)
                self.managed[pane_id] = ManagedPane(label, now, agent_name)
            elif managed:
                self._client.release_agent(pane_id, SOURCE, self.managed[pane_id].agent)
                del self.managed[pane_id]
        for pane_id in list(self.managed):
            if pane_id not in seen:
                del self.managed[pane_id]

    def run(self, poll_interval_s: float, sleep: Callable[[float], None] = time.sleep) -> None:
        while True:
            self.tick()
            sleep(poll_interval_s)


def build_daemon(config: Config, client=None) -> Daemon:
    client = client or HerdrClient()
    cache = TTLCache(config.ci_cache_ttl_s)
    probes = []
    if config.probes.get("marker"):
        probes.append(MarkerProbe(MarkerStore(MARKER_DIR)))
    if config.probes.get("roborev"):
        probes.append(RoborevProbe(cache))
    if config.probes.get("ci"):
        probes.append(CIProbe(cache))
    if config.probes.get("bgjobs"):
        probes.append(BgJobsProbe(process_info=client.pane_process_info,
                                  min_age_s=config.bgjobs_min_age_s))
    return Daemon(client, probes, reprobe_interval_s=config.reprobe_interval_s)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_daemon.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/herdwatch/daemon.py tests/test_daemon.py
git commit -m "feat: add daemon state machine and wiring factory"
```

---

### Task 14: CLI

**Files:**
- Create: `src/herdwatch/cli.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `config.load`, `daemon.build_daemon`, `MarkerStore`, `daemon.MARKER_DIR`.
- Produces: `main(argv: list[str] | None = None) -> int` — subcommands `daemon`, `status`, `add`, `list`, `rm`. `add` binds to `$HERDR_PANE_ID` when `--pane` is omitted.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli.py
import json
from herdwatch import cli
from herdwatch.markers import MarkerStore

def test_add_and_list_marker(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "MARKER_DIR", str(tmp_path))
    monkeypatch.setenv("HERDR_PANE_ID", "w1:p1")
    assert cli.main(["add", "deploy"]) == 0
    assert cli.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "deploy" in out and "w1:p1" in out

def test_add_requires_pane(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "MARKER_DIR", str(tmp_path))
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    assert cli.main(["add", "x"]) == 2  # no pane available

def test_rm_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "MARKER_DIR", str(tmp_path))
    store = MarkerStore(tmp_path)
    m = store.add("w1:p1", "deploy")
    assert cli.main(["rm", m.id]) == 0
    assert store.all() == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cli.py -v`
Expected: FAIL — `AttributeError` / `ModuleNotFoundError: No module named 'herdwatch.cli'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/herdwatch/cli.py
from __future__ import annotations

import argparse
import os
import sys

from .config import load as load_config
from .daemon import MARKER_DIR, build_daemon
from .markers import MarkerStore


def _store() -> MarkerStore:
    return MarkerStore(MARKER_DIR)


def _cmd_daemon(args) -> int:
    cfg = load_config(args.config)
    daemon = build_daemon(cfg)
    daemon.run(cfg.poll_interval_s)
    return 0


def _cmd_add(args) -> int:
    pane = args.pane or os.environ.get("HERDR_PANE_ID")
    if not pane:
        print("no pane: pass --pane or run inside a herdr pane", file=sys.stderr)
        return 2
    m = _store().add(pane, args.label, until=args.until, pid=args.pid, ttl_s=args.ttl)
    print(m.id)
    return 0


def _cmd_list(args) -> int:
    for m in _store().all():
        print(f"{m.id}  {m.pane_id}  {m.label}")
    return 0


def _cmd_rm(args) -> int:
    store = _store()
    if args.all:
        for m in store.all():
            store.remove(m.id)
    else:
        store.remove(args.marker_id)
    return 0


def _cmd_status(args) -> int:
    for m in _store().all():
        print(f"marker {m.id} {m.pane_id} {m.label}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="herdwatch")
    parser.add_argument("--config", default=None)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("daemon").set_defaults(func=_cmd_daemon)
    sub.add_parser("status").set_defaults(func=_cmd_status)
    sub.add_parser("list").set_defaults(func=_cmd_list)

    p_add = sub.add_parser("add")
    p_add.add_argument("label")
    p_add.add_argument("--pane", default=None)
    p_add.add_argument("--until", default=None)
    p_add.add_argument("--pid", type=int, default=None)
    p_add.add_argument("--ttl", type=float, default=None)
    p_add.set_defaults(func=_cmd_add)

    p_rm = sub.add_parser("rm")
    p_rm.add_argument("marker_id", nargs="?", default=None)
    p_rm.add_argument("--all", action="store_true")
    p_rm.set_defaults(func=_cmd_rm)

    args = parser.parse_args(argv)
    return args.func(args)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_cli.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/herdwatch/cli.py tests/test_cli.py
git commit -m "feat: add herdwatch CLI"
```

---

### Task 15: Packaging — launchd plist + README + full suite

**Files:**
- Create: `deploy/dev.herdwatch.daemon.plist`, `README.md`

**Interfaces:**
- Produces: launchd unit that runs `herdwatch daemon`; README with install + usage.

- [ ] **Step 1: Write the launchd plist**

```xml
<!-- deploy/dev.herdwatch.daemon.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>dev.herdwatch.daemon</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/admin/projects/herdwatch/.venv/bin/herdwatch</string>
    <string>daemon</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/opt/homebrew/bin:/Users/admin/.local/bin:/usr/bin:/bin</string>
  </dict>
  <key>KeepAlive</key><true/>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>/tmp/herdwatch.out.log</string>
  <key>StandardErrorPath</key><string>/tmp/herdwatch.err.log</string>
</dict>
</plist>
```

- [ ] **Step 2: Write the README**

```markdown
# herdwatch

Keeps a [herdr](https://herdr.dev) pane shown as **working** with a `⏳` status
while background work (CI, roborev review, background jobs, manual markers) is
still pending after the agent went idle — so a finished-looking pane isn't
mistaken for a done one.

## Install

    pip install -e ".[dev]"

## Run

    herdwatch daemon          # supervise via deploy/dev.herdwatch.daemon.plist

## Manual markers

    herdwatch add "deploy" --until 'gh run watch --exit-status'
    herdwatch add "backup" --ttl 600
    herdwatch list
    herdwatch rm <id>

## Config

`~/.config/herdwatch/config.toml` — enable/disable probes, intervals. See
`docs/superpowers/specs/2026-07-01-herdwatch-design.md`.

## Install the launchd agent

    cp deploy/dev.herdwatch.daemon.plist ~/Library/LaunchAgents/
    launchctl load ~/Library/LaunchAgents/dev.herdwatch.daemon.plist
```

- [ ] **Step 3: Run the full test suite**

Run: `python -m pytest -v`
Expected: PASS (all tasks' tests green)

- [ ] **Step 4: Commit**

```bash
git add deploy/dev.herdwatch.daemon.plist README.md
git commit -m "docs: add launchd unit and README"
```

---

## Post-implementation manual verification (gated, against real herdr)

Not a task; run once after the suite is green. Mirrors the design's feasibility
experiments and must target a throwaway split pane, never a live agent:

1. `herdr pane split --pane <your> --direction down --ratio 0.2 --no-focus` → note new pane id.
2. Start the daemon; from the new pane run a fake long job (`sleep 120 &`) or `herdwatch add test --ttl 60`.
3. Confirm `herdr pane get <new>` shows `agent_status=working` + `custom_status` while pending.
4. Clear the marker / let the job finish; confirm it releases and self-heals.
5. `herdr pane close <new>`.

---

## Self-Review

**Spec coverage:**
- Goal / assert-working-with-custom_status → Tasks 3, 12, 13. ✓
- Auto-detection, all-panes, agent-agnostic → Task 13 (`tick` over `agent_list`). ✓
- Probes: roborev / ci / bgjobs / marker → Tasks 10, 9, 11, 8. ✓
- State machine (assert/maintain/release, idempotent, only when idle/done) → Task 13. ✓
- 32-char truncation, `working` only, source `herdwatch` → Tasks 3, 12, 13 + Global Constraints. ✓
- Performance caching by (repo, sha) + roborev status gate → Tasks 4, 9, 10, 13 (reprobe throttle). ✓
- Config schema → Task 6. ✓
- CLI + markers → Tasks 7, 14. ✓
- Packaging (launchd) → Task 15. ✓
- Degrade-to-not-pending on tool error → Tasks 9, 10, 11, 12 (all catch + return empty/None). ✓
- v1 polling vs subscription follow-up → Architecture note. Subscription is explicitly out of v1.

**Placeholder scan:** No TBD/TODO; every code step is complete. bgjobs and roborev json shapes are resolved from real tool output (verified 2026-07-01).

**Type consistency:** `Pending(label, priority, source)`, `PaneContext(...)`, `GitInfo(...)`, probe `check(ctx)->Pending|None`, `HerdrClient.report_agent/release_agent/agent_list/pane_process_info`, `Daemon.tick/managed`, `MarkerStore.add/all/remove/is_pending/active_for_pane`, `TTLCache.get_or`, `aggregate(list[Pending])->str|None` — used consistently across Tasks 2–14.
