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


def _first_line_exceeds_limit(buf: bytes) -> bool:
    """Measure the NDJSON payload, excluding its line terminator."""
    newline = buf.find(b"\n")
    length = newline if newline >= 0 else len(buf)
    return length > _MAX_RESPONSE_SIZE


def _parse_event_line(line: bytes) -> dict | None:
    try:
        event = json.loads(line)
    except ValueError:
        return None
    if not isinstance(event, dict) or not isinstance(event.get("event"), str):
        return None
    data = event.get("data")
    if data is not None and not isinstance(data, dict):
        return None
    return event


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
    try:
        msg = json.loads(line)
    except (TypeError, ValueError) as exc:
        raise HerdrUnavailable("invalid JSON response from herdr") from exc
    if not isinstance(msg, dict):
        raise HerdrUnavailable("herdr response must be a JSON object")
    if "error" in msg:
        err = msg["error"]
        if not isinstance(err, dict):
            raise HerdrUnavailable("herdr response error field must be an object")
        raise HerdrApiError(err.get("code", "unknown"), err.get("message", ""))
    result = msg.get("result")
    if not isinstance(result, dict):
        raise HerdrUnavailable("herdr response result field must be an object")
    return result


def request(method: str, params: dict, *, socket_path: str | None = None,
            timeout_s: float = 10.0) -> dict:
    path = socket_path or resolve_socket_path()
    deadline = time.monotonic() + timeout_s

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HerdrUnavailable("timeout before connect")
            conn.settimeout(remaining)
            conn.connect(path)
        except OSError as exc:
            raise HerdrUnavailable(str(exc)) from exc

        payload = json.dumps({"id": "herdwatch", "method": method, "params": params})
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HerdrUnavailable("timeout before send")
            conn.settimeout(remaining)
            conn.sendall(payload.encode() + b"\n")
        except OSError as exc:
            raise HerdrUnavailable(str(exc)) from exc

        buf = b""
        while b"\n" not in buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HerdrUnavailable("timeout waiting for response")
            conn.settimeout(remaining)
            try:
                chunk = conn.recv(_RECV_CHUNK)
            except OSError as exc:
                raise HerdrUnavailable(str(exc)) from exc
            if not chunk:
                raise HerdrUnavailable("connection closed before response")
            buf += chunk
            if _first_line_exceeds_limit(buf):
                raise HerdrUnavailable(f"response line exceeds {_MAX_RESPONSE_SIZE} bytes")

    return _parse_response(buf.split(b"\n", 1)[0])


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
        deadline = time.monotonic() + ack_timeout_s
        try:
            self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HerdrUnavailable("timeout before subscribe connect")
            self._sock.settimeout(remaining)
            self._sock.connect(path)
            payload = json.dumps({"id": "herdwatch-sub", "method": "events.subscribe",
                                  "params": {"subscriptions": subscriptions}})
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HerdrUnavailable("timeout before subscribe send")
            self._sock.settimeout(remaining)
            self._sock.sendall(payload.encode() + b"\n")
            while b"\n" not in self._buf:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise HerdrUnavailable("timeout waiting for subscribe ack")
                self._sock.settimeout(remaining)
                try:
                    chunk = self._sock.recv(_RECV_CHUNK)
                except OSError as exc:
                    raise HerdrUnavailable(str(exc)) from exc
                if not chunk:
                    raise HerdrUnavailable("connection closed before subscribe ack")
                self._buf += chunk
                if _first_line_exceeds_limit(self._buf):
                    raise HerdrUnavailable(f"subscribe ack line exceeds {_MAX_RESPONSE_SIZE} bytes")
        except HerdrUnavailable:
            self.close()
            raise
        except OSError as exc:
            self.close()
            raise HerdrUnavailable(str(exc)) from exc
        line, self._buf = self._buf.split(b"\n", 1)
        try:
            msg = json.loads(line)
            if not isinstance(msg, dict):
                raise HerdrUnavailable("subscribe ack must be a JSON object")
            if "error" in msg:
                err = msg["error"]
                if not isinstance(err, dict):
                    raise HerdrUnavailable("subscribe ack error field must be an object")
                raise HerdrApiError(err.get("code", "unknown"), err.get("message", ""))
            result = msg.get("result")
            if not isinstance(result, dict):
                raise HerdrUnavailable(
                    "subscribe ack result field must be an object"
                )
            if result.get("type") != "subscription_started":
                raise HerdrUnavailable(
                    "subscribe ack result type must be subscription_started"
                )
        except HerdrApiError:
            self.close()
            raise
        except (ValueError, HerdrUnavailable, AttributeError, TypeError) as exc:
            self.close()
            if isinstance(exc, HerdrUnavailable):
                raise
            raise HerdrUnavailable("invalid subscribe ack") from None
        self._sock.setblocking(False)

    def fileno(self) -> int:
        return self._sock.fileno()

    @property
    def has_buffered_data(self) -> bool:
        """Whether an incomplete event line is waiting for more bytes."""
        return bool(self._buf)

    def read_events(self, *, max_chunks: int | None = None) -> list[dict]:
        """Drain complete event lines without blocking. On EOF or a socket
        error, parse what remains and set `closed`. ``max_chunks`` bounds
        socket reads so callers with a deadline can regain control even while
        a producer continuously fills the socket. Zero only parses buffered
        complete lines."""
        events: list[dict] = []
        chunks_read = 0
        if not self.closed:
            while True:
                if max_chunks is not None and chunks_read >= max_chunks:
                    break
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
                chunks_read += 1
                self._buf += chunk
                # Continuously process complete lines to prevent buffer growth.
                while b"\n" in self._buf:
                    line, self._buf = self._buf.split(b"\n", 1)
                    # Enforce max line size first, before checking if line is empty.
                    # This prevents whitespace-only lines from bypassing the check.
                    if len(line) > _MAX_RESPONSE_SIZE:
                        self.closed = True
                        self._buf = b""
                        return events
                    if not line.strip():
                        continue
                    event = _parse_event_line(line)
                    if event is not None:
                        events.append(event)
                # The loop above drained every complete line, so only an
                # incomplete suffix remains. Bound it without raising; the
                # daemon reconnects when it observes `closed`.
                if len(self._buf) > _MAX_RESPONSE_SIZE:
                    self.closed = True
                    self._buf = b""
                    break
        # Process any remaining complete lines after socket is closed
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            # Enforce max line size first, before checking if line is empty
            if len(line) > _MAX_RESPONSE_SIZE:
                self.closed = True
                self._buf = b""
                break
            if not line.strip():
                continue
            event = _parse_event_line(line)
            if event is not None:
                events.append(event)
        # Final check: discard any remaining oversized incomplete suffix
        if len(self._buf) > _MAX_RESPONSE_SIZE:
            self.closed = True
            self._buf = b""
        return events

    def close(self) -> None:
        self.closed = True
        sock = getattr(self, "_sock", None)
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
