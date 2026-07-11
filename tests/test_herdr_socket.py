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
