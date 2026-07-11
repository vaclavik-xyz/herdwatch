# herdwatch Socket API Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace CLI-subprocess polling with herdr's raw socket API (event-driven daemon) and move display-only labels to `pane.report_metadata` (done-pane ⏳, progress labels).

**Architecture:** New stdlib-only transport module (`herdr_socket.py`: one-request-per-connection `request()` + persistent `EventStream`), `HerdrClient` facade rewritten over it, daemon main loop rewritten as a single-threaded `selectors` loop (events trigger, snapshots are truth), per-pane probe logic preserved. Spec: `docs/superpowers/specs/2026-07-11-socket-api-migration-design.md` — read it before starting any task.

**Tech Stack:** Python ≥ 3.11, stdlib only (socket, selectors, json), pytest.

## Global Constraints

- Work on branch `feat/socket-api-migration` (create from `main` at start; plain merge or rebase at the end, NEVER squash).
- Python stdlib only — no new runtime dependencies.
- herdr server ≥ 0.7.2 (protocol 16) is required at runtime; no CLI-polling fallback. After Task 9 the daemon's herdr communication is socket-only — the sole remaining `herdr` subprocess in `src/herdwatch/` is doctor's `herdr status` CLI-presence check, which stays.
- All herdr writes use `source = "herdwatch"` (existing `SOURCE` constant in `daemon.py`).
- `custom_status` values must stay ≤ 32 chars (herdr caps them; existing `aggregate()`/`progress_label()` already comply — do not add new label sources).
- `ttl_ms` sent to herdr must be clamped to `[1000, 86_400_000]`.
- Commits: conventional format (`feat:`/`fix:`/`refactor:`/`test:`/`docs:`), no `Co-Authored-By`.
- Run tests with: `python3 -m pytest tests/ -q` (pythonpath is configured in pyproject.toml).
- Every task: after its commit, run `roborev show HEAD` (wait for the review with `until roborev show HEAD >/dev/null 2>&1; do sleep 3; done`) and fix reported findings before moving on.

---

### Task 1: `herdr_socket.request()` — one-shot socket transport

**Files:**
- Create: `src/herdwatch/herdr_socket.py`
- Create: `tests/test_herdr_socket.py`

**Interfaces:**
- Consumes: nothing (leaf module).
- Produces (used by Tasks 2, 3, 10):
  - `resolve_socket_path(env: dict | None = None) -> str`
  - `request(method: str, params: dict, *, socket_path: str | None = None, timeout_s: float = 10.0) -> dict` — returns the response `result` object
  - `class HerdrUnavailable(Exception)` — connect/timeout/EOF
  - `class HerdrApiError(Exception)` with `.code: str` and `.message: str`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_herdr_socket.py`:

```python
# tests/test_herdr_socket.py
import json
import os
import socket
import threading

import pytest

from herdwatch.herdr_socket import (
    HerdrApiError,
    HerdrUnavailable,
    request,
    resolve_socket_path,
)


class FakeServer:
    """Minimal herdr-like ndjson unix-socket server: one request per
    connection, scripted response per method."""

    def __init__(self, tmp_path, responses=None):
        self.path = str(tmp_path / "herdr.sock")
        self.responses = responses or {}
        self.requests = []
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(self.path)
        self._srv.listen(8)
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            with conn:
                buf = b""
                while b"\n" not in buf:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                if b"\n" not in buf:
                    continue
                msg = json.loads(buf.split(b"\n", 1)[0])
                self.requests.append(msg)
                reply = self.responses.get(msg["method"])
                if reply is None:
                    reply = {"id": msg["id"],
                             "error": {"code": "unknown_method", "message": msg["method"]}}
                else:
                    reply = dict(reply)
                    reply.setdefault("id", msg["id"])
                conn.sendall(json.dumps(reply).encode() + b"\n")

    def close(self):
        self._stop = True
        try:
            self._srv.close()
        except OSError:
            pass
        try:
            os.unlink(self.path)
        except OSError:
            pass


@pytest.fixture
def server(tmp_path):
    srv = FakeServer(tmp_path, responses={
        "ping": {"result": {"type": "pong"}},
    })
    yield srv
    srv.close()


def test_resolve_prefers_explicit_socket_path():
    env = {"HERDR_SOCKET_PATH": "/x/y.sock", "HERDR_SESSION": "s1"}
    assert resolve_socket_path(env) == "/x/y.sock"


def test_resolve_uses_session_socket():
    env = {"HERDR_SESSION": "s1"}
    assert resolve_socket_path(env).endswith("/.config/herdr/sessions/s1/herdr.sock")


def test_resolve_defaults_to_main_socket():
    assert resolve_socket_path({}).endswith("/.config/herdr/herdr.sock")


def test_request_returns_result(server):
    result = request("ping", {}, socket_path=server.path)
    assert result == {"type": "pong"}
    assert server.requests[0]["method"] == "ping"
    assert server.requests[0]["params"] == {}


def test_request_raises_api_error(server):
    with pytest.raises(HerdrApiError) as exc:
        request("nope.nope", {}, socket_path=server.path)
    assert exc.value.code == "unknown_method"


def test_request_raises_unavailable_when_no_socket(tmp_path):
    with pytest.raises(HerdrUnavailable):
        request("ping", {}, socket_path=str(tmp_path / "missing.sock"))


