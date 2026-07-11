# tests/test_herdr_socket.py
import json
import os
import socket
import threading
import time

import pytest

from herdwatch.herdr_socket import (
    EventStream,
    HerdrApiError,
    HerdrUnavailable,
    request,
    resolve_socket_path,
)


class FakeServer:
    """Minimal herdr-like ndjson unix-socket server: one request per
    connection; `events.subscribe` connections stay open for push()."""

    def __init__(self, sock_dir, responses=None, subscribe_error=None):
        self.path = str(sock_dir / "herdr.sock")
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


@pytest.fixture
def server(sock_dir):
    srv = FakeServer(sock_dir, responses={
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


def test_request_raises_unavailable_when_no_socket(sock_dir):
    with pytest.raises(HerdrUnavailable):
        request("ping", {}, socket_path=str(sock_dir / "missing.sock"))


def test_request_raises_unavailable_on_eof(sock_dir):
    # server that accepts and closes without responding
    path = str(sock_dir / "dead.sock")
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


def test_request_raises_unavailable_on_oversized_response(sock_dir):
    # server that sends data without newline exceeding the size limit
    path = str(sock_dir / "bloat.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)

    def send_oversized():
        conn, _ = srv.accept()
        # Send data larger than the 1MB limit without newline
        conn.sendall(b"x" * (1024 * 1024 + 1000))  # 1MB + 1000 bytes without newline
        # Keep connection open to avoid EOF
        import time
        time.sleep(10)
        conn.close()

    t = threading.Thread(target=send_oversized, daemon=True)
    t.start()
    try:
        with pytest.raises(HerdrUnavailable) as exc:
            request("ping", {}, socket_path=path, timeout_s=5.0)
        assert "exceeds" in str(exc.value)
    finally:
        srv.close()


def test_request_closes_socket_with_zero_timeout():
    # Verify socket is properly closed even when timeout_s=0 expires before connect
    from unittest.mock import patch, MagicMock

    with patch('herdwatch.herdr_socket.socket.socket') as mock_socket_class:
        # Create a real MagicMock that will act like a context manager
        mock_sock = MagicMock()
        mock_socket_class.return_value = mock_sock
        mock_sock.__enter__ = MagicMock(return_value=mock_sock)
        mock_sock.__exit__ = MagicMock(return_value=False)
        # With timeout_s=0.0, deadline check should fire before connect is called
        mock_sock.connect.side_effect = Exception("Should not be called")

        with pytest.raises(HerdrUnavailable) as exc:
            request("ping", {}, socket_path="/any", timeout_s=0.0)

        # Verify:
        # 1. The socket's __exit__ (close) was called despite early timeout
        mock_sock.__exit__.assert_called_once()
        # 2. connect was never called (timeout caught it first)
        mock_sock.connect.assert_not_called()
        # 3. The error is about timeout, not the mocked connect exception
        assert "timeout" in str(exc.value).lower()


def test_request_raises_unavailable_on_slow_drip(sock_dir):
    # server that sends data very slowly (slowly drip attack):
    # each byte within timeout but unbounded total time
    path = str(sock_dir / "slow.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)

    def send_slow_drip():
        conn, _ = srv.accept()
        # Send 1KB of data, 1 byte per recv call with pauses
        # Each pause is less than timeout, but total time exceeds it
        try:
            for i in range(1024):
                conn.send(b"x")
                time.sleep(0.5)  # 0.5s per byte = 512s total >> 1s timeout
        except OSError:
            # Client closed or connection error; exit cleanly
            pass
        finally:
            conn.close()

    t = threading.Thread(target=send_slow_drip, daemon=True)
    t.start()
    try:
        with pytest.raises(HerdrUnavailable):
            request("ping", {}, socket_path=path, timeout_s=1.0)
    finally:
        srv.close()


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


def test_event_stream_raises_on_error_ack(sock_dir):
    srv = FakeServer(sock_dir, subscribe_error={"code": "not_found", "message": "pane gone"})
    try:
        with pytest.raises(HerdrApiError):
            EventStream([{"type": "pane.agent_status_changed", "pane_id": "w1:p1"}],
                        socket_path=srv.path)
    finally:
        srv.close()


def test_event_stream_raises_unavailable_when_no_socket(sock_dir):
    with pytest.raises(HerdrUnavailable):
        EventStream([{"type": "pane.created"}], socket_path=str(sock_dir / "no.sock"))
