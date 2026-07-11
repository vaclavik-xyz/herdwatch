# src/herdwatch/herdr.py
"""HerdrClient: herdwatch's facade over the raw herdr socket API.

Most write methods keep boolean semantics (False = failed, retry later) so
the daemon's retry logic stays transport-agnostic; metadata `clear` treats
a structured `not_found` as success (the pane is gone and its metadata
with it). `release_agent` is the exception — it returns "ok" / "gone" /
"failed", because `not_found` may mean the pane was *moved* (assertion
alive under a new pane id): the caller must reconcile before dropping
bookkeeping. `session_snapshot` raises instead — the daemon needs to tell
"herdr is down" (HerdrUnavailable, retry with backoff) from "server too
old for session.snapshot" (HerdrApiError, log the >= 0.7.2 requirement).
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
