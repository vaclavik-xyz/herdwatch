# src/herdwatch/herdr.py
"""HerdrClient: herdwatch's facade over the raw herdr socket API.

Most write methods keep boolean semantics (False = failed, retry later) so
the daemon's retry logic stays transport-agnostic; metadata `clear` treats
a structured `not_found` as success (the pane is gone and its metadata
with it). `release_agent` is the exception — it returns "ok" / "gone" /
"failed", because `not_found` may mean the pane was *moved* (assertion
alive under a new pane id): the caller must reconcile before dropping
bookkeeping. `session_snapshot` raises instead — the daemon needs to tell
"herdr is down" (HerdrUnavailable, retry with backoff) from an incompatible
server (HerdrApiError, log the >= 0.7.4 requirement).
"""
from __future__ import annotations

import logging
from typing import Callable

from . import herdr_socket
from .herdr_socket import HerdrApiError, HerdrUnavailable

log = logging.getLogger(__name__)


def validate_agent_record(
    record,
    *,
    context: str = "agent record",
    expected_pane_id: str | None = None,
) -> dict:
    """Validate agent data before daemon bookkeeping can observe it."""
    if not isinstance(record, dict):
        raise HerdrUnavailable(f"{context} must be an object")
    pane_id = record.get("pane_id")
    if not isinstance(pane_id, str) or not pane_id:
        raise HerdrUnavailable(f"{context} pane_id must be a string")
    if expected_pane_id is not None and pane_id != expected_pane_id:
        raise HerdrUnavailable(
            f"{context} pane_id does not match requested pane"
        )
    terminal_id = record.get("terminal_id")
    if terminal_id is not None and (
        not isinstance(terminal_id, str) or not terminal_id
    ):
        raise HerdrUnavailable(
            f"{context} terminal_id must be a nonempty string or null"
        )
    for field in (
        "agent",
        "agent_status",
        "cwd",
        "foreground_cwd",
    ):
        value = record.get(field)
        if value is not None and not isinstance(value, str):
            raise HerdrUnavailable(
                f"{context} {field} must be a string or null"
            )
    tokens = record.get("tokens")
    if tokens is not None and (
        not isinstance(tokens, dict)
        or not all(isinstance(key, str) for key in tokens)
        or not all(isinstance(value, str) for value in tokens.values())
    ):
        raise HerdrUnavailable(f"{context} tokens must be a string map")
    session = record.get("agent_session")
    if session is not None and not isinstance(session, dict):
        raise HerdrUnavailable(
            f"{context} agent_session must be an object or null"
        )
    if isinstance(session, dict):
        for field in ("source", "agent", "kind", "value"):
            value = session.get(field)
            if value is not None and not isinstance(value, str):
                raise HerdrUnavailable(
                    f"{context} agent_session.{field} must be a string or null"
                )
    return record


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
        result = self._call("session.snapshot", {})
        if not isinstance(result, dict):
            raise HerdrUnavailable("session.snapshot result must be an object")
        snapshot = result.get("snapshot")
        if not isinstance(snapshot, dict):
            raise HerdrUnavailable(
                "session.snapshot result is missing the snapshot object"
            )
        return snapshot

    def agent_get(self, pane_id: str) -> dict | None:
        try:
            result = self._call("agent.get", {"target": pane_id})
        except (HerdrApiError, HerdrUnavailable) as exc:
            log.debug("agent.get %s failed: %s", pane_id, exc)
            return None
        agent = result.get("agent")
        if agent is None:
            return None
        try:
            return validate_agent_record(
                agent,
                context="agent.get agent",
                expected_pane_id=pane_id,
            )
        except HerdrUnavailable as exc:
            log.warning("agent.get %s returned invalid data: %s", pane_id, exc)
            return None

    def report_agent(
        self, pane_id: str, source: str, agent: str, state: str
    ) -> bool | None:
        params = {
            "pane_id": pane_id,
            "source": source,
            "agent": agent,
            "state": state,
        }
        try:
            self._call("pane.report_agent", params)
        except HerdrApiError as exc:
            log.warning("herdr pane.report_agent failed: %s", exc)
            return False
        except HerdrUnavailable as exc:
            # The transport can fail after send (for example while reading the
            # response), so application is unknown until a later readback.
            log.warning("herdr pane.report_agent outcome is unknown: %s", exc)
            return None

        # Authority arbitration can accept a request without making it the
        # effective lifecycle source. Always verify the applied state.
        observed = self.agent_get(pane_id)
        if observed is None:
            log.warning(
                "herdr accepted pane.report_agent for %s but its applied "
                "state could not be verified yet",
                pane_id,
            )
            return None
        applied = (
            observed.get("agent") == agent
            and observed.get("agent_status") == state
        )
        if not applied:
            log.warning(
                "herdr accepted pane.report_agent for %s but did not apply "
                "the requested %s state",
                pane_id,
                state,
            )
        return applied

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

    def report_metadata(
        self,
        pane_id: str,
        source: str,
        *,
        tokens: dict[str, str | None],
        ttl_ms: int | None = None,
    ) -> bool:
        params: dict = {"pane_id": pane_id, "source": source, "tokens": tokens}
        if ttl_ms is not None:
            params["ttl_ms"] = ttl_ms
        clearing = bool(tokens) and all(value is None for value in tokens.values())
        return self._call_bool(
            "pane.report_metadata", params, not_found_ok=clearing
        )

    def pane_process_info(self, pane_id: str) -> dict:
        try:
            result = self._call("pane.process_info", {"pane_id": pane_id})
        except (HerdrApiError, HerdrUnavailable):
            return {}
        info = result.get("process_info")
        return info if isinstance(info, dict) else {}
