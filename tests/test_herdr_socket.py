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


def test_request_raises_unavailable_on_oversized_response(tmp_path):
    # server that sends data without newline exceeding the size limit
    path = str(tmp_path / "bloat.sock")
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


def test_request_raises_unavailable_on_slow_drip(tmp_path):
    # server that sends data very slowly (slowly drip attack):
    # each byte within timeout but unbounded total time
    import time
    path = str(tmp_path / "slow.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)

    def send_slow_drip():
        conn, _ = srv.accept()
        # Send 1KB of data, 1 byte per recv call with pauses
        # Each pause is less than timeout, but total time exceeds it
        for i in range(1024):
            conn.send(b"x")
            time.sleep(0.5)  # 0.5s per byte = 512s total >> 1s timeout
        conn.close()

    t = threading.Thread(target=send_slow_drip, daemon=True)
    t.start()
    try:
        with pytest.raises(HerdrUnavailable):
            request("ping", {}, socket_path=path, timeout_s=1.0)
    finally:
        srv.close()
