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
import time

DEFAULT_SOCKET_PATH = "~/.config/herdr/herdr.sock"
SESSION_SOCKET_PATH = "~/.config/herdr/sessions/{name}/herdr.sock"
_RECV_CHUNK = 65536
_MAX_RESPONSE_SIZE = 1024 * 1024  # 1MB max for a single NDJSON line


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
    deadline = time.time() + timeout_s

    try:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(timeout_s)
        conn.connect(path)
    except OSError as exc:
        raise HerdrUnavailable(str(exc)) from exc
    try:
        payload = json.dumps({"id": "herdwatch", "method": method, "params": params})
        try:
            conn.sendall(payload.encode() + b"\n")
        except OSError as exc:
            raise HerdrUnavailable(str(exc)) from exc
        buf = b""
        while b"\n" not in buf:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise HerdrUnavailable("timeout waiting for response")
            conn.settimeout(min(remaining, timeout_s))
            try:
                chunk = conn.recv(_RECV_CHUNK)
            except OSError as exc:
                raise HerdrUnavailable(str(exc)) from exc
            if not chunk:
                raise HerdrUnavailable("connection closed before response")
            buf += chunk
            if len(buf) > _MAX_RESPONSE_SIZE:
                raise HerdrUnavailable(f"response line exceeds {_MAX_RESPONSE_SIZE} bytes")
    finally:
        conn.close()
    return _parse_response(buf.split(b"\n", 1)[0])