def test_request_raises_unavailable_on_eof(tmp_path):
    # server that accepts and closes without responding
    path = str(tmp_path / "dead.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)

    def close_immediately():
        conn, _ = srv.accept()
        conn.close()

    t = threading.Thread(target=close_immediately, daemon=True)
    t.start()
    try:
        with pytest.raises(HerdrUnavailable):
            request("ping", {}, socket_path=path)
    finally:
        srv.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_herdr_socket.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'herdwatch.herdr_socket'`

- [ ] **Step 3: Write the implementation**

Create `src/herdwatch/herdr_socket.py`:

```python
# src/herdwatch/herdr_socket.py
"""Raw herdr socket API transport (ndjson over a unix domain socket).

herdr's protocol is one request per connection: send a single
`{"id", "method", "params"}` line, read a single response line, close.
An `events.subscribe` request instead keeps the connection open and
streams event lines — see EventStream (added separately).
"""
from __future__ import annotations

import json
import os
import socket

DEFAULT_SOCKET_PATH = "~/.config/herdr/herdr.sock"
SESSION_SOCKET_PATH = "~/.config/herdr/sessions/{name}/herdr.sock"
_RECV_CHUNK = 65536


class HerdrUnavailable(Exception):
    """herdr server unreachable: connect failure, timeout, or EOF."""


class HerdrApiError(Exception):
    """Structured error response from herdr."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def resolve_socket_path(env: dict | None = None) -> str:
    """herdr's documented resolution order: HERDR_SOCKET_PATH,
    HERDR_SESSION's socket, default session socket."""
    env = os.environ if env is None else env
    explicit = env.get("HERDR_SOCKET_PATH")
    if explicit:
        return explicit
    session = env.get("HERDR_SESSION")
    if session:
        return os.path.expanduser(SESSION_SOCKET_PATH.format(name=session))
    return os.path.expanduser(DEFAULT_SOCKET_PATH)


def _parse_response(line: bytes) -> dict:
    msg = json.loads(line)
    err = msg.get("error")
    if err:
        raise HerdrApiError(err.get("code", "unknown"), err.get("message", ""))
    return msg.get("result") or {}


def request(method: str, params: dict, *, socket_path: str | None = None,
            timeout_s: float = 10.0) -> dict:
    path = socket_path or resolve_socket_path()
    try:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(timeout_s)
        conn.connect(path)
    except OSError as exc:
        raise HerdrUnavailable(str(exc)) from exc
    try:
        payload = json.dumps({"id": "herdwatch", "method": method, "params": params})
        conn.sendall(payload.encode() + b"\n")
        buf = b""
        while b"\n" not in buf:
            try:
                chunk = conn.recv(_RECV_CHUNK)
            except OSError as exc:
                raise HerdrUnavailable(str(exc)) from exc
            if not chunk:
                raise HerdrUnavailable("connection closed before response")
            buf += chunk
    finally:
        conn.close()
    return _parse_response(buf.split(b"\n", 1)[0])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_herdr_socket.py -q`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add src/herdwatch/herdr_socket.py tests/test_herdr_socket.py
git commit -m "feat: add raw herdr socket transport (request)"
```

---

### Task 2: `herdr_socket.EventStream` — persistent subscription connection

**Files:**
- Modify: `src/herdwatch/herdr_socket.py` (append)
- Modify: `tests/test_herdr_socket.py` (append; extend `FakeServer`)

**Interfaces:**
- Consumes: Task 1 (`resolve_socket_path`, `HerdrUnavailable`, `HerdrApiError`).
- Produces (used by Tasks 8, 9):
  - `class EventStream:` with `__init__(self, subscriptions: list[dict], *, socket_path: str | None = None, ack_timeout_s: float = 10.0)`, `fileno() -> int`, `read_events() -> list[dict]` (non-blocking, sets `closed` on EOF), `closed: bool` attribute, `close() -> None`.

- [ ] **Step 1: Extend FakeServer for subscriptions and write failing tests**

In `tests/test_herdr_socket.py`, replace the `FakeServer._serve` connection body with a version that keeps subscribe connections open, and add a `push()` helper:

```python
# replace the FakeServer class implementation with:
class FakeServer:
    """Minimal herdr-like ndjson unix-socket server: one request per
    connection; `events.subscribe` connections stay open for push()."""

    def __init__(self, tmp_path, responses=None, subscribe_error=None):
        self.path = str(tmp_path / "herdr.sock")
        self.responses = responses or {}
        self.subscribe_error = subscribe_error
        self.requests = []
        self.subscriptions = []
        self._sub_conns = []
        self._lock = threading.Lock()
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(self.path)
        self._srv.listen(8)
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
            if b"\n" not in buf:
                conn.close()
                continue
            msg = json.loads(buf.split(b"\n", 1)[0])
            self.requests.append(msg)
            if msg["method"] == "events.subscribe":
                if self.subscribe_error is not None:
                    reply = {"id": msg["id"], "error": self.subscribe_error}
                    conn.sendall(json.dumps(reply).encode() + b"\n")
                    conn.close()
                    continue
                self.subscriptions.append(msg["params"]["subscriptions"])
                # register BEFORE the ack: the client returns from its
                # constructor on the ack, and may push()/inspect immediately
                with self._lock:
                    self._sub_conns.append(conn)
                ack = {"id": msg["id"], "result": {"type": "subscription_started"}}
                conn.sendall(json.dumps(ack).encode() + b"\n")
                continue
            reply = self.responses.get(msg["method"])
            if reply is None:
                reply = {"id": msg["id"],
                         "error": {"code": "unknown_method", "message": msg["method"]}}
            else:
                reply = dict(reply)
                reply.setdefault("id", msg["id"])
            conn.sendall(json.dumps(reply).encode() + b"\n")
            conn.close()

    def push(self, event: dict):
        with self._lock:
            for conn in self._sub_conns:
                conn.sendall(json.dumps(event).encode() + b"\n")

    def drop_subscribers(self):
        with self._lock:
            for conn in self._sub_conns:
                conn.close()
            self._sub_conns = []

    def close(self):
        self._stop = True
        self.drop_subscribers()
        try:
            self._srv.close()
        except OSError:
            pass
        try:
            os.unlink(self.path)
        except OSError:
            pass
```

Append the new tests (plus `import time` and `from herdwatch.herdr_socket import EventStream` at the top):

```python
def _wait_events(stream, want=1, timeout=2.0):
    deadline = time.time() + timeout
    got = []
    while time.time() < deadline and len(got) < want and not stream.closed:
        got.extend(stream.read_events())
        time.sleep(0.01)
    return got


def test_event_stream_subscribes_and_receives(server):
    stream = EventStream([{"type": "pane.created"}], socket_path=server.path)
    try:
        assert server.subscriptions == [[{"type": "pane.created"}]]
        server.push({"event": "pane.created", "data": {"type": "pane_created"}})
        events = _wait_events(stream)
        assert events == [{"event": "pane.created", "data": {"type": "pane_created"}}]
        assert not stream.closed
    finally:
        stream.close()


def test_event_stream_buffers_partial_lines(server):
    stream = EventStream([{"type": "pane.created"}], socket_path=server.path)
    try:
        raw = json.dumps({"event": "pane.created", "data": {}}).encode() + b"\n"
        with server._lock:
            conn = server._sub_conns[0]
        conn.sendall(raw[:7])
        assert stream.read_events() == []       # partial line buffered
        conn.sendall(raw[7:])
        assert _wait_events(stream) == [{"event": "pane.created", "data": {}}]
    finally:
        stream.close()


def test_event_stream_sets_closed_on_eof(server):
    stream = EventStream([{"type": "pane.created"}], socket_path=server.path)
    try:
        server.drop_subscribers()
        deadline = time.time() + 2.0
        while time.time() < deadline and not stream.closed:
            stream.read_events()
            time.sleep(0.01)
        assert stream.closed
    finally:
        stream.close()


def test_event_stream_raises_on_error_ack(tmp_path):
    srv = FakeServer(tmp_path, subscribe_error={"code": "not_found", "message": "pane gone"})
    try:
        with pytest.raises(HerdrApiError):
            EventStream([{"type": "pane.agent_status_changed", "pane_id": "w1:p1"}],
                        socket_path=srv.path)
    finally:
        srv.close()


def test_event_stream_raises_unavailable_when_no_socket(tmp_path):
    with pytest.raises(HerdrUnavailable):
        EventStream([{"type": "pane.created"}], socket_path=str(tmp_path / "no.sock"))
```

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `python3 -m pytest tests/test_herdr_socket.py -q`
Expected: Task 1 tests PASS, new tests FAIL with `ImportError: cannot import name 'EventStream'`

- [ ] **Step 3: Implement EventStream**

Append to `src/herdwatch/herdr_socket.py`:

```python
class EventStream:
    """Persistent `events.subscribe` connection.

    The subscription set is fixed at construction (herdr semantics); to
    change it, close this stream and open a new one. `read_events()` never
    blocks; EOF/errors set `closed` instead of raising so the caller's
    loop can tear down and reconnect.
    """

    def __init__(self, subscriptions: list[dict], *, socket_path: str | None = None,
                 ack_timeout_s: float = 10.0) -> None:
        path = socket_path or resolve_socket_path()
        self.closed = False
        self._buf = b""
        try:
            self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._sock.settimeout(ack_timeout_s)
            self._sock.connect(path)
            payload = json.dumps({"id": "herdwatch-sub", "method": "events.subscribe",
                                  "params": {"subscriptions": subscriptions}})
            self._sock.sendall(payload.encode() + b"\n")
            while b"\n" not in self._buf:
                chunk = self._sock.recv(_RECV_CHUNK)
                if not chunk:
                    raise HerdrUnavailable("connection closed before subscribe ack")
                self._buf += chunk
        except HerdrUnavailable:
            self.close()
            raise
        except OSError as exc:
            self.close()
            raise HerdrUnavailable(str(exc)) from exc
        line, self._buf = self._buf.split(b"\n", 1)
        try:
            _parse_response(line)  # raises HerdrApiError on error ack
        except HerdrApiError:
            self.close()
            raise
        self._sock.setblocking(False)

    def fileno(self) -> int:
        return self._sock.fileno()

    def read_events(self) -> list[dict]:
        """Drain complete event lines without blocking. On EOF or a socket
        error, parse what remains and set `closed`."""
        if not self.closed:
            while True:
                try:
                    chunk = self._sock.recv(_RECV_CHUNK)
                except (BlockingIOError, InterruptedError):
                    break
                except OSError:
                    self.closed = True
                    break
                if not chunk:
                    self.closed = True
                    break
                self._buf += chunk
        events: list[dict] = []
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except ValueError:
                pass  # skip malformed line, keep the stream alive
        return events

    def close(self) -> None:
        self.closed = True
        sock = getattr(self, "_sock", None)
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_herdr_socket.py -q`
Expected: 12 passed

- [ ] **Step 5: Commit**

```bash
git add src/herdwatch/herdr_socket.py tests/test_herdr_socket.py
git commit -m "feat: add herdr event subscription stream"
```

---

### Task 3: Rewrite `HerdrClient` over the socket transport

**Files:**
- Modify: `src/herdwatch/herdr.py` (full rewrite)
- Modify: `tests/test_herdr.py` (full rewrite)

**Interfaces:**
- Consumes: Task 1 (`herdr_socket.request`, `HerdrApiError`, `HerdrUnavailable`).
- Produces (used by Tasks 5–11):
  - `HerdrClient(socket_path: str | None = None, request=herdr_socket.request)`
  - `session_snapshot() -> dict` — **raises** `HerdrUnavailable` / `HerdrApiError` (caller distinguishes herdr-down from old-server)
  - `agent_get(pane_id: str) -> dict | None` — None on any failure
  - `report_agent(pane_id, source, agent, state, custom_status=None) -> bool`
  - `release_agent(pane_id, source, agent) -> str` — `"ok"` (released), `"gone"` (structured `not_found`: the pane id no longer exists — the pane may have been **moved**, so the caller must reconcile before dropping bookkeeping), `"failed"` (transport/other error, retry)
  - `report_metadata(pane_id, source, *, agent=None, custom_status=None, clear_custom_status=False, ttl_ms=None) -> bool` — `not_found` counts as True only when clearing
  - `pane_process_info(pane_id) -> dict` — `{}` on failure
- **Deleted:** `agent_list()`, `agent_explain()` (the progress path no longer masks state, so explain has no consumer).

- [ ] **Step 1: Write the failing tests**

Replace `tests/test_herdr.py` entirely:

```python
# tests/test_herdr.py
import pytest

from herdwatch.herdr import HerdrClient
from herdwatch.herdr_socket import HerdrApiError, HerdrUnavailable


class FakeRequest:
    """Records calls; per-method scripted result or exception."""

    def __init__(self, results=None):
        self.results = results or {}
        self.calls = []

    def __call__(self, method, params, *, socket_path=None, timeout_s=10.0):
        self.calls.append((method, params))
        outcome = self.results.get(method, {})
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_session_snapshot_returns_result():
    req = FakeRequest({"session.snapshot": {"agents": [{"pane_id": "w1:p1"}]}})
    c = HerdrClient(request=req)
    assert c.session_snapshot() == {"agents": [{"pane_id": "w1:p1"}]}


def test_session_snapshot_propagates_errors():
    c = HerdrClient(request=FakeRequest({"session.snapshot": HerdrApiError("unknown_method", "x")}))
    with pytest.raises(HerdrApiError):
        c.session_snapshot()
    c = HerdrClient(request=FakeRequest({"session.snapshot": HerdrUnavailable("down")}))
    with pytest.raises(HerdrUnavailable):
        c.session_snapshot()


def test_agent_get_returns_record_and_none_on_failure():
    req = FakeRequest({"agent.get": {"agent": {"pane_id": "w1:p1", "agent_status": "idle"}}})
    assert HerdrClient(request=req).agent_get("w1:p1") == {"pane_id": "w1:p1", "agent_status": "idle"}
    assert req.calls == [("agent.get", {"target": "w1:p1"})]
    assert HerdrClient(request=FakeRequest({"agent.get": HerdrUnavailable("down")})).agent_get("w1:p1") is None
    assert HerdrClient(request=FakeRequest({"agent.get": HerdrApiError("not_found", "x")})).agent_get("w1:p1") is None


def test_report_agent_sends_params_and_maps_result():
    req = FakeRequest({"pane.report_agent": {"type": "ok"}})
    c = HerdrClient(request=req)
    assert c.report_agent("w1:p1", "herdwatch", "claude", "working", "⏳ CI") is True
    assert req.calls == [("pane.report_agent",
                          {"pane_id": "w1:p1", "source": "herdwatch", "agent": "claude",
                           "state": "working", "custom_status": "⏳ CI"})]
    assert HerdrClient(request=FakeRequest({"pane.report_agent": HerdrUnavailable("x")})) \
        .report_agent("w1:p1", "herdwatch", "claude", "working") is False


def test_report_agent_omits_custom_status_when_none():
    req = FakeRequest({"pane.report_agent": {"type": "ok"}})
    HerdrClient(request=req).report_agent("w1:p1", "herdwatch", "claude", "working")
    assert "custom_status" not in req.calls[0][1]


def test_release_agent_returns_tristate():
    # "gone" is NOT success: the pane id may have changed via a move while
    # the assertion lives on -- the daemon reconciles before dropping state
    assert HerdrClient(request=FakeRequest({"pane.release_agent": {"type": "ok"}})) \
        .release_agent("w1:p1", "herdwatch", "claude") == "ok"
    assert HerdrClient(request=FakeRequest({"pane.release_agent": HerdrApiError("not_found", "gone")})) \
        .release_agent("w1:p1", "herdwatch", "claude") == "gone"
    assert HerdrClient(request=FakeRequest({"pane.release_agent": HerdrApiError("invalid_params", "x")})) \
        .release_agent("w1:p1", "herdwatch", "claude") == "failed"
    assert HerdrClient(request=FakeRequest({"pane.release_agent": HerdrUnavailable("down")})) \
        .release_agent("w1:p1", "herdwatch", "claude") == "failed"


def test_report_metadata_set_and_clear():
    req = FakeRequest({"pane.report_metadata": {"type": "ok"}})
    c = HerdrClient(request=req)
    assert c.report_metadata("w1:p1", "herdwatch", agent="claude",
                             custom_status="⏳ CI", ttl_ms=30000) is True
    assert req.calls[-1] == ("pane.report_metadata",
                             {"pane_id": "w1:p1", "source": "herdwatch", "agent": "claude",
                              "custom_status": "⏳ CI", "ttl_ms": 30000})
    assert c.report_metadata("w1:p1", "herdwatch", clear_custom_status=True) is True
    assert req.calls[-1] == ("pane.report_metadata",
                             {"pane_id": "w1:p1", "source": "herdwatch",
                              "clear_custom_status": True})


def test_report_metadata_not_found_true_only_for_clear():
    err = FakeRequest({"pane.report_metadata": HerdrApiError("not_found", "gone")})
    c = HerdrClient(request=err)
    assert c.report_metadata("w1:p1", "herdwatch", clear_custom_status=True) is True
    assert c.report_metadata("w1:p1", "herdwatch", custom_status="⏳ CI") is False


def test_pane_process_info_maps_result_and_failure():
    req = FakeRequest({"pane.process_info": {"process_info": {"shell_pid": 1}}})
    assert HerdrClient(request=req).pane_process_info("w1:p1") == {"shell_pid": 1}
    assert HerdrClient(request=FakeRequest({"pane.process_info": HerdrUnavailable("x")})) \
        .pane_process_info("w1:p1") == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_herdr.py -q`
Expected: FAIL — `TypeError: HerdrClient.__init__() got an unexpected keyword argument 'request'` (or import errors)

- [ ] **Step 3: Rewrite the client**

Replace `src/herdwatch/herdr.py` entirely:

```python
# src/herdwatch/herdr.py
"""HerdrClient: herdwatch's facade over the raw herdr socket API.

Write methods keep boolean semantics (False = failed, retry later) so the
daemon's retry logic stays transport-agnostic. `release`/`clear` calls
treat a structured `not_found` as success: the pane is gone, so there is
nothing left to release. `session_snapshot` raises instead — the daemon
needs to tell "herdr is down" (HerdrUnavailable, retry with backoff) from
"server too old for session.snapshot" (HerdrApiError, log the >= 0.7.2
requirement).
"""
from __future__ import annotations

import logging
from typing import Callable

from . import herdr_socket
from .herdr_socket import HerdrApiError, HerdrUnavailable

log = logging.getLogger(__name__)


class HerdrClient:
    def __init__(self, socket_path: str | None = None,
                 request: Callable[..., dict] = herdr_socket.request) -> None:
        self._socket_path = socket_path
        self._request = request

    def _call(self, method: str, params: dict) -> dict:
        """Raises HerdrApiError / HerdrUnavailable."""
        return self._request(method, params, socket_path=self._socket_path)

    def _call_bool(self, method: str, params: dict, *, not_found_ok: bool) -> bool:
        try:
            self._call(method, params)
            return True
        except HerdrApiError as exc:
            if not_found_ok and exc.code == "not_found":
                return True
            log.warning("herdr %s failed: %s", method, exc)
            return False
        except HerdrUnavailable as exc:
            log.warning("herdr unavailable for %s: %s", method, exc)
            return False

    def session_snapshot(self) -> dict:
        return self._call("session.snapshot", {})

    def agent_get(self, pane_id: str) -> dict | None:
        try:
            result = self._call("agent.get", {"target": pane_id})
        except (HerdrApiError, HerdrUnavailable) as exc:
            log.debug("agent.get %s failed: %s", pane_id, exc)
            return None
        agent = result.get("agent")
        return agent if isinstance(agent, dict) else None

    def report_agent(self, pane_id: str, source: str, agent: str, state: str,
                     custom_status: str | None = None) -> bool:
        params = {"pane_id": pane_id, "source": source, "agent": agent, "state": state}
        if custom_status:
            params["custom_status"] = custom_status
        return self._call_bool("pane.report_agent", params, not_found_ok=False)

    def release_agent(self, pane_id: str, source: str, agent: str) -> str:
        """Returns "ok", "gone" (pane id unknown to herdr -- possibly moved,
        the caller must reconcile before dropping bookkeeping), or "failed"."""
        params = {"pane_id": pane_id, "source": source, "agent": agent}
        try:
            self._call("pane.release_agent", params)
            return "ok"
        except HerdrApiError as exc:
            if exc.code == "not_found":
                return "gone"
            log.warning("herdr pane.release_agent failed: %s", exc)
            return "failed"
        except HerdrUnavailable as exc:
            log.warning("herdr unavailable for pane.release_agent: %s", exc)
            return "failed"

    def report_metadata(self, pane_id: str, source: str, *, agent: str | None = None,
                        custom_status: str | None = None,
                        clear_custom_status: bool = False,
                        ttl_ms: int | None = None) -> bool:
        params: dict = {"pane_id": pane_id, "source": source}
        if agent:
            params["agent"] = agent
        if clear_custom_status:
            params["clear_custom_status"] = True
        if custom_status is not None:
            params["custom_status"] = custom_status
        if ttl_ms is not None:
            params["ttl_ms"] = ttl_ms
        return self._call_bool("pane.report_metadata", params,
                               not_found_ok=clear_custom_status)

    def pane_process_info(self, pane_id: str) -> dict:
        try:
            result = self._call("pane.process_info", {"pane_id": pane_id})
        except (HerdrApiError, HerdrUnavailable):
            return {}
        info = result.get("process_info")
        return info if isinstance(info, dict) else {}
```

Note: `agent.get` uses `{"target": pane_id}` (the agent CLI resolves targets; `agent.get` accepts a pane id as target). If the live server rejects `target`, check `herdr api schema --json | python3 -c "import json,sys; s=json.load(sys.stdin); print([v for v in s['schemas']['request']['oneOf'] if 'agent.get' in str(v)][:1])"` and adjust the param name — the daemon tests are transport-agnostic either way.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_herdr.py -q`
Expected: all pass. Also run `python3 -m pytest tests/ -q` — the whole suite stays green at this point (`tests/test_daemon.py` uses its own FakeClient and `daemon.py` is untouched here); the daemon rewrite lands in Tasks 5–8.

- [ ] **Step 5: Commit**

```bash
git add src/herdwatch/herdr.py tests/test_herdr.py
git commit -m "feat: rewrite HerdrClient over the raw socket API"
```

---

### Task 4: Config — `resync_interval_s`, `[progress] interval_s`, deprecate `poll_interval_s`

**Files:**
- Modify: `src/herdwatch/config.py`
- Modify: `src/herdwatch/cli.py` (`_cmd_daemon`: drop the interval argument)
- Modify: `src/herdwatch/daemon.py` (one line: default for the old `run()` param)
- Modify: `tests/test_config.py` (append), `tests/test_cli.py` (FakeDaemon.run signature)

**Interfaces:**
- Produces (used by Tasks 9, 10): `Config.resync_interval_s: float = 60.0`, `Config.progress_interval_s: float = 4.0`. `Config.poll_interval_s` is **removed**; a `poll_interval_s` key in `[daemon]` only logs a deprecation warning. `cli._cmd_daemon` calls `daemon.run()` with no arguments from here on.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py`:

```python
def test_new_interval_defaults():
    cfg = load(path="/nonexistent/config.toml")
    assert cfg.resync_interval_s == 60.0
    assert cfg.progress_interval_s == 4.0
    assert not hasattr(cfg, "poll_interval_s")


def test_new_intervals_load_from_file(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("[daemon]\nresync_interval_s = 120\n[progress]\ninterval_s = 2.5\n")
    cfg = load(path=str(p))
    assert cfg.resync_interval_s == 120.0
    assert cfg.progress_interval_s == 2.5


def test_poll_interval_is_ignored_with_warning(tmp_path, caplog):
    p = tmp_path / "config.toml"
    p.write_text("[daemon]\npoll_interval_s = 4\n")
    with caplog.at_level("WARNING"):
        cfg = load(path=str(p))
    assert not hasattr(cfg, "poll_interval_s")
    assert any("poll_interval_s" in r.message for r in caplog.records)
```

(`tests/test_config.py` already imports `load`; keep its existing imports.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_config.py -q`
Expected: new tests FAIL (`AttributeError: resync_interval_s` / `hasattr` assertion)

- [ ] **Step 3: Implement**

In `src/herdwatch/config.py`:
- add `import logging` and `log = logging.getLogger(__name__)` at module level;
- in `Config`, **remove** `poll_interval_s: float = 4.0` and add:

```python
    resync_interval_s: float = 60.0
    progress_interval_s: float = 4.0
```

- in `load()`, replace the `cfg.poll_interval_s = ...` line with:

```python
    if "poll_interval_s" in daemon:
        log.warning("config: daemon.poll_interval_s is deprecated and ignored "
                    "(the daemon is event-driven; see resync_interval_s)")
    cfg.resync_interval_s = float(daemon.get("resync_interval_s", cfg.resync_interval_s))
```

- in the `[progress]` handling block, after the `enabled` handling add:

```python
    if isinstance(prog, dict) and "interval_s" in prog:
        cfg.progress_interval_s = float(prog["interval_s"])
```

Removing the field breaks its consumers — fix them in the same commit:
- `src/herdwatch/cli.py` `_cmd_daemon`: replace `daemon.run(cfg.poll_interval_s)` with `daemon.run()`.
- `src/herdwatch/daemon.py`: give the old loop a default so the argless call works until the rewrite lands — `def run(self, poll_interval_s: float = 4.0, sleep: Callable[[float], None] = time.sleep) -> None:` (temporary; Tasks 5/8 replace `run` entirely).
- `tests/test_cli.py`: the daemon-command test's fake daemon defines `def run(self, interval): ...` — change it to `def run(self, interval=None): ...` (it keeps passing in both worlds).

- [ ] **Step 4: Run tests, expect existing + new to pass**

Run: `python3 -m pytest tests/ -q`
Expected: the whole suite passes. If an existing config test asserts `poll_interval_s`, update it to assert the deprecation behavior instead (delete the assertion, keep the rest).

- [ ] **Step 5: Commit**

```bash
git add src/herdwatch/config.py src/herdwatch/cli.py src/herdwatch/daemon.py tests/test_config.py tests/test_cli.py
git commit -m "feat: add resync/progress intervals, deprecate poll_interval_s"
```

---

### Task 5: Daemon per-pane engine + sweeps (registry, hold/done/progress, TTL, adopt/legacy, shutdown)

The heart of the migration. Replaces `tick()` with a registry + two sweeps; per-pane decisions keep today's semantics. Read the spec sections "daemon.py", "Per-pane behavior", and the reprobe/progress sweep bullets before starting.

**Files:**
- Modify: `src/herdwatch/daemon.py` (major rewrite; keep `SOURCE`, `MARKER_DIR`, `_context` logic, probe/aggregate usage)
- Modify: `tests/test_daemon.py` (full rewrite of harness + ported tests)

**Interfaces:**
- Consumes: Task 3 client method signatures (see Task 3 Produces).
- Produces (used by Tasks 6–10):
  - `ManagedPane(custom_status, agent, kind="hold", terminal_id="")`
  - `Daemon.__init__(client, probes, *, reprobe_interval_s=15.0, resync_interval_s=60.0, progress_interval_s=4.0, clock=time.time, enrich=gitctx.enrich, allow=None, deny=None, on_snapshot=lambda rows: None, progress=None, stream_factory=None, backoff_base_s=0.5, backoff_max_s=30.0)`
  - state: `managed: dict[str, ManagedPane]`, `_legacy_release: dict[str, ManagedPane]`, `_registry: dict[str, dict]`, `_session_cache`, `_last_probe`, `_meta_asserted_at`, `_adopted`, `_resync_due: bool`, `_stream`
  - methods: `adopt(rows)`, `shutdown()`, `_probe_pane(pane_id)`, `_reprobe_sweep()`, `_progress_sweep()`, `_ttl_ms()`, `_remember_record(rec)`, `_rows()`, `_publish()`, `_eligible(pane_id)`, `_release_hold(pane_id, reason) -> bool`, `_clear_metadata(pane_id, reason) -> bool`
  - constants: `TTL_MIN_MS = 1000`, `TTL_MAX_MS = 86_400_000`
- State-file row shape (consumed by Task 9's cli/status): `{"pane_id", "agent", "status", "kind", "terminal_id", "meta": bool}` — `meta` is True for rows the new daemon wrote via `report_metadata` (kinds `progress`/`done`), False otherwise.

- [ ] **Step 1: Rewrite the test harness and port the per-pane tests**

Replace `tests/test_daemon.py` header (everything above the first test) with:

```python
# tests/test_daemon.py
from herdwatch.daemon import Daemon, ManagedPane, SOURCE, TTL_MAX_MS, TTL_MIN_MS
from herdwatch.gitctx import GitInfo
from herdwatch.models import Pending, WorktreeHead


class FakeClient:
    """Socket-era fake: agents keyed by pane_id; snapshot/get read them."""

    def __init__(self, agents=None, report_ok=True, release_ok=True, meta_ok=True):
        self.agents = {a["pane_id"]: a for a in (agents or [])}
        self.reports = []
        self.releases = []
        self.metadata = []          # (pane_id, custom_status, clear, ttl_ms)
        self._report_ok = report_ok
        # tri-state like the real client: "ok" | "gone" | "failed"
        self._release_result = "ok" if release_ok else "failed"
        self._meta_ok = meta_ok
        self.snapshot_error = None  # exception instance to raise from session_snapshot

    def set_agents(self, agents):
        self.agents = {a["pane_id"]: a for a in agents}

    def session_snapshot(self):
        if self.snapshot_error is not None:
            raise self.snapshot_error
        return {"agents": [dict(a) for a in self.agents.values()]}

    def agent_get(self, pane_id):
        a = self.agents.get(pane_id)
        return dict(a) if a is not None else None

    def report_agent(self, pane_id, source, agent, state, custom_status=None):
        self.reports.append((pane_id, state, custom_status))
        return self._report_ok

    def release_agent(self, pane_id, source, agent):
        self.releases.append(pane_id)
        return self._release_result

    def report_metadata(self, pane_id, source, *, agent=None, custom_status=None,
                        clear_custom_status=False, ttl_ms=None):
        self.metadata.append((pane_id, custom_status, clear_custom_status, ttl_ms))
        return self._meta_ok

    def pane_process_info(self, pane_id):
        return {}


class StaticProbe:
    name = "static"

    def __init__(self, result):
        self.result = result

    def check(self, ctx):
        return self.result


_ENRICH = lambda cwd: GitInfo(True, "sha", "main", True)


def _agent(pane="w1:p1", status="idle", agent="claude", term=None):
    return {"pane_id": pane, "terminal_id": term or f"term-{pane}",
            "agent_status": status, "agent": agent, "cwd": "/x"}


def _claude_agent(pane="w1:p1", status="working",
                  session="c00b128f-68c8-4643-82d6-2835c317517d"):
    a = _agent(pane, status)
    if session is not None:
        # herdr exposes agent_session only for idle/done panes, not working
        # ones; pass session=None to mimic a working pane without it
        a["agent_session"] = {"value": session}
    return a


def make_daemon(client, probes=(), **kw):
    kw.setdefault("clock", lambda: 0.0)
    kw.setdefault("enrich", _ENRICH)
    return Daemon(client, list(probes), **kw)


def seed(d, client):
    """Load the fake's agents into the registry (what bootstrap/resync do)."""
    d._registry = {p: dict(a) for p, a in client.agents.items()}
    for rec in d._registry.values():
        d._remember_record(rec)
```

Then port each existing test. The mechanical rule: `Daemon(client, [probe], ...)` → `make_daemon(client, [probe], ...)`; add `seed(d, client)` after construction; `d.tick()` → `d._reprobe_sweep()` (for idle/hold/done flows) or `d._progress_sweep()` (for progress flows); when the fake's agents change mid-test call `client.set_agents([...])` **and** `seed(d, client)` (events/resync keep the registry current in production; Task 7/8 test that separately); `d.release_all()` → `d.shutdown()`. Drop these tests (behavior deleted with `agent_explain`): `test_progress_released_when_explain_fails`. Ported examples — write all of them out concretely in this style:

```python
def test_asserts_working_when_pending():
    client = FakeClient([_agent(status="idle")])
    d = make_daemon(client, [StaticProbe(Pending("review", 30, "roborev"))])
    seed(d, client)
    d._reprobe_sweep()
    assert client.reports == [("w1:p1", "working", "⏳ review")]
    assert "w1:p1" in d.managed
    assert d.managed["w1:p1"].kind == "hold"
    assert d.managed["w1:p1"].terminal_id == "term-w1:p1"


def test_done_pane_gets_metadata_not_hold():
    client = FakeClient([_agent(status="done")])
    d = make_daemon(client, [StaticProbe(Pending("review", 30, "roborev"))])
    seed(d, client)
    d._reprobe_sweep()
    assert client.reports == []                        # semantic state untouched
    assert client.metadata == [("w1:p1", "⏳ review", False, 30000)]
    assert d.managed["w1:p1"].kind == "done"


def test_done_metadata_cleared_when_work_clears():
    client = FakeClient([_agent(status="done")])
    probe = StaticProbe(Pending("review", 30, "roborev"))
    d = make_daemon(client, [probe], reprobe_interval_s=0)
    seed(d, client)
    d._reprobe_sweep()
    probe.result = None
    d._reprobe_sweep()
    assert client.metadata[-1] == ("w1:p1", None, True, None)   # cleared
    assert "w1:p1" not in d.managed


def test_done_metadata_refreshes_ttl_each_sweep():
    # reprobe_interval_s must stay at the 15s default here: 0 would clamp
    # the TTL to TTL_MIN_MS and the throttle is instead advanced via clock
    now = [0.0]
    client = FakeClient([_agent(status="done")])
    d = make_daemon(client, [StaticProbe(Pending("review", 30, "roborev"))],
                    clock=lambda: now[0])
    seed(d, client)
    d._reprobe_sweep()
    now[0] = 16.0                # past the reprobe throttle
    d._reprobe_sweep()
    sets = [m for m in client.metadata if not m[2]]
    assert len(sets) == 2 and all(m[3] == 30000 for m in sets)  # refreshed


def test_done_to_idle_hands_over_to_hold():
    client = FakeClient([_agent(status="done")])
    probe = StaticProbe(Pending("CI: ci", 20, "ci"))
    d = make_daemon(client, [probe], reprobe_interval_s=0)
    seed(d, client)
    d._reprobe_sweep()
    assert d.managed["w1:p1"].kind == "done"
    client.set_agents([_agent(status="idle")])
    seed(d, client)
    d._reprobe_sweep()
    assert client.metadata[-1] == ("w1:p1", None, True, None)   # metadata cleared
    assert client.reports[-1] == ("w1:p1", "working", "⏳ CI: ci")
    assert d.managed["w1:p1"].kind == "hold"


def test_ttl_clamped_to_valid_range():
    client = FakeClient([_agent(status="done")])
    d = make_daemon(client, [StaticProbe(Pending("review", 30, "roborev"))],
                    reprobe_interval_s=0)
    assert d._ttl_ms() == TTL_MIN_MS
    d2 = make_daemon(client, [], reprobe_interval_s=100_000)
    assert d2._ttl_ms() == TTL_MAX_MS


def test_progress_uses_metadata_not_report_agent():
    client = FakeClient([_claude_agent()])
    d = make_daemon(client, progress=lambda sid: "2/5 Fixing auth")
    seed(d, client)
    d._progress_sweep()
    assert client.reports == []
    assert client.metadata == [("w1:p1", "2/5 Fixing auth", False, 30000)]
    assert d.managed["w1:p1"].kind == "progress"


def test_progress_writes_only_on_label_change_within_half_ttl():
    client = FakeClient([_claude_agent()])
    d = make_daemon(client, progress=lambda sid: "2/5 Fixing auth")
    seed(d, client)
    d._progress_sweep()
    d._progress_sweep()          # same label, ttl young -> no second write
    assert len(client.metadata) == 1


def test_progress_refreshes_after_half_ttl():
    now = [0.0]
    client = FakeClient([_claude_agent()])
    d = make_daemon(client, clock=lambda: now[0], progress=lambda sid: "2/5 X")
    seed(d, client)
    d._progress_sweep()
    now[0] = 16.0                # ttl 30s -> half is 15s
    d._progress_sweep()
    assert len(client.metadata) == 2


def test_progress_cleared_when_no_active_task():
    labels = iter(["2/5 Fixing auth", None])
    client = FakeClient([_claude_agent()])
    d = make_daemon(client, progress=lambda sid: next(labels))
    seed(d, client)
    d._progress_sweep()
    d._progress_sweep()
    assert client.metadata[-1] == ("w1:p1", None, True, None)
    assert "w1:p1" not in d.managed


def test_hold_pane_not_claimed_by_progress_sweep():
    # an idle pane held for CI must stay a hold even if its session has an
    # in_progress task (holds own the pane until their work clears)
    client = FakeClient([_claude_agent(status="idle")])
    probe = StaticProbe(Pending("CI: ci", 20, "ci"))
    d = make_daemon(client, [probe], reprobe_interval_s=0,
                    progress=lambda sid: "2/5 X")
    seed(d, client)
    d._reprobe_sweep()
    assert d.managed["w1:p1"].kind == "hold"
    d._registry["w1:p1"]["agent_status"] = "working"   # echo of our own assert
    d._progress_sweep()
    assert d.managed["w1:p1"].kind == "hold"
    assert client.metadata == []


def test_progress_pane_recovered_when_status_drifted():
    # missed working->idle event: reprobe sweep clears the label and runs
    # the idle flow in the same sweep
    client = FakeClient([_claude_agent(status="idle")])
    probe = StaticProbe(Pending("CI: ci", 20, "ci"))
    d = make_daemon(client, [probe], reprobe_interval_s=0,
                    progress=lambda sid: "2/5 X")
    d.managed["w1:p1"] = ManagedPane("2/5 X", "claude", kind="progress",
                                     terminal_id="term-w1:p1")
    seed(d, client)                # registry says idle now
    d._reprobe_sweep()
    assert client.metadata[-1] == ("w1:p1", None, True, None)
    assert d.managed["w1:p1"].kind == "hold"


def test_adopt_hold_rows_and_legacy_rows():
    d = make_daemon(FakeClient([]))
    d.adopt([
        {"pane_id": "w1:p1", "agent": "claude", "status": "⏳ review",
         "kind": "hold", "terminal_id": "t1"},
        {"pane_id": "w2:p1", "agent": "claude", "status": "2/5 X",
         "kind": "progress"},                       # pre-migration row: no meta
        {"pane_id": "w3:p1", "agent": "claude", "status": "3/7 Y",
         "kind": "progress", "meta": True},         # new-daemon row: TTL cleans
    ])
    assert d.managed["w1:p1"].kind == "hold"
    assert d.managed["w1:p1"].terminal_id == "t1"
    assert "w1:p1" in d._adopted
    assert "w2:p1" in d._legacy_release
    assert "w3:p1" not in d.managed and "w3:p1" not in d._legacy_release


def test_legacy_release_retried_until_confirmed():
    client = FakeClient([_agent(pane="w2:p1", status="idle")], release_ok=False)
    d = make_daemon(client)
    d.adopt([{"pane_id": "w2:p1", "agent": "claude", "status": "2/5 X",
              "kind": "progress"}])
    seed(d, client)
    d._reprobe_sweep()
    assert client.releases == ["w2:p1"]
    assert "w2:p1" in d._legacy_release          # failed -> retained
    client._release_result = "ok"
    d._reprobe_sweep()
    assert "w2:p1" not in d._legacy_release      # confirmed -> dropped


def test_release_gone_keeps_entry_and_schedules_resync():
    # a release racing a pane move answers not_found while the assertion
    # survives under the new id: keep the bookkeeping, let resync remap it
    client = FakeClient([_agent(status="idle")])
    probe = StaticProbe(Pending("review", 30, "roborev"))
    d = make_daemon(client, [probe], reprobe_interval_s=0)
    seed(d, client)
    d._reprobe_sweep()
    probe.result = None
    client._release_result = "gone"
    d._reprobe_sweep()
    assert "w1:p1" in d.managed                  # NOT dropped on "gone"
    assert d._resync_due is True                 # reconciliation queued


def test_shutdown_keeps_rows_for_failed_cleanup():
    # a clean shutdown racing a herdr blip must leave the rows in the state
    # file -- the next daemon adopts them; wiping them would orphan the
    # assertion with nothing left to reconcile
    snaps = []
    client = FakeClient([_agent(status="idle")], release_ok=False)
    d = make_daemon(client, [StaticProbe(Pending("review", 30, "roborev"))],
                    reprobe_interval_s=0, on_snapshot=snaps.append)
    seed(d, client)
    d._reprobe_sweep()
    d.shutdown()
    assert "w1:p1" in d.managed                  # retained for adoption
    assert snaps[-1] != []                       # state file still lists it


def test_rows_include_terminal_id_and_meta_flag():
    client = FakeClient([_agent(status="done")])
    d = make_daemon(client, [StaticProbe(Pending("review", 30, "roborev"))])
    seed(d, client)
    d._reprobe_sweep()
    assert d._rows() == [{"pane_id": "w1:p1", "agent": "claude",
                          "status": "⏳ review", "kind": "done",
                          "terminal_id": "term-w1:p1", "meta": True}]


def test_shutdown_releases_holds_and_clears_metadata():
    client = FakeClient([_agent(pane="w1:p1", status="idle"),
                         _agent(pane="w2:p1", status="done")])
    d = make_daemon(client, [StaticProbe(Pending("review", 30, "roborev"))],
                    reprobe_interval_s=0)
    seed(d, client)
    d._reprobe_sweep()
    assert d.managed["w1:p1"].kind == "hold" and d.managed["w2:p1"].kind == "done"
    d.shutdown()
    assert client.releases == ["w1:p1"]
    assert client.metadata[-1] == ("w2:p1", None, True, None)
    assert d.managed == {}
```

Additionally port these existing tests 1:1 with the mechanical rule above (same asserts, new entry points): `test_context_carries_worktree_heads_to_probes`, `test_ignores_working_pane_not_managed`, `test_releases_when_cleared`, `test_reasserts_only_on_label_change`, `test_raising_probe_does_not_crash_tick` (rename `_sweep`), `test_reprobe_throttle_skips_within_interval`, `test_managed_pane_released_when_cleared_even_if_status_working` (set registry status to "working" via `d._registry["w1:p1"]["agent_status"] = "working"` before the second sweep), `test_deny_skips_pane`, `test_allow_only_listed`, `test_unmanaged_idle_pane_is_throttled`, `test_failed_release_keeps_pane_for_retry` (vanish leg moves to Task 7's resync tests — here keep the pane present and only assert failed-release retention on work-cleared), `test_failed_report_does_not_record_managed`, `test_failed_report_is_not_throttled`, `test_failed_work_cleared_release_is_not_throttled`, `test_tick_snapshots_managed_rows` (assert the new row shape with `terminal_id`/`meta`), `test_tick_snapshots_empty_when_nothing_held`, `test_release_all_snapshots_empty` (`shutdown`), `test_raising_snapshot_does_not_crash_tick`, `test_adopt_ignores_rows_without_pane_id`, `test_adopt_defaults_kind_to_hold`, `test_adopted_pane_released_when_work_already_cleared`, `test_adopted_pane_kept_when_still_pending`, `test_adopted_pending_pane_is_reasserted_once`, `test_progress_uses_cached_session_when_working_omits_it` (idle leg via `_reprobe_sweep`, working leg via `_progress_sweep`), `test_progress_skipped_when_session_never_seen`, `test_progress_skips_non_claude_agents`, `test_progress_disabled_leaves_working_panes_alone`, `test_progress_reader_exception_is_contained` — **for every ported progress test, the primary assertion target changes from `client.reports` to `client.metadata`, and every one of them — positive and negative — additionally asserts `client.reports == []`** (this pins the regression where the progress path would wrongly fall back to semantic `report_agent`; e.g. the cached-session test ends with `assert client.metadata == [("w1:p1", "2/5 c00b", False, 30000)]` and `assert client.reports == []`; a negative test asserts both `client.metadata == []` and `client.reports == []`), `test_build_daemon_constructs` (moves to Task 9 — delete here, re-added there). Delete: `test_releases_assertion_when_pane_vanishes`, `test_session_cache_dropped_when_pane_vanishes`, `test_adopted_pane_gone_is_released_on_restart` (all re-created as resync tests in Task 7), `test_progress_released_when_detection_says_stopped`, `test_progress_hands_over_to_hold_in_same_tick`, `test_progress_released_when_blocked`, `test_progress_stop_falls_through_despite_stale_reprobe_timer` (re-created as event tests in Task 6), `test_progress_released_when_explain_fails` (behavior deleted). Also delete these — each is superseded by a new test defined above (old name → replacement): `test_does_not_hold_done_pane` → `test_done_pane_gets_metadata_not_hold` (done panes now DO get a metadata label; the old "leave done alone" behavior is intentionally gone), `test_holds_idle_pane_after_it_was_done` → `test_done_to_idle_hands_over_to_hold`, `test_adopt_seeds_managed_from_rows` and `test_adopt_preserves_progress_kind` → `test_adopt_hold_rows_and_legacy_rows`, `test_progress_asserts_label_for_working_claude_pane` → `test_progress_uses_metadata_not_report_agent`, `test_progress_reasserts_only_on_label_change` → `test_progress_writes_only_on_label_change_within_half_ttl`, `test_progress_released_when_no_active_task` → `test_progress_cleared_when_no_active_task`, `test_hold_pane_not_probed_by_progress_path` → `test_hold_pane_not_claimed_by_progress_sweep`, `test_release_all_releases_managed` → `test_shutdown_releases_holds_and_clears_metadata`. After the rewrite, `grep -n "explain\|tick()" tests/test_daemon.py` must return nothing.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_daemon.py -q`
Expected: FAIL — `ImportError` (TTL constants, ManagedPane signature) and attribute errors.

- [ ] **Step 3: Rewrite the daemon engine**

Replace `src/herdwatch/daemon.py`'s `ManagedPane` and `Daemon` (keep module header, imports, `SOURCE`, `MARKER_DIR`; `build_daemon` is reworked in Task 9 — for now update its `Daemon(...)` call to the new constructor without `poll` usage):

```python
TTL_MIN_MS = 1000
TTL_MAX_MS = 86_400_000


@dataclass
class ManagedPane:
    custom_status: str
    agent: str
    kind: str = "hold"        # "hold" | "progress" | "done"
    terminal_id: str = ""


class Daemon:
    def __init__(self, client, probes, *,
                 reprobe_interval_s: float = 15.0,
                 resync_interval_s: float = 60.0,
                 progress_interval_s: float = 4.0,
                 clock: Callable[[], float] = time.time,
                 enrich: Callable[[str], gitctx.GitInfo] = gitctx.enrich,
                 allow: list[str] | None = None,
                 deny: list[str] | None = None,
                 on_snapshot: Callable[[list[dict]], None] = lambda rows: None,
                 progress: Callable[[str], str | None] | None = None,
                 stream_factory=None,
                 backoff_base_s: float = 0.5,
                 backoff_max_s: float = 30.0) -> None:
        self._client = client
        self._probes = probes
        self._reprobe = reprobe_interval_s
        self._resync_interval = resync_interval_s
        self._progress_interval = progress_interval_s
        self._clock = clock
        self._enrich = enrich
        self._allow = set(allow or [])
        self._deny = set(deny or [])
        self._on_snapshot = on_snapshot
        self._progress = progress
        self._stream_factory = stream_factory
        self._backoff_base = backoff_base_s
        self._backoff_max = backoff_max_s
        self.managed: dict[str, ManagedPane] = {}
        # pre-migration semantic assertions awaiting release (see adopt)
        self._legacy_release: dict[str, ManagedPane] = {}
        # last known agent records, keyed by pane_id (snapshot is truth)
        self._registry: dict[str, dict] = {}
        self._last_probe: dict[str, float] = {}
        # herdr reports agent_session only for idle/done panes, never while a
        # pane is working -- exactly when the progress path needs it. Cache it
        # from any record that carries it.
        self._session_cache: dict[str, str] = {}
        self._meta_asserted_at: dict[str, float] = {}
        self._adopted: set[str] = set()
        self._stream = None
        self._resync_due = False
        self._last_boot_error: str | None = None

    # ---------- small helpers ----------

    def _ttl_ms(self) -> int:
        ttl = int(2 * self._reprobe * 1000)
        return max(TTL_MIN_MS, min(TTL_MAX_MS, ttl))

    def _eligible(self, pane_id: str) -> bool:
        if self._deny and pane_id in self._deny:
            return False
        if self._allow and pane_id not in self._allow:
            return False
        return True

    def _remember_record(self, rec: dict) -> None:
        sess = (rec.get("agent_session") or {}).get("value")
        if sess:
            self._session_cache[rec["pane_id"]] = sess

    def _terminal_id(self, pane_id: str) -> str:
        rec = self._registry.get(pane_id) or {}
        return rec.get("terminal_id") or ""

    def _rows(self) -> list[dict]:
        rows = [{"pane_id": pid, "agent": mp.agent, "status": mp.custom_status,
                 "kind": mp.kind, "terminal_id": mp.terminal_id,
                 "meta": mp.kind in ("progress", "done")}
                for pid, mp in sorted(self.managed.items())]
        rows += [{"pane_id": pid, "agent": mp.agent, "status": mp.custom_status,
                  "kind": mp.kind, "terminal_id": mp.terminal_id, "meta": False}
                 for pid, mp in sorted(self._legacy_release.items())]
        return rows

    def _publish(self) -> None:
        try:
            self._on_snapshot(self._rows())
        except Exception:
            log.warning("snapshot publish failed; continuing", exc_info=True)

    def adopt(self, rows: list[dict]) -> None:
        """Recover state from a prior run. `hold` rows re-adopt (force one
        re-assert). Metadata rows from THIS daemon generation (`meta` flag)
        are skipped -- their TTL self-cleans. Non-hold rows without the flag
        come from a pre-migration daemon: semantic assertions with no TTL,
        kept in a retry set until release_agent confirms."""
        for row in rows:
            pane_id = row.get("pane_id")
            if not pane_id:
                continue
            mp = ManagedPane(row.get("status", ""), row.get("agent", "agent"),
                             kind=row.get("kind", "hold"),
                             terminal_id=row.get("terminal_id", ""))
            if mp.kind == "hold":
                self.managed[pane_id] = mp
                self._adopted.add(pane_id)
            elif not row.get("meta"):
                self._legacy_release[pane_id] = mp

    # ---------- assert / release primitives ----------

    def _assert_hold(self, pane_id: str, agent: str, label: str) -> None:
        if self._client.report_agent(pane_id, SOURCE, agent, "working", label):
            self.managed[pane_id] = ManagedPane(label, agent, kind="hold",
                                                terminal_id=self._terminal_id(pane_id))
            self._adopted.discard(pane_id)
            log.info("hold %s -> %s (%s)", pane_id, label, agent)
        else:
            # report failed (herdr down): don't record, and don't let the
            # throttle defer the retry to the next reprobe interval
            self._last_probe.pop(pane_id, None)

    def _assert_metadata(self, pane_id: str, agent: str, label: str, kind: str) -> None:
        if self._client.report_metadata(pane_id, SOURCE, agent=agent,
                                        custom_status=label, ttl_ms=self._ttl_ms()):
            self.managed[pane_id] = ManagedPane(label, agent, kind=kind,
                                                terminal_id=self._terminal_id(pane_id))
            self._meta_asserted_at[pane_id] = self._clock()
            log.info("%s %s -> %s (%s)", kind, pane_id, label, agent)
        else:
            self._last_probe.pop(pane_id, None)

    def _release_hold(self, pane_id: str, reason: str) -> bool:
        mp = self.managed.get(pane_id)
        if mp is None:
            return True
        outcome = self._client.release_agent(pane_id, SOURCE, mp.agent)
        if outcome == "ok":
            del self.managed[pane_id]
            self._adopted.discard(pane_id)
            log.info("release %s (%s)", pane_id, reason)
            return True
        if outcome == "gone":
            # not_found can mean "moved": the assertion may live on under a
            # new pane id. Keep the bookkeeping and let the next resync
            # reconcile by terminal_id (remap) or confirm the pane is gone
            # (drop). Dropping here would orphan a permanent `working ⏳`.
            self._resync_due = True
            log.info("release of %s answered not_found (%s); reconciling",
                     pane_id, reason)
            return False
        log.warning("release of %s failed (%s); keeping to retry", pane_id, reason)
        return False

    def _clear_metadata(self, pane_id: str, reason: str) -> bool:
        mp = self.managed.get(pane_id)
        if mp is None:
            return True
        if self._client.report_metadata(pane_id, SOURCE, agent=mp.agent,
                                        clear_custom_status=True):
            del self.managed[pane_id]
            self._meta_asserted_at.pop(pane_id, None)
            log.info("clear %s (%s)", pane_id, reason)
            return True
        log.warning("metadata clear of %s failed (%s); keeping to retry",
                    pane_id, reason)
        return False

    # ---------- per-pane decision ----------

    def _context(self, rec: dict) -> PaneContext:
        cwd = rec.get("cwd") or rec.get("foreground_cwd") or ""
        gi = self._enrich(cwd)
        return PaneContext(
            pane_id=rec["pane_id"],
            agent=rec.get("agent") or "agent",
            cwd=cwd,
            status=rec.get("agent_status") or "unknown",
            head_sha=gi.head_sha,
            branch=gi.branch,
            is_git_repo=gi.is_git_repo,
            has_github_remote=gi.has_github_remote,
            worktree_heads=gi.worktree_heads,
        )

    def _run_probes(self, ctx: PaneContext) -> str | None:
        pendings = []
        for p in self._probes:
            try:
                r = p.check(ctx)
            except Exception:
                log.warning("probe %r raised; treating as not pending",
                            getattr(p, "name", p), exc_info=True)
                r = None
            if r:
                pendings.append(r)
        return aggregate(pendings)

    def _probe_pane(self, pane_id: str) -> None:
        """The per-pane decision the old tick() made, driven by the current
        registry record (refreshed via agent.get) and managed kind."""
        if not self._eligible(pane_id):
            return
        now = self._clock()
        last = self._last_probe.get(pane_id)
        if last is not None and (now - last) < self._reprobe:
            return
        rec = self._client.agent_get(pane_id)
        if rec is not None:
            self._registry[pane_id] = rec
            self._remember_record(rec)
        else:
            rec = self._registry.get(pane_id)
            if rec is None:
                return
        status = rec.get("agent_status") or "unknown"
        mp = self.managed.get(pane_id)
        if mp is not None and mp.kind == "progress":
            # recovery path: a progress label whose pane stopped working
            if status != "working":
                if not self._clear_metadata(pane_id, "agent stopped"):
                    return
                self._last_probe.pop(pane_id, None)
                mp = None
            else:
                return  # live progress panes belong to the progress sweep
        ctx = self._context(rec)
        label = self._run_probes(ctx)
        self._last_probe[pane_id] = now
        agent_name = ctx.agent
        if mp is not None and mp.kind == "hold":
            if label:
                if mp.custom_status != label or pane_id in self._adopted:
                    self._assert_hold(pane_id, agent_name, label)
                return
            if not self._release_hold(pane_id, "work cleared"):
                self._last_probe.pop(pane_id, None)
            return
        if mp is not None and mp.kind == "done":
            if status == "done":
                if label:
                    self._assert_metadata(pane_id, agent_name, label, "done")
                elif not self._clear_metadata(pane_id, "work cleared"):
                    self._last_probe.pop(pane_id, None)
                return
            # left done (viewed -> idle, or restarted work): clear the label,
            # then fall through so an idle pane can be held right now
            if not self._clear_metadata(pane_id, f"left done ({status})"):
                self._last_probe.pop(pane_id, None)
                return
            mp = None
        if status == "idle":
            if label:
                self._assert_hold(pane_id, agent_name, label)
            return
        if status == "done":
            if label:
                self._assert_metadata(pane_id, agent_name, label, "done")
            return
        # working/blocked/unknown and unmanaged: leave alone; forget the
        # timer so the next idle/done edge probes immediately
        self._last_probe.pop(pane_id, None)

    # ---------- sweeps ----------

    def _reprobe_sweep(self) -> None:
        for pane_id, mp in list(self._legacy_release.items()):
            outcome = self._client.release_agent(pane_id, SOURCE, mp.agent)
            if outcome == "ok":
                del self._legacy_release[pane_id]
                log.info("released legacy assertion on %s", pane_id)
            elif outcome == "gone":
                self._resync_due = True  # possibly moved: reconcile first
        for pane_id in list(self.managed):
            mp = self.managed.get(pane_id)
            if mp is None:
                continue
            if not self._eligible(pane_id):
                # e.g. moved to a denied pane id: stop managing, with retry
                if mp.kind == "hold":
                    self._release_hold(pane_id, "pane no longer eligible")
                else:
                    self._clear_metadata(pane_id, "pane no longer eligible")
                continue
            self._probe_pane(pane_id)
        for pane_id, rec in list(self._registry.items()):
            if pane_id in self.managed or not self._eligible(pane_id):
                continue
            if rec.get("agent_status") in ("idle", "done"):
                self._probe_pane(pane_id)
        self._publish()

    def _progress_sweep(self) -> None:
        if self._progress is None:
            return
        now = self._clock()
        for pane_id, rec in list(self._registry.items()):
            if not self._eligible(pane_id):
                continue
            mp = self.managed.get(pane_id)
            if mp is not None and mp.kind != "progress":
                continue  # never claim a hold/done pane (spec: progress guard)
            if rec.get("agent_status") != "working" or (rec.get("agent") or "") != "claude":
                continue
            session = ((rec.get("agent_session") or {}).get("value")
                       or self._session_cache.get(pane_id))
            if not session:
                continue
            try:
                label = self._progress(session)
            except Exception:
                log.warning("progress read failed; skipping", exc_info=True)
                continue
            if label:
                stale = (now - self._meta_asserted_at.get(pane_id, 0.0)) >= self._ttl_ms() / 2000.0
                if mp is None or mp.custom_status != label or stale:
                    self._assert_metadata(pane_id, rec.get("agent") or "agent",
                                          label, "progress")
            elif mp is not None:
                self._clear_metadata(pane_id, "no active task")
        self._publish()

    # ---------- shutdown ----------

    def shutdown(self) -> None:
        """Release every assertion herdwatch currently owns (clean exit).
        Rows whose cleanup did NOT succeed stay in the state file so the
        next daemon adopts and reconciles them -- wiping them on a herdr
        blip would orphan live assertions with no record left."""
        for pane_id, mp in list(self.managed.items()):
            try:
                if mp.kind == "hold":
                    if self._client.release_agent(pane_id, SOURCE, mp.agent) == "ok":
                        del self.managed[pane_id]
                else:
                    # metadata self-expires via TTL; drop the row either way
                    self._client.report_metadata(pane_id, SOURCE, agent=mp.agent,
                                                 clear_custom_status=True)
                    del self.managed[pane_id]
            except Exception:
                log.warning("failed to clean %s on shutdown", pane_id, exc_info=True)
        for pane_id, mp in list(self._legacy_release.items()):
            try:
                if self._client.release_agent(pane_id, SOURCE, mp.agent) == "ok":
                    del self._legacy_release[pane_id]
            except Exception:
                log.warning("failed to release legacy %s on shutdown", pane_id,
                            exc_info=True)
        self._publish()
```

Delete the old `tick()`, `_progress_tick()`, `_release()`, `release_all()` (superseded by `shutdown()`), and `run()` (rebuilt in Task 9 — leave a stub `def run(self): raise NotImplementedError("rebuilt in the run-loop task")` so nothing silently calls the old loop). Update `build_daemon` to pass `client.pane_process_info` as before and construct `Daemon(client, probes, reprobe_interval_s=config.reprobe_interval_s, allow=config.allow, deny=config.deny, on_snapshot=StateStore().write, progress=progress_label if config.progress_enabled else None)` (full wiring including intervals and stream factory lands in Task 9).

- [ ] **Step 4: Run the daemon tests**

Run: `python3 -m pytest tests/test_daemon.py -q`
Expected: all pass.

- [ ] **Step 5: Run the whole suite**

Run: `python3 -m pytest tests/ -q`
Expected: all green (`cli.py` already calls `daemon.run()` argless since Task 4; the `run` stub is only hit by a real daemon launch, not by tests).

- [ ] **Step 6: Commit**

```bash
git add src/herdwatch/daemon.py tests/test_daemon.py
git commit -m "feat: registry-driven per-pane engine with metadata labels and sweeps"
```

---

### Task 6: Event dispatch — status events, self-echo guard, `pane.moved` remap

**Files:**
- Modify: `src/herdwatch/daemon.py` (add methods)
- Modify: `tests/test_daemon.py` (append)

**Interfaces:**
- Consumes: Task 5 engine.
- Produces (used by Task 9): `dispatch_event(msg: dict) -> None`, `_on_status_event(data: dict)`, `_on_pane_moved(data: dict)`, `_remap(old: str, new: str, rec: dict | None)`, module constant `LIFECYCLE_RESYNC_KINDS`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_daemon.py`:

```python
def _status_event(pane="w1:p1", status="idle", custom=None, agent="claude"):
    return {"event": "pane.agent_status_changed",
            "data": {"pane_id": pane, "workspace_id": "w1",
                     "agent_status": status, "agent": agent,
                     "custom_status": custom}}


class FakeStream:
    """Minimal stand-in for herdr_socket.EventStream bookkeeping (Task 8
    replaces it with a socketpair-backed version; the interface is a
    superset of this one)."""

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_idle_edge_event_probes_immediately():
    client = FakeClient([_agent(status="working")])
    probe = StaticProbe(Pending("review", 30, "roborev"))
    d = make_daemon(client, [probe], reprobe_interval_s=15)
    seed(d, client)
    d._last_probe["w1:p1"] = 0.0             # stale throttle from an old probe
    client.agents["w1:p1"]["agent_status"] = "idle"
    d.dispatch_event(_status_event(status="idle"))
    assert client.reports == [("w1:p1", "working", "⏳ review")]


def test_done_edge_event_labels_immediately():
    client = FakeClient([_agent(status="working")])
    probe = StaticProbe(Pending("review", 30, "roborev"))
    d = make_daemon(client, [probe], reprobe_interval_s=15)
    seed(d, client)
    d._last_probe["w1:p1"] = 0.0
    client.agents["w1:p1"]["agent_status"] = "done"
    d.dispatch_event(_status_event(status="done"))
    assert client.metadata == [("w1:p1", "⏳ review", False, 30000)]


def test_self_echo_event_is_ignored():
    client = FakeClient([_agent(status="idle")])
    probe = StaticProbe(Pending("review", 30, "roborev"))
    d = make_daemon(client, [probe], reprobe_interval_s=0)
    seed(d, client)
    d._reprobe_sweep()                       # hold asserted
    assert len(client.reports) == 1
    d.dispatch_event(_status_event(status="working", custom="⏳ review"))
    assert len(client.reports) == 1          # echo: no re-probe, no re-assert
    assert d._registry["w1:p1"]["agent_status"] == "working"


def test_progress_stop_event_hands_over_to_hold():
    client = FakeClient([_claude_agent()])
    probe = StaticProbe(None)
    d = make_daemon(client, [probe], reprobe_interval_s=15,
                    progress=lambda sid: "2/5 X")
    seed(d, client)
    d._progress_sweep()                      # progress label on
    assert d.managed["w1:p1"].kind == "progress"
    probe.result = Pending("CI: ci", 20, "ci")
    client.agents["w1:p1"]["agent_status"] = "idle"
    d.dispatch_event(_status_event(status="idle"))
    assert client.metadata[-1] == ("w1:p1", None, True, None)
    assert client.reports[-1] == ("w1:p1", "working", "⏳ CI: ci")
    assert d.managed["w1:p1"].kind == "hold"


def test_blocked_event_clears_progress_without_hold():
    client = FakeClient([_claude_agent()])
    probe = StaticProbe(Pending("CI: ci", 20, "ci"))
    d = make_daemon(client, [probe], reprobe_interval_s=0,
                    progress=lambda sid: "2/5 X")
    seed(d, client)
    d._progress_sweep()
    client.agents["w1:p1"]["agent_status"] = "blocked"
    d.dispatch_event(_status_event(status="blocked"))
    assert client.metadata[-1] == ("w1:p1", None, True, None)
    assert client.reports == []              # no hold over a blocked pane
    assert "w1:p1" not in d.managed


def test_unknown_pane_event_schedules_resync():
    client = FakeClient([])
    d = make_daemon(client)
    d.dispatch_event(_status_event(pane="w9:p9"))
    assert d._resync_due is True


def test_lifecycle_event_schedules_resync():
    d = make_daemon(FakeClient([]))
    d.dispatch_event({"event": "pane_created",
                      "data": {"type": "pane_created", "pane": {"pane_id": "w1:p1"}}})
    assert d._resync_due is True


def test_pane_moved_remaps_bookkeeping():
    client = FakeClient([_agent(status="idle")])
    probe = StaticProbe(Pending("review", 30, "roborev"))
    d = make_daemon(client, [probe], reprobe_interval_s=0)
    seed(d, client)
    d._reprobe_sweep()
    moved = _agent(pane="w2:p9", status="idle", term="term-w1:p1")
    client.set_agents([moved])
    d.dispatch_event({"event": "pane_moved",
                      "data": {"type": "pane_moved", "previous_pane_id": "w1:p1",
                               "previous_workspace_id": "w1", "previous_tab_id": "w1:t1",
                               "pane": dict(moved)}})
    assert "w1:p1" not in d.managed
    assert d.managed["w2:p9"].kind == "hold"
    assert d.managed["w2:p9"].terminal_id == "term-w1:p1"
    assert "w2:p9" in d._registry and "w1:p1" not in d._registry
    assert d._resync_due is True


def test_pane_moved_tears_down_stream_for_resubscribe():
    # the old per-pane subscription is bound to the dead pane id; only a
    # new stream (re-bootstrap) restores event coverage for the moved pane
    client = FakeClient([_agent(status="idle")])
    d = make_daemon(client)
    seed(d, client)
    stream = FakeStream()
    d._stream = stream
    moved = _agent(pane="w2:p9", status="idle", term="term-w1:p1")
    d.dispatch_event({"event": "pane_moved",
                      "data": {"type": "pane_moved", "previous_pane_id": "w1:p1",
                               "previous_workspace_id": "w1", "previous_tab_id": "w1:t1",
                               "pane": dict(moved)}})
    assert stream.closed and d._stream is None


def test_pane_moved_to_denied_pane_releases():
    client = FakeClient([_agent(status="idle")])
    probe = StaticProbe(Pending("review", 30, "roborev"))
    d = make_daemon(client, [probe], reprobe_interval_s=0, deny=["w2:p9"])
    seed(d, client)
    d._reprobe_sweep()
    moved = _agent(pane="w2:p9", status="idle", term="term-w1:p1")
    client.set_agents([moved])
    d.dispatch_event({"event": "pane_moved",
                      "data": {"type": "pane_moved", "previous_pane_id": "w1:p1",
                               "previous_workspace_id": "w1", "previous_tab_id": "w1:t1",
                               "pane": dict(moved)}})
    assert client.releases == ["w2:p9"]      # released under the NEW id
    assert "w2:p9" not in d.managed


def test_pane_moved_remaps_legacy_entry():
    client = FakeClient([], release_ok=False)
    d = make_daemon(client)
    d.adopt([{"pane_id": "w1:p1", "agent": "claude", "status": "2/5 X",
              "kind": "progress", "terminal_id": "term-w1:p1"}])
    moved = _agent(pane="w2:p9", status="working", term="term-w1:p1")
    d.dispatch_event({"event": "pane_moved",
                      "data": {"type": "pane_moved", "previous_pane_id": "w1:p1",
                               "previous_workspace_id": "w1", "previous_tab_id": "w1:t1",
                               "pane": dict(moved)}})
    assert "w2:p9" in d._legacy_release and "w1:p1" not in d._legacy_release
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_daemon.py -q -k "event or moved"`
Expected: FAIL — `AttributeError: 'Daemon' object has no attribute 'dispatch_event'`

- [ ] **Step 3: Implement dispatch**

Add to `src/herdwatch/daemon.py` (module level, near `SOURCE`):

```python
GLOBAL_SUBSCRIPTIONS = [
    {"type": "pane.created"}, {"type": "pane.closed"}, {"type": "pane.exited"},
    {"type": "pane.agent_detected"}, {"type": "pane.moved"},
    {"type": "workspace.closed"}, {"type": "tab.closed"},
]
# herdr emits dotted kinds on subscription events and snake_case kinds on
# generic lifecycle events; accept both spellings for each
LIFECYCLE_RESYNC_KINDS = {
    "pane.created", "pane_created", "pane.closed", "pane_closed",
    "pane.exited", "pane_exited", "pane.agent_detected", "pane_agent_detected",
    "workspace.closed", "workspace_closed", "tab.closed", "tab_closed",
}
```

Add methods to `Daemon`:

```python
    def dispatch_event(self, msg: dict) -> None:
        kind = msg.get("event") or ""
        data = msg.get("data") or {}
        if kind in ("pane.agent_status_changed", "pane_agent_status_changed"):
            self._on_status_event(data)
        elif kind in ("pane.moved", "pane_moved"):
            self._on_pane_moved(data)
        elif kind in LIFECYCLE_RESYNC_KINDS:
            self._resync_due = True

    def _on_status_event(self, data: dict) -> None:
        pane_id = data.get("pane_id")
        if not pane_id or not self._eligible(pane_id):
            return
        rec = self._registry.get(pane_id)
        if rec is None:
            self._resync_due = True  # unknown pane: topology drifted
            return
        status = data.get("agent_status") or "unknown"
        prev = rec.get("agent_status")
        rec["agent_status"] = status
        if data.get("agent"):
            rec["agent"] = data["agent"]
        mp = self.managed.get(pane_id)
        if mp is not None:
            expected = "done" if mp.kind == "done" else "working"
            if status == expected and (data.get("custom_status") or "") == mp.custom_status:
                return  # ack of our own report/metadata write
        if mp is not None and mp.kind == "progress" and status != "working":
            # agent stopped (or blocked): drop the label now, and hold in the
            # same dispatch when the pane is idle with pending work
            if not self._clear_metadata(pane_id, "agent stopped"):
                return
            self._last_probe.pop(pane_id, None)
        if status in ("idle", "done"):
            if prev != status:
                self._last_probe.pop(pane_id, None)  # fresh edge: probe now
            self._probe_pane(pane_id)
            self._publish()
            return
        # working/blocked/unknown edge: forget the timer so the next
        # idle/done edge probes immediately (old tick semantics)
        self._last_probe.pop(pane_id, None)

    def _on_pane_moved(self, data: dict) -> None:
        pane = data.get("pane") or {}
        old, new = data.get("previous_pane_id"), pane.get("pane_id")
        if not old or not new:
            self._resync_due = True
            return
        self._remap(old, new, pane)
        # The per-pane agent_status_changed subscription is bound to the OLD
        # public pane id and goes silent after a move (herdr matches events
        # by pane_id). A later resync cannot notice — the registry is
        # already remapped — so tear the stream down here; the run loop
        # re-bootstraps and resubscribes with the new pane set.
        if self._stream is not None:
            self._stream.close()
            self._stream = None
        self._resync_due = True

    def _remap(self, old: str, new: str, rec: dict | None) -> None:
        """Follow herdr's pane-id change: the assertion lives on inside herdr,
        so bookkeeping must follow it -- releasing the old id would just
        `not_found` while a permanent `working ⏳` survived under the new one."""
        if rec:
            self._registry[new] = rec
            self._remember_record(rec)
        self._registry.pop(old, None)
        for d in (self._last_probe, self._session_cache, self._meta_asserted_at):
            if old in d:
                d[new] = d.pop(old)
        if old in self.managed:
            self.managed[new] = self.managed.pop(old)
        if old in self._adopted:
            self._adopted.discard(old)
            self._adopted.add(new)
        if old in self._legacy_release:
            self._legacy_release[new] = self._legacy_release.pop(old)
        mp = self.managed.get(new)
        if mp is not None and not self._eligible(new):
            if mp.kind == "hold":
                self._release_hold(new, "moved to ineligible pane")
            else:
                self._clear_metadata(new, "moved to ineligible pane")
        self._publish()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_daemon.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/herdwatch/daemon.py tests/test_daemon.py
git commit -m "feat: event dispatch with self-echo guard and pane.moved remap"
```

---

### Task 7: Resync — snapshots are truth (terminal_id reconciliation, vanished panes, legacy backfill)

**Files:**
- Modify: `src/herdwatch/daemon.py` (add `_resync`)
- Modify: `tests/test_daemon.py` (append)

**Interfaces:**
- Consumes: Tasks 5–6 (`_remap`, engine state, client `session_snapshot`).
- Produces (used by Task 9): `_resync() -> None`; sets `_stream = None` when the pane set changed (Task 9's loop re-bootstraps); never raises.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_daemon.py`:

```python
from herdwatch.herdr_socket import HerdrApiError, HerdrUnavailable


def test_resync_releases_vanished_pane_and_drops_bookkeeping():
    client = FakeClient([_agent(status="idle")])
    d = make_daemon(client, [StaticProbe(Pending("review", 30, "roborev"))],
                    reprobe_interval_s=0)
    seed(d, client)
    d._reprobe_sweep()
    client.set_agents([])
    client._release_ok = False          # herdr can't release a gone pane
    d._resync()
    assert client.releases == ["w1:p1"]  # best-effort attempted
    assert d.managed == {}               # dropped regardless of outcome
    assert d._registry == {}


def test_resync_remaps_moved_pane_by_terminal_id():
    # the pane.moved event was missed; the snapshot still reconciles it
    client = FakeClient([_agent(status="idle")])
    d = make_daemon(client, [StaticProbe(Pending("review", 30, "roborev"))],
                    reprobe_interval_s=0)
    seed(d, client)
    d._reprobe_sweep()
    client.set_agents([_agent(pane="w2:p9", status="idle", term="term-w1:p1")])
    d._resync()
    assert d.managed.get("w2:p9") is not None
    assert d.managed["w2:p9"].kind == "hold"
    assert client.releases == []         # nothing dropped, nothing released


def test_resync_keeps_state_when_herdr_down():
    client = FakeClient([_agent(status="idle")])
    d = make_daemon(client, [StaticProbe(Pending("review", 30, "roborev"))],
                    reprobe_interval_s=0)
    seed(d, client)
    d._reprobe_sweep()
    client.snapshot_error = HerdrUnavailable("down")
    d._resync()
    assert "w1:p1" in d.managed          # a blip must not drop assertions
    assert "w1:p1" in d._registry


def test_resync_logs_old_server_and_keeps_state(caplog):
    client = FakeClient([_agent(status="idle")])
    d = make_daemon(client, [StaticProbe(Pending("review", 30, "roborev"))],
                    reprobe_interval_s=0)
    seed(d, client)
    d._reprobe_sweep()
    client.snapshot_error = HerdrApiError("unknown_method", "session.snapshot")
    with caplog.at_level("ERROR"):
        d._resync()
    assert any("0.7.2" in r.message for r in caplog.records)
    assert "w1:p1" in d.managed


def test_resync_tears_down_stream_on_pane_set_change():
    client = FakeClient([_agent(status="idle")])
    d = make_daemon(client)
    seed(d, client)
    d._stream = FakeStream()
    client.set_agents([_agent(), _agent(pane="w2:p1")])
    d._resync()
    assert d._stream is None             # run loop re-bootstraps/resubscribes


def test_resync_keeps_stream_when_pane_set_unchanged():
    client = FakeClient([_agent(status="idle")])
    d = make_daemon(client)
    seed(d, client)
    stream = FakeStream()
    d._stream = stream
    d._resync()
    assert d._stream is stream


def test_resync_backfills_legacy_terminal_id():
    client = FakeClient([_agent(status="idle")], release_ok=False)
    d = make_daemon(client)
    d.adopt([{"pane_id": "w1:p1", "agent": "claude", "status": "2/5 X",
              "kind": "progress"}])       # pre-migration row: no terminal_id
    d._resync()
    assert d._legacy_release["w1:p1"].terminal_id == "term-w1:p1"


def test_resync_remaps_legacy_row_by_terminal_id():
    client = FakeClient([_agent(status="idle")], release_ok=False)
    d = make_daemon(client)
    d.adopt([{"pane_id": "w1:p1", "agent": "claude", "status": "2/5 X",
              "kind": "progress", "terminal_id": "term-w1:p1"}])
    client.set_agents([_agent(pane="w2:p9", status="idle", term="term-w1:p1")])
    d._resync()
    assert "w2:p9" in d._legacy_release and "w1:p1" not in d._legacy_release


def test_resync_salvages_legacy_row_by_unique_label_match():
    # moved between old daemon's crash and our first snapshot: no terminal_id;
    # exactly one pane carries our stored label -> remap to it
    moved = _agent(pane="w2:p9", status="working")
    moved["custom_status"] = "2/5 X"
    client = FakeClient([moved], release_ok=False)
    d = make_daemon(client)
    d.adopt([{"pane_id": "w1:p1", "agent": "claude", "status": "2/5 X",
              "kind": "progress"}])
    d._resync()
    assert "w2:p9" in d._legacy_release and "w1:p1" not in d._legacy_release


def test_resync_drops_unmatchable_legacy_row():
    client = FakeClient([_agent(pane="w3:p1", status="idle")])
    d = make_daemon(client)
    d.adopt([{"pane_id": "w1:p1", "agent": "claude", "status": "2/5 X",
              "kind": "progress"}])
    d._resync()
    assert d._legacy_release == {}       # no terminal, no label match -> gone


def test_resync_drops_stale_session_cache():
    client = FakeClient([_claude_agent(status="idle")])
    d = make_daemon(client, progress=lambda sid: "2/5 x")
    seed(d, client)
    assert d._session_cache.get("w1:p1")
    client.set_agents([])
    d._resync()
    assert "w1:p1" not in d._session_cache
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_daemon.py -q -k resync`
Expected: FAIL — `AttributeError: '_resync'`

- [ ] **Step 3: Implement `_resync`**

Add to `Daemon` (imports at top of daemon.py: `from .herdr_socket import HerdrApiError, HerdrUnavailable`):

```python
    def _backfill_legacy_terminals(self, records: dict[str, dict]) -> None:
        """Legacy rows predate terminal_id persistence; grab it from the
        first snapshot that still shows the pane, so later moves remap."""
        for pane_id, mp in self._legacy_release.items():
            if not mp.terminal_id and pane_id in records:
                mp.terminal_id = records[pane_id].get("terminal_id") or ""

    def _reconcile_books(self, records: dict[str, dict],
                         by_terminal: dict[str, str]) -> None:
        """Move reconciliation + vanish handling for managed and legacy rows
        against snapshot truth. Shared by _resync and bootstrap (so adopted
        rows are reconciled BEFORE the first sweep can release stale ids)."""
        for book in (self.managed, self._legacy_release):
            for pane_id in list(book):
                if pane_id in records:
                    continue
                mp = book[pane_id]
                new_id = by_terminal.get(mp.terminal_id) if mp.terminal_id else None
                if (new_id is None and not mp.terminal_id
                        and book is self._legacy_release):
                    # label-match salvage (legacy rows only): a move before
                    # our first snapshot left no terminal_id; a unique
                    # (agent, custom_status) match among untracked panes is
                    # safe to adopt -- a mismatched target either has no
                    # herdwatch assertion (release is a no-op) or carries a
                    # herdwatch orphan (releasing it is also correct cleanup)
                    matches = [pid for pid, a in records.items()
                               if pid not in self.managed
                               and pid not in self._legacy_release
                               and (a.get("agent") or "") == mp.agent
                               and (a.get("custom_status") or "") == mp.custom_status]
                    if len(matches) == 1:
                        new_id = matches[0]
                if new_id and new_id not in self.managed \
                        and new_id not in self._legacy_release:
                    self._remap(pane_id, new_id, records.get(new_id))
                    continue
                del book[pane_id]
                if book is self.managed:
                    self._adopted.discard(pane_id)
                    # best-effort cleanup; the pane is gone, drop regardless
                    if mp.kind == "hold":
                        self._client.release_agent(pane_id, SOURCE, mp.agent)
                    else:
                        self._client.report_metadata(pane_id, SOURCE, agent=mp.agent,
                                                     clear_custom_status=True)
                    log.info("dropped vanished pane %s (%s)", pane_id, mp.kind)

    def _resync(self) -> None:
        """Snapshot is truth: reconcile managed/legacy/registry against it.
        Never raises; herdr being down keeps all state for a later retry."""
        self._resync_due = False
        try:
            snap = self._client.session_snapshot()
        except HerdrApiError as exc:
            log.error("herdwatch requires herdr >= 0.7.2 with session.snapshot "
                      "(server said: %s)", exc)
            return
        except HerdrUnavailable as exc:
            log.warning("resync skipped, herdr unreachable: %s", exc)
            return
        records = {a["pane_id"]: a for a in snap.get("agents", [])
                   if a.get("pane_id")}
        # captured BEFORE reconciliation: _remap mutates the registry, and a
        # post-remap comparison would miss the pane-id change entirely
        before_ids = set(self._registry)
        by_terminal = {a["terminal_id"]: pid for pid, a in records.items()
                       if a.get("terminal_id")}
        self._backfill_legacy_terminals(records)
        self._reconcile_books(records, by_terminal)
        pane_set_changed = set(records) != before_ids
        self._registry = records
        for rec in records.values():
            self._remember_record(rec)
        for d in (self._last_probe, self._session_cache, self._meta_asserted_at):
            for pane_id in list(d):
                if pane_id not in records:
                    d.pop(pane_id, None)
        if pane_set_changed and self._stream is not None:
            self._stream.close()
            self._stream = None  # run loop re-bootstraps with the new pane set
        self._publish()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_daemon.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/herdwatch/daemon.py tests/test_daemon.py
git commit -m "feat: snapshot resync with terminal_id move reconciliation"
```

---

### Task 8: Bootstrap and the run loop

**Files:**
- Modify: `src/herdwatch/daemon.py` (add `bootstrap`, replace `run` stub)
- Modify: `tests/test_daemon.py` (append)

**Interfaces:**
- Consumes: Tasks 2, 5–7. `stream_factory(subscriptions: list[dict]) -> EventStream-like` (fileno/read_events/closed/close) — injected; Task 9 wires the real one.
- Produces: `bootstrap() -> bool`, `run(sleep=time.sleep) -> None` (blocks forever; SIGTERM/atexit call `shutdown()`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_daemon.py` (extend `FakeStream` in place — replace the Task 6 definition with this richer one; it is a superset, all earlier call sites keep working):

```python
import socket as _socket


class FakeStream:
    """Test double backed by a real socketpair so selectors can poll it."""

    def __init__(self, subscriptions=None):
        self.subscriptions = subscriptions or []
        self.closed = False
        self._r, self._w = _socket.socketpair()
        self._r.setblocking(False)
        self._pending = []

    def feed(self, event):
        self._pending.append(event)
        self._w.send(b"x")                  # wake the selector

    def fileno(self):
        return self._r.fileno()

    def read_events(self):
        try:
            while True:
                if not self._r.recv(4096):
                    self.closed = True
                    break
        except BlockingIOError:
            pass
        out, self._pending = self._pending, []
        return out

    def close(self):
        self.closed = True
        for s in (self._r, self._w):
            try:
                s.close()
            except OSError:
                pass


def test_bootstrap_subscribes_then_snapshots():
    client = FakeClient([_agent(status="idle")])
    made = []

    def factory(subs):
        made.append(subs)
        return FakeStream(subs)

    d = make_daemon(client, stream_factory=factory)
    assert d.bootstrap() is True
    assert d._stream is not None
    assert "w1:p1" in d._registry
    per_pane = [s for s in made[0] if s.get("type") == "pane.agent_status_changed"]
    assert per_pane == [{"type": "pane.agent_status_changed", "pane_id": "w1:p1"}]
    globals_ = {s["type"] for s in made[0] if "pane_id" not in s}
    assert {"pane.created", "pane.closed", "pane.exited", "pane.agent_detected",
            "pane.moved", "workspace.closed", "tab.closed"} <= globals_


def test_bootstrap_retries_when_pane_set_drifts():
    # snapshot A sees one pane, snapshot B two -> resubscribe with B's set
    client = FakeClient([])
    calls = {"n": 0}
    made = []

    def drifting_snapshot():
        calls["n"] += 1
        if calls["n"] == 1:                  # A: stale single-pane view
            return {"agents": [_agent()]}
        return {"agents": [_agent(), _agent(pane="w2:p1")]}

    client.session_snapshot = drifting_snapshot

    def factory(subs):
        made.append(subs)
        return FakeStream(subs)

    d = make_daemon(client, stream_factory=factory)
    assert d.bootstrap() is True
    assert set(d._registry) == {"w1:p1", "w2:p1"}
    per_pane = [s["pane_id"] for s in made[-1] if "pane_id" in s]
    assert per_pane == ["w1:p1", "w2:p1"]    # resubscribed with the full set


def test_bootstrap_fails_when_unavailable():
    client = FakeClient([])
    client.snapshot_error = HerdrUnavailable("down")
    d = make_daemon(client, stream_factory=lambda subs: FakeStream(subs))
    assert d.bootstrap() is False


def test_bootstrap_old_server_logs_and_fails(caplog):
    client = FakeClient([])
    client.snapshot_error = HerdrApiError("unknown_method", "session.snapshot")
    d = make_daemon(client, stream_factory=lambda subs: FakeStream(subs))
    with caplog.at_level("ERROR"):
        assert d.bootstrap() is False
    assert any("0.7.2" in r.message for r in caplog.records)


def test_bootstrap_reconciles_adopted_rows_before_first_sweep():
    # a hold pane moved while the daemon was down: bootstrap must remap it
    # via terminal_id BEFORE any sweep can release the obsolete id
    client = FakeClient([_agent(pane="w2:p9", status="idle", term="t-old")])
    d = make_daemon(client, stream_factory=lambda subs: FakeStream(subs))
    d.adopt([{"pane_id": "w1:p1", "agent": "claude", "status": "⏳ review",
              "kind": "hold", "terminal_id": "t-old"}])
    assert d.bootstrap() is True
    assert "w2:p9" in d.managed and "w1:p1" not in d.managed
    assert client.releases == []             # nothing released blindly


def test_bootstrap_logs_each_failure_reason_once(caplog):
    client = FakeClient([])
    client.snapshot_error = HerdrUnavailable("down")
    d = make_daemon(client, stream_factory=lambda subs: FakeStream(subs))
    with caplog.at_level("WARNING"):
        assert d.bootstrap() is False
        assert d.bootstrap() is False        # same reason again
    assert sum("down" in r.message for r in caplog.records) == 1


def test_bootstrap_subscribe_rejection_retries_with_fresh_set():
    client = FakeClient([_agent(status="idle")])
    attempts = {"n": 0}

    def factory(subs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise HerdrApiError("not_found", "pane closed during setup")
        return FakeStream(subs)

    d = make_daemon(client, stream_factory=factory)
    assert d.bootstrap() is True
    assert attempts["n"] == 2


class Stop(Exception):
    pass


def _stop_sleep(_):
    raise Stop()


def test_run_loop_processes_idle_event_from_stream():
    # The snapshot shows the pane WORKING, so neither bootstrap nor any
    # sweep can assert the hold -- it can only come from the pushed idle
    # event flowing through dispatch_event -> _probe_pane. Not vacuous.
    # NOTE: sel.select() sleeps REAL seconds while the clock is fake, so
    # every interval must be tiny or the test wall-blocks on the timeout.
    client = FakeClient([_agent(status="working")])
    probe = StaticProbe(Pending("review", 30, "roborev"))
    stream = FakeStream()
    d = make_daemon(client, [probe], stream_factory=lambda subs: stream,
                    reprobe_interval_s=0.001, resync_interval_s=999.0,
                    progress_interval_s=999.0)
    ticks = {"n": 0}

    def clock():
        ticks["n"] += 1
        if ticks["n"] == 50:                  # mid-loop: the agent finishes
            client.agents["w1:p1"]["agent_status"] = "idle"
            stream.feed(_status_event(status="idle"))
        if ticks["n"] > 400:                  # end the loop via the handler
            raise Stop()
        return float(ticks["n"]) * 0.001

    d._clock = clock
    try:
        d.run(sleep=_stop_sleep)              # handler's sleep raises Stop
    except Stop:
        pass
    assert ("w1:p1", "working", "⏳ review") in client.reports


def test_run_rebootstraps_after_stream_close():
    client = FakeClient([_agent(status="idle")])
    streams = []

    def factory(subs):
        s = FakeStream(subs)
        streams.append(s)
        return s

    d = make_daemon(client, [StaticProbe(None)], stream_factory=factory,
                    reprobe_interval_s=0.001, resync_interval_s=0.001,
                    progress_interval_s=0.001)
    ticks = {"n": 0}

    def clock():
        ticks["n"] += 1
        if ticks["n"] == 20 and streams:
            streams[0].feed({"event": "noop", "data": {}})  # wake, then EOF
            streams[0]._w.close()
        if ticks["n"] > 400:
            raise Stop()
        return float(ticks["n"]) * 0.001

    d._clock = clock
    try:
        # backoff sleep only runs if bootstrap fails (it should not here);
        # the loop is ended by clock() raising Stop, which the handler
        # forwards into sleep -> Stop propagates
        d.run(sleep=_stop_sleep)
    except Stop:
        pass
    assert len(streams) >= 2                  # reconnected with a new stream
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_daemon.py -q -k "bootstrap or run_"`
Expected: FAIL — `bootstrap` missing / `run` raises NotImplementedError.

- [ ] **Step 3: Implement bootstrap + run**

Add to `Daemon` (imports at top: `import selectors`):

```python
    def _subscriptions_for(self, pane_ids: list[str]) -> list[dict]:
        subs = [dict(s) for s in GLOBAL_SUBSCRIPTIONS]
        subs += [{"type": "pane.agent_status_changed", "pane_id": p}
                 for p in sorted(pane_ids)]
        return subs

    def bootstrap(self) -> bool:
        """snapshot A -> subscribe(A) -> snapshot B -> registry := B.
        Pre-subscribe transitions are absorbed into herdr's per-pane baseline
        and never emitted, so the authoritative snapshot must come AFTER the
        subscribe ack; loop while the pane set drifts underneath us."""
        if self._stream_factory is None:
            return False
        for _ in range(5):
            try:
                snap_a = self._client.session_snapshot()
            except HerdrApiError as exc:
                self._log_boot_failure(
                    f"herdwatch requires herdr >= 0.7.2 with session.snapshot "
                    f"(server said: {exc})", error=True)
                return False
            except HerdrUnavailable as exc:
                self._log_boot_failure(f"herdr unreachable: {exc}")
                return False
            pane_ids = [a["pane_id"] for a in snap_a.get("agents", [])
                        if a.get("pane_id")]
            try:
                stream = self._stream_factory(self._subscriptions_for(pane_ids))
            except HerdrApiError as exc:
                log.info("subscribe rejected (%s); retrying with a fresh "
                         "snapshot", exc)
                continue  # e.g. a pane closed between snapshot and subscribe
            except HerdrUnavailable as exc:
                self._log_boot_failure(f"subscribe failed: {exc}")
                return False
            try:
                snap_b = self._client.session_snapshot()
            except (HerdrApiError, HerdrUnavailable) as exc:
                log.warning("bootstrap: post-subscribe snapshot failed: %s", exc)
                stream.close()
                return False
            records = {a["pane_id"]: a for a in snap_b.get("agents", [])
                       if a.get("pane_id")}
            if set(records) != set(pane_ids):
                stream.close()
                continue  # pane set drifted; resubscribe against it
            self._stream = stream
            self._registry = records
            for rec in records.values():
                self._remember_record(rec)
            # reconcile adopted/legacy rows against fresh truth BEFORE the
            # first sweep -- otherwise the sweep releases stale pane ids
            # that may in fact have been moved while we were down
            by_terminal = {a["terminal_id"]: pid for pid, a in records.items()
                           if a.get("terminal_id")}
            self._backfill_legacy_terminals(records)
            self._reconcile_books(records, by_terminal)
            for d in (self._last_probe, self._session_cache,
                      self._meta_asserted_at):
                for pane_id in list(d):
                    if pane_id not in records:
                        d.pop(pane_id, None)
            self._last_boot_error = None
            log.info("connected to herdr (%d panes)", len(records))
            return True
        self._log_boot_failure("pane set kept drifting; will retry")
        return False

    def _log_boot_failure(self, reason: str, *, error: bool = False) -> None:
        """Log once per DISTINCT failure reason: an old server must be
        visible, a herdr that stays down through 30s-backoff retries must
        not spam the log (herdeck connector pattern)."""
        if reason == getattr(self, "_last_boot_error", None):
            return
        self._last_boot_error = reason
        (log.error if error else log.warning)("bootstrap: %s", reason)

    def run(self, sleep: Callable[[float], None] = time.sleep) -> None:
        atexit.register(self.shutdown)

        def _handle_term(signum, frame):
            self.shutdown()
            raise SystemExit(0)

        try:
            signal.signal(signal.SIGTERM, _handle_term)
        except (ValueError, OSError):
            pass  # not main thread; atexit still covers shutdown

        sel = selectors.DefaultSelector()
        backoff = self._backoff_base
        registered = None
        next_reprobe = next_progress = self._clock()
        next_resync = self._clock() + self._resync_interval
        while True:
            try:
                if self._stream is None:
                    if registered is not None:
                        try:
                            sel.unregister(registered)
                        except (KeyError, ValueError):
                            pass
                        registered = None
                    if self.bootstrap():
                        backoff = self._backoff_base
                        sel.register(self._stream, selectors.EVENT_READ)
                        registered = self._stream
                        # a reconnect may have missed edges: reconcile and
                        # probe everything eligible right now
                        self._last_probe.clear()
                        self._resync_due = False
                        self._reprobe_sweep()
                        self._progress_sweep()
                        now = self._clock()
                        next_reprobe = now + self._reprobe
                        next_progress = now + self._progress_interval
                        next_resync = now + self._resync_interval
                    else:
                        sleep(backoff)
                        backoff = min(backoff * 2, self._backoff_max)
                    continue
                now = self._clock()
                timeout = max(0.0, min(next_reprobe, next_progress, next_resync) - now)
                ready = sel.select(timeout)
                if ready:
                    stream = self._stream
                    for msg in stream.read_events():
                        self.dispatch_event(msg)
                    # dispatch may itself drop the stream (pane.moved needs a
                    # resubscribe) -- compare identity, not just .closed
                    if self._stream is not stream or stream.closed:
                        try:
                            sel.unregister(stream)
                        except (KeyError, ValueError):
                            pass
                        registered = None
                        stream.close()
                        if self._stream is stream:
                            self._stream = None
                        continue
                if self._resync_due:
                    self._resync()
                    if self._stream is None:
                        continue  # pane set changed: re-bootstrap now
                now = self._clock()
                if now >= next_reprobe:
                    self._reprobe_sweep()
                    next_reprobe = now + self._reprobe
                if now >= next_progress:
                    self._progress_sweep()
                    next_progress = now + self._progress_interval
                if now >= next_resync:
                    self._resync()
                    next_resync = now + self._resync_interval
            except SystemExit:
                raise
            except Exception:
                log.exception("daemon loop iteration failed; continuing")
                sleep(1.0)
```

(Also add `import atexit`, `import signal`, `import selectors` to daemon.py imports if not present; `atexit`/`signal` exist from the old `run`.)

- [ ] **Step 4: Run the full suite**

Run: `python3 -m pytest tests/ -q`
Expected: all pass (cli test may still carry the Task 5 shim; fine).

- [ ] **Step 5: Commit**

```bash
git add src/herdwatch/daemon.py tests/test_daemon.py
git commit -m "feat: event-driven run loop with bootstrap and reconnect backoff"
```

---

### Task 9: Wire `build_daemon`, CLI, and the plugin manifest

**Files:**
- Modify: `src/herdwatch/daemon.py` (`build_daemon`)
- Modify: `src/herdwatch/cli.py` (`_cmd_daemon`, `_cmd_status`)
- Modify: `tests/test_daemon.py`, `tests/test_cli.py`
- Modify: `herdr-plugin.toml`

**Interfaces:**
- Consumes: Tasks 4, 5, 8 (`Config` fields, `Daemon` constructor, `run()`).
- Produces: `build_daemon(config, client=None) -> Daemon` wiring all intervals and the real `EventStream` factory.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_daemon.py`:

```python
def test_build_daemon_constructs_with_new_wiring():
    from herdwatch.config import Config
    from herdwatch.daemon import build_daemon

    class FakeC:
        def pane_process_info(self, pid):
            return {}

    cfg = Config(resync_interval_s=90.0, progress_interval_s=2.0)
    d = build_daemon(cfg, client=FakeC())
    assert len(d._probes) == 3           # roborev, ci, marker on by default
    assert d._resync_interval == 90.0
    assert d._progress_interval == 2.0
    assert d._stream_factory is not None
```

In `tests/test_cli.py`, update the daemon-invocation test (find the test that stubs `build_daemon`/`run`) so the fake daemon's `run` takes no positional interval: `def run(self): ...` — and assert it was called. Update the status test expectations to accept the `labeling`/`releasing` verbs:

```python
def test_status_prints_kind_verbs(monkeypatch, capsys):
    from herdwatch import cli as cli_mod

    class Snap:
        pid = 1
        updated_at = 0.0
        panes = [
            {"pane_id": "w1:p1", "agent": "claude", "status": "⏳ CI", "kind": "hold"},
            {"pane_id": "w2:p1", "agent": "claude", "status": "3/7 X", "kind": "progress"},
            {"pane_id": "w3:p1", "agent": "claude", "status": "⏳ rev", "kind": "done"},
        ]

    class Store:
        def read(self):
            return Snap()

    monkeypatch.setattr(cli_mod, "_state_store", lambda: Store())
    monkeypatch.setattr(cli_mod._state, "pid_alive", lambda pid: True)
    monkeypatch.setattr(cli_mod, "_store", lambda: type("S", (), {"all": lambda self: []})())
    cli_mod.main(["status"])
    out = capsys.readouterr().out
    assert "holding w1:p1" in out
    assert "working w2:p1" in out
    assert "labeling w3:p1" in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_daemon.py::test_build_daemon_constructs_with_new_wiring tests/test_cli.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement**

`build_daemon` in `src/herdwatch/daemon.py` (add `from . import herdr_socket` import):

```python
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
                                  min_age_s=config.bgjobs_min_age_s,
                                  extra_ignore=config.bgjobs_ignore))
    return Daemon(client, probes,
                  reprobe_interval_s=config.reprobe_interval_s,
                  resync_interval_s=config.resync_interval_s,
                  progress_interval_s=config.progress_interval_s,
                  allow=config.allow, deny=config.deny,
                  on_snapshot=StateStore().write,
                  progress=progress_label if config.progress_enabled else None,
                  stream_factory=lambda subs: herdr_socket.EventStream(subs))
```

`cli.py` `_cmd_daemon` already calls `daemon.run()` (Task 4). `_cmd_status` verb mapping (legacy rows publish their original kind, so no separate verb exists for them):

```python
    _VERBS = {"hold": "holding", "progress": "working", "done": "labeling"}
    ...
    for p in snap.panes:
        verb = _VERBS.get(p.get("kind", "hold"), "holding")
        print(f"{verb} {p['pane_id']}  {p['status']}  ({p['agent']})")
```

(Keep `_VERBS` as a module-level constant in `cli.py`.)

`herdr-plugin.toml`: change `min_herdr_version = "0.7.0"` to `min_herdr_version = "0.7.2"`.

- [ ] **Step 4: Run the full suite**

Run: `python3 -m pytest tests/ -q`
Expected: all pass. Also run `grep -rn "subprocess" src/herdwatch/ --include="*.py" | grep -v service.py | grep -v doctor.py | grep -v gitctx.py | grep -v markers.py | grep -v probes/` — expected: no output. (The daemon's herdr path spawns nothing; the exempt modules legitimately shell out: service→launchctl, doctor→`herdr status` CLI presence check + ps, gitctx→git, markers→`--until` shell commands, probes→gh/roborev.)

- [ ] **Step 5: Commit**

```bash
git add src/herdwatch/daemon.py src/herdwatch/cli.py tests/test_daemon.py tests/test_cli.py herdr-plugin.toml
git commit -m "feat: wire event-driven daemon into build_daemon, cli, and plugin manifest"
```

---

### Task 10: Doctor — socket reachability and protocol checks

**Files:**
- Modify: `src/herdwatch/doctor.py`
- Modify: `tests/test_doctor.py`

**Interfaces:**
- Consumes: Task 1 (`herdr_socket.request`, errors).
- Produces: `run_checks(..., snapshot: Callable[[], dict])` — a **required** keyword (only `diagnose()` passes the live `_snapshot`, so unit tests can never hit the real socket) — gains two required checks: "herdr socket reachable" and "herdr >= 0.7.2 (session.snapshot)".

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_doctor.py` (match its existing style of calling `run_checks` with fakes):

```python
from herdwatch.herdr_socket import HerdrApiError, HerdrUnavailable


def _base_kwargs():
    return dict(which=lambda cmd: True,
                run=lambda args: (0, "running"),
                list_procs=lambda: [],
                plist_path="/nonexistent")


def _by_name(checks):
    return {c.name: c for c in checks}


def test_doctor_socket_ok():
    checks = run_checks(**_base_kwargs(), snapshot=lambda: {"agents": []})
    by = _by_name(checks)
    assert by["herdr socket reachable"].ok
    assert by["herdr >= 0.7.2 (session.snapshot)"].ok


def test_doctor_socket_unreachable():
    def snap():
        raise HerdrUnavailable("no socket")
    by = _by_name(run_checks(**_base_kwargs(), snapshot=snap))
    assert not by["herdr socket reachable"].ok
    assert by["herdr socket reachable"].required
    assert not by["herdr >= 0.7.2 (session.snapshot)"].ok


def test_doctor_old_server():
    def snap():
        raise HerdrApiError("unknown_method", "session.snapshot")
    by = _by_name(run_checks(**_base_kwargs(), snapshot=snap))
    assert by["herdr socket reachable"].ok          # it answered, just old
    assert not by["herdr >= 0.7.2 (session.snapshot)"].ok
    assert "0.7.2" in by["herdr >= 0.7.2 (session.snapshot)"].detail
```

(Ensure `run_checks` is imported in the test module header; it already is in the existing tests.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_doctor.py -q`
Expected: FAIL — unexpected keyword `snapshot`.

- [ ] **Step 3: Implement**

In `src/herdwatch/doctor.py`: add imports `from . import herdr_socket` and `from .herdr_socket import HerdrApiError, HerdrUnavailable`. Add a module-level default:

```python
def _snapshot() -> dict:
    return herdr_socket.request("session.snapshot", {})
```

Extend the signature to `run_checks(*, which, run, list_procs, plist_path, snapshot)` — **required keyword, no default**, so no test can accidentally hit the live socket. Update **every** existing `run_checks(...)` call in `tests/test_doctor.py` to also pass `snapshot=lambda: {"agents": []}` — run `grep -n "run_checks(" tests/test_doctor.py` first and cover all hits (currently three tests: `test_all_required_pass`, `test_missing_herdr_fails_required`, `test_optional_missing_is_warn_not_fail`; names may drift, the grep is authoritative). After the "herdr server running" check insert:

```python
    reachable = False
    modern = False
    detail_sock = ""
    detail_ver = ""
    try:
        snapshot()
        reachable = True
        modern = True
    except HerdrApiError as exc:
        reachable = True
        detail_ver = (f"server rejected session.snapshot ({exc.code}); "
                      "herdwatch requires herdr >= 0.7.2 — run `herdr update`")
    except HerdrUnavailable as exc:
        detail_sock = f"cannot reach {herdr_socket.resolve_socket_path()}: {exc}"
        detail_ver = "unreachable"
    checks.append(Check("herdr socket reachable", reachable, True, detail_sock))
    checks.append(Check("herdr >= 0.7.2 (session.snapshot)", modern, True, detail_ver))
```

And update `diagnose()` to pass `snapshot=_snapshot`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_doctor.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/herdwatch/doctor.py tests/test_doctor.py
git commit -m "feat: doctor checks for herdr socket and protocol floor"
```

---

### Task 11: README, spec status, full-suite gate

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-07-11-socket-api-migration-design.md` (status line)

- [ ] **Step 1: Update README**

In `README.md`:
1. In "How it works", replace the sentence describing polling (`polls \`herdr agent list\`` …) with: it bootstraps from `session.snapshot`, subscribes to herdr's socket events (`pane.agent_status_changed` per pane plus lifecycle events), reacts to idle/done edges within ~100 ms, and re-verifies against a fresh snapshot every `resync_interval_s` (60 s default) so correctness never depends on seeing every event. State the requirement: **herdr ≥ 0.7.2**.
2. Replace the done-pane paragraph ("It deliberately does **not** hold a pane herdr reports as `done`…") — the new behavior: a `done` pane with pending work keeps its semantic `done` state and gets a **display-only** `⏳` label via `pane.report_metadata` (shown as `done · ⏳ CI`); when viewed (`done` → `idle`) the label is cleared and a normal `working ⏳` hold takes over if work is still pending.
3. Update "Task progress in the sidebar": progress labels are now display-only metadata — herdr keeps detecting the real lifecycle state underneath, so the label never masks `blocked`/`idle`.
4. In "v1 limitations": delete the "Poll-based" and "No ⏳ on unseen (`done`) panes" bullets; reword the "No step aside" bullet to apply only to the ⏳ hold (progress labels no longer mask state); update the `status` bullet's "one `poll_interval_s`" to "one sweep interval"; add a bullet: "Requires herdr ≥ 0.7.2 (socket `session.snapshot` + event subscriptions); there is no CLI-polling fallback — `herdwatch doctor` checks this."
5. Update the config example: replace `poll_interval_s = 4` with `resync_interval_s = 60`, and add `interval_s = 4` under `[progress]`.

- [ ] **Step 2: Flip the spec status**

In the spec header change `**Status:** approved` to `**Status:** implemented`.

- [ ] **Step 3: Full suite + no-subprocess gate**

Run: `python3 -m pytest tests/ -q` — expected: all pass.
Run: `grep -rn "agent_explain\|agent list" src/ README.md` — expected: no hits (the polling-era API is gone everywhere).
Run: `grep -rn "poll_interval" src/ README.md` — expected: hits ONLY in `src/herdwatch/config.py` (the deprecation branch is intentional and must stay).

- [ ] **Step 4: Commit**

```bash
git add README.md docs/superpowers/specs/2026-07-11-socket-api-migration-design.md
git commit -m "docs: document event-driven daemon and metadata labels"
```

---

### Task 12: Live verification against the running herdr

No new code — an end-to-end check on this machine (herdr 0.7.3 runs locally with real panes). Use the `verify` skill mindset: drive the real flow, observe behavior.

- [ ] **Step 1:** Install the branch build into the venv the launchd service uses (or run `python3 -m herdwatch.cli daemon` manually in a scratch terminal with `HERDR_SOCKET_PATH` unset). Stop the old daemon first (`herdwatch install-service --uninstall` or `launchctl unload`).
- [ ] **Step 2:** With the daemon running, add a manual marker to this session's pane: `herdwatch add "live test" --pane <idle pane id> --ttl 60`. Within ~15 s the pane must show `working · ⏳ live test` (`herdr agent get <pane>` shows our `custom_status`, `agent_status` `"working"`). When the TTL lapses, the pane must return to `idle` within one reprobe interval.
- [ ] **Step 3:** Idle-edge latency: on a working pane, wait for the agent to finish; the ⏳ hold (with a pending marker) must appear within ~1 s of the idle edge, not ~4 s.
- [ ] **Step 4:** Done-pane label: arrange a pane to reach `done` (finish an agent turn unfocused) with a marker pending; `herdr agent get` must show `agent_status: "done"` with our `custom_status`, and the sidebar shows `done · ⏳ …`. Focus the pane; the label must transition to a `working ⏳` hold.
- [ ] **Step 5:** Restart resilience: `kill -9` the daemon while it holds a pane; restart it; the pane must be re-adopted (still ⏳, no orphan after work clears). Then `herdr server stop` + restart herdr while the daemon runs; the daemon must reconnect and re-assert within the backoff window.
- [ ] **Step 6:** Record results (pane ids, observed latencies, anomalies) in the PR/merge description. Reinstall the launchd service if it was unloaded.

---

## Final integration

- [ ] Merge `feat/socket-api-migration` into `main` with a merge commit (`git merge --no-ff`) or rebase — never squash.
- [ ] After the merge commit, check `roborev show HEAD` once more.
