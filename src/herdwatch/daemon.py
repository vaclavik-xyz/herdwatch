# src/herdwatch/daemon.py
from __future__ import annotations

import atexit
import logging
import math
import os
import selectors
import signal
import time
from dataclasses import dataclass
from typing import Callable

from . import gitctx, herdr_socket
from .aggregate import aggregate
from .cache import TTLCache
from .config import Config
from .herdr import HerdrClient
from .herdr_socket import HerdrApiError, HerdrUnavailable
from .markers import MarkerStore
from .models import PaneContext, Pending
from .probes.bgjobs import BgJobsProbe
from .probes.ci import CIProbe
from .probes.marker import MarkerProbe
from .probes.roborev import RoborevProbe
from .progress import progress_label
from .state import StateStore

SOURCE = "herdwatch"
GLOBAL_SUBSCRIPTIONS = [
    {"type": "pane.created"},
    {"type": "pane.closed"},
    {"type": "pane.exited"},
    {"type": "pane.moved"},
    {"type": "workspace.closed"},
    {"type": "tab.closed"},
]
# Herdr 0.7.3 starts generic event cursors at zero and emits at most one
# retained event of each type per 100 ms loop. pane.agent_detected is noisy
# enough to replay for tens of seconds and then trip the server's macOS EAGAIN
# bug, repeatedly destroying the status stream. Subscribing to status changes
# for every pane (including unknown ones) preserves immediate agent discovery
# without that replay-broken generic feed.
# herdr emits dotted kinds on subscription events and snake_case kinds on
# generic lifecycle events; accept both spellings for each
LIFECYCLE_RESYNC_KINDS = {
    "pane.created",
    "pane_created",
    "pane.closed",
    "pane_closed",
    "pane.exited",
    "pane_exited",
    "pane.agent_detected",
    "pane_agent_detected",
    "workspace.closed",
    "workspace_closed",
    "tab.closed",
    "tab_closed",
}
MARKER_DIR = os.path.expanduser("~/.local/state/herdwatch/markers")
TTL_MIN_MS = 1000
TTL_MAX_MS = 86_400_000
LIFECYCLE_RESYNC_DEBOUNCE_S = 0.25
STARTUP_REPLAY_QUIET_S = 0.25
STARTUP_REPLAY_MAX_S = 55.0
SEMANTIC_HOLD_KINDS = {"hold", "hold-pending"}
log = logging.getLogger(__name__)


@dataclass
class ManagedPane:
    custom_status: str
    agent: str
    # "hold" | "hold-pending" | "idle-meta" | "progress" | "done"
    kind: str = "hold"
    terminal_id: str = ""


class Daemon:
    def __init__(
        self,
        client,
        probes,
        *,
        reprobe_interval_s: float = 15.0,
        resync_interval_s: float = 60.0,
        progress_interval_s: float = 4.0,
        clock: Callable[[], float] = time.monotonic,
        enrich: Callable[[str], gitctx.GitInfo] = gitctx.enrich,
        allow: list[str] | None = None,
        deny: list[str] | None = None,
        on_snapshot: Callable[[list[dict]], None] = lambda rows: None,
        progress: Callable[[str], str | None] | None = None,
        stream_factory=None,
        backoff_base_s: float = 0.5,
        backoff_max_s: float = 30.0,
        startup_replay_quiet_s: float = STARTUP_REPLAY_QUIET_S,
        startup_replay_max_s: float = STARTUP_REPLAY_MAX_S,
    ) -> None:
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
        self._startup_replay_quiet = startup_replay_quiet_s
        self._startup_replay_max = startup_replay_max_s
        self.managed: dict[str, ManagedPane] = {}
        # Pre-migration semantic assertions awaiting release (see adopt).
        self._legacy_release: dict[str, ManagedPane] = {}
        # Last known agent records, keyed by pane_id (snapshot is truth).
        self._registry: dict[str, dict] = {}
        self._subscribed_pane_ids: set[str] = set()
        self._last_probe: dict[str, float] = {}
        # herdr reports agent_session only for idle/done panes, never while a
        # pane is working -- exactly when the progress path needs it. Cache it
        # from any record that carries it.
        self._session_cache: dict[str, str] = {}
        self._meta_asserted_at: dict[str, float] = {}
        self._adopted: set[str] = set()
        self._stream = None
        self._resync_due = False
        self._resync_not_before: float | None = None
        self._last_boot_error: str | None = None

    # ---------- small helpers ----------

    def _ttl_ms(self, kind: str | None = None) -> int:
        interval = self._reprobe
        if kind == "progress":
            interval = max(interval, self._progress_interval)
        max_interval = TTL_MAX_MS / 2000.0
        if not math.isfinite(interval):
            interval = max_interval if interval > 0 else 0.0
        interval = max(0.0, min(interval, max_interval))
        ttl = int(2 * interval * 1000)
        return max(TTL_MIN_MS, min(TTL_MAX_MS, ttl))

    def _eligible(self, pane_id: str) -> bool:
        if self._deny and pane_id in self._deny:
            return False
        if self._allow and pane_id not in self._allow:
            return False
        return True

    def _schedule_resync(self, *, debounce: bool = True) -> None:
        was_due = self._resync_due
        self._resync_due = True
        if not debounce:
            self._resync_not_before = None
        elif not was_due:
            self._resync_not_before = (
                self._clock() + LIFECYCLE_RESYNC_DEBOUNCE_S
            )

    def _resync_ready(self, now: float | None = None) -> bool:
        if not self._resync_due:
            return False
        if self._resync_not_before is None:
            return True
        return (self._clock() if now is None else now) >= self._resync_not_before

    def _remember_record(self, rec: dict) -> None:
        session = (rec.get("agent_session") or {}).get("value")
        if session:
            self._session_cache[rec["pane_id"]] = session

    def _terminal_id(self, pane_id: str) -> str:
        rec = self._registry.get(pane_id) or {}
        return rec.get("terminal_id") or ""

    def _rows(self) -> list[dict]:
        rows = [
            {
                "pane_id": pane_id,
                "agent": mp.agent,
                "status": mp.custom_status,
                "kind": mp.kind,
                "terminal_id": mp.terminal_id,
                "meta": mp.kind not in SEMANTIC_HOLD_KINDS,
            }
            for pane_id, mp in sorted(self.managed.items())
        ]
        rows += [
            {
                "pane_id": pane_id,
                "agent": mp.agent,
                "status": mp.custom_status,
                "kind": mp.kind,
                "terminal_id": mp.terminal_id,
                "meta": False,
            }
            for pane_id, mp in sorted(self._legacy_release.items())
        ]
        return rows

    def _publish(self) -> None:
        try:
            self._on_snapshot(self._rows())
        except Exception:
            log.warning("snapshot publish failed; continuing", exc_info=True)

    def adopt(self, rows: list[dict]) -> None:
        """Recover state from a prior run.

        Hold rows are re-adopted and force one re-assert. Metadata rows from
        this daemon generation are skipped because their TTL self-cleans.
        Non-hold rows without the flag are pre-migration semantic assertions
        and stay in a retry set until release_agent confirms cleanup.
        """
        for row in rows:
            pane_id = row.get("pane_id")
            if not pane_id:
                continue
            mp = ManagedPane(
                row.get("status", ""),
                row.get("agent", "agent"),
                kind=row.get("kind", "hold"),
                terminal_id=row.get("terminal_id", ""),
            )
            if mp.kind in SEMANTIC_HOLD_KINDS:
                self.managed[pane_id] = mp
                self._adopted.add(pane_id)
            elif not row.get("meta"):
                self._legacy_release[pane_id] = mp

    # ---------- bootstrap ----------

    def _subscriptions_for(self, pane_ids: list[str]) -> list[dict]:
        subscriptions = [dict(subscription) for subscription in GLOBAL_SUBSCRIPTIONS]
        subscriptions += [
            {"type": "pane.agent_status_changed", "pane_id": pane_id}
            for pane_id in sorted(pane_ids)
        ]
        return subscriptions

    @staticmethod
    def _snapshot_pane_ids(snapshot: dict) -> set[str]:
        """Return every pane that can carry a per-pane status subscription.

        Real session snapshots expose all panes separately from detected
        agents. Falling back to agents keeps transport fakes and older recorded
        fixtures useful without weakening the live behavior.
        """
        panes = snapshot.get("panes")
        rows = panes if isinstance(panes, list) else snapshot.get("agents", [])
        return {
            row["pane_id"]
            for row in rows
            if isinstance(row, dict) and row.get("pane_id")
        }

    @staticmethod
    def _snapshot_agent_records(snapshot: dict) -> dict[str, dict]:
        agents = snapshot.get("agents")
        if not isinstance(agents, list):
            raise HerdrUnavailable(
                "session.snapshot agents field must be a list"
            )
        records = {}
        for agent in agents:
            if not isinstance(agent, dict):
                raise HerdrUnavailable(
                    "session.snapshot agent entries must be objects"
                )
            pane_id = agent.get("pane_id")
            if not isinstance(pane_id, str) or not pane_id:
                raise HerdrUnavailable(
                    "session.snapshot agent pane_id must be a string"
                )
            terminal_id = agent.get("terminal_id")
            if terminal_id is not None and (
                not isinstance(terminal_id, str) or not terminal_id
            ):
                raise HerdrUnavailable(
                    "session.snapshot agent terminal_id must be a nonempty "
                    "string or null"
                )
            for field in (
                "agent",
                "agent_status",
                "cwd",
                "foreground_cwd",
                "custom_status",
            ):
                value = agent.get(field)
                if value is not None and not isinstance(value, str):
                    raise HerdrUnavailable(
                        f"session.snapshot agent {field} must be a string "
                        "or null"
                    )
            session = agent.get("agent_session")
            if session is not None and not isinstance(session, dict):
                raise HerdrUnavailable(
                    "session.snapshot agent agent_session must be an object "
                    "or null"
                )
            if isinstance(session, dict):
                for field in ("source", "agent", "kind", "value"):
                    value = session.get(field)
                    if value is not None and not isinstance(value, str):
                        raise HerdrUnavailable(
                            "session.snapshot agent agent_session."
                            f"{field} must be a string or null"
                        )
            records[pane_id] = agent
        return records

    def bootstrap(self) -> bool:
        """Subscribe between two snapshots and seed the registry from truth."""
        if self._stream_factory is None:
            return False
        for _ in range(5):
            try:
                snapshot = self._client.session_snapshot()
            except HerdrApiError as exc:
                self._log_boot_failure(
                    "herdwatch requires herdr >= 0.7.2 with "
                    f"session.snapshot (server said: {exc})",
                    error=True,
                )
                return False
            except HerdrUnavailable as exc:
                self._log_boot_failure(f"herdr unreachable: {exc}")
                return False
            pane_ids = self._snapshot_pane_ids(snapshot)
            try:
                stream = self._stream_factory(
                    self._subscriptions_for(sorted(pane_ids))
                )
            except HerdrApiError as exc:
                log.info(
                    "subscribe rejected (%s); retrying with a fresh snapshot",
                    exc,
                )
                continue
            except HerdrUnavailable as exc:
                self._log_boot_failure(f"subscribe failed: {exc}")
                return False
            try:
                snapshot = self._client.session_snapshot()
                records = self._snapshot_agent_records(snapshot)
            except (HerdrApiError, HerdrUnavailable) as exc:
                log.warning("bootstrap: post-subscribe snapshot failed: %s", exc)
                stream.close()
                return False
            if self._snapshot_pane_ids(snapshot) != pane_ids:
                stream.close()
                continue
            self._stream = stream
            self._registry = records
            self._subscribed_pane_ids = pane_ids
            by_terminal = {
                record["terminal_id"]: pane_id
                for pane_id, record in records.items()
                if record.get("terminal_id")
            }
            self._backfill_legacy_terminals(records)
            self._reconcile_books(records, by_terminal)
            for record in records.values():
                self._remember_record(record)
            for mapping in (
                self._last_probe,
                self._session_cache,
                self._meta_asserted_at,
            ):
                for pane_id in list(mapping):
                    if pane_id not in records:
                        mapping.pop(pane_id, None)
            self._last_boot_error = None
            log.info("connected to herdr (%d panes)", len(records))
            return True
        self._log_boot_failure("pane set kept drifting; will retry")
        return False

    def _log_boot_failure(self, reason: str, *, error: bool = False) -> None:
        """Log each distinct bootstrap failure once across retry attempts."""
        if reason == self._last_boot_error:
            return
        self._last_boot_error = reason
        (log.error if error else log.warning)("bootstrap: %s", reason)

    def _drain_startup_replay(
        self, selector, stream
    ) -> tuple[str, int]:
        """Drain retained subscription events before running slow probes.

        Herdr 0.7.3 replays retained events to every new subscriber. Processing
        those events individually is both stale and slow enough to fill the
        socket while probes are running. Drain until the stream has been quiet,
        then let one authoritative snapshot reconcile all startup changes.

        Return ``(outcome, event_count)``. A timeout is not success: letting
        stale events through after the authoritative snapshot could overwrite
        current state, so the caller must reconnect instead.
        """
        quiet_s = max(0.0, self._startup_replay_quiet)
        max_s = max(0.0, self._startup_replay_max)
        if quiet_s == 0.0 or max_s == 0.0:
            return ("closed" if stream.closed else "disabled"), 0

        started = self._clock()
        stop_at = started + max_s
        quiet_until = started + quiet_s
        buffered = stream.read_events(max_chunks=0)
        if stream.closed:
            return "closed", 0
        drained = len(buffered)
        if buffered or stream.has_buffered_data:
            quiet_until = self._clock() + quiet_s
        while True:
            now = self._clock()
            if now >= stop_at:
                return "timeout", drained
            if now >= quiet_until and not stream.has_buffered_data:
                # Close the race where data becomes readable exactly as the
                # preceding timed select returns with no events.
                ready = selector.select(0.0)
                if not ready:
                    return "quiet", drained
            else:
                deadline = (
                    stop_at
                    if now >= quiet_until
                    else min(quiet_until, stop_at)
                )
                ready = selector.select(max(0.0, deadline - now))
            if not ready:
                continue
            messages = stream.read_events(max_chunks=1)
            if stream.closed:
                return "closed", drained
            # Readability itself is replay activity, including a partial line.
            quiet_until = self._clock() + quiet_s
            if messages:
                drained += len(messages)

    # ---------- event dispatch ----------

    def dispatch_event(self, msg: dict) -> None:
        kind = msg.get("event") or ""
        data = msg.get("data") or {}
        if kind in ("pane.agent_status_changed", "pane_agent_status_changed"):
            self._on_status_event(data)
        elif kind in ("pane.moved", "pane_moved"):
            self._on_pane_moved(data)
        elif kind in ("pane.agent_detected", "pane_agent_detected"):
            pane_id = data.get("pane_id")
            current = self._registry.get(pane_id) if pane_id else None
            detected = data.get("agent")
            if current is not None and (
                not detected or detected == current.get("agent")
            ):
                return
            self._schedule_resync()
        elif kind in LIFECYCLE_RESYNC_KINDS:
            self._schedule_resync()

    def _on_status_event(self, data: dict) -> None:
        pane_id = data.get("pane_id")
        if not pane_id or not self._eligible(pane_id):
            return
        rec = self._registry.get(pane_id)
        if rec is None:
            self._schedule_resync(debounce=False)  # unknown pane: topology drifted
            return
        # Herdr 0.7.3 replays retained subscription events. Snapshot/readback
        # is truth: a stale working event must not suppress a hold for a pane
        # that is currently idle (and a stale idle event must not claim one
        # that is currently working).
        event_status = data.get("agent_status") or "unknown"
        prev = rec.get("agent_status")
        observed = self._client.agent_get(pane_id)
        if observed is None:
            self._schedule_resync(debounce=False)
            return
        self._registry[pane_id] = observed
        self._remember_record(observed)
        status = observed.get("agent_status") or "unknown"
        mp = self.managed.get(pane_id)
        if mp is not None:
            expected = {
                "done": "done",
                "idle-meta": "idle",
            }.get(mp.kind, "working")
            event_matches = (
                event_status == expected
                and (data.get("custom_status") or "") == mp.custom_status
                and (
                    mp.kind != "hold-pending"
                    or data.get("agent") == mp.agent
                )
            )
            observed_matches = (
                status == expected
                and observed.get("agent") == mp.agent
                and (observed.get("custom_status") or "")
                == mp.custom_status
            )
            if event_matches and observed_matches:
                if mp.kind == "hold-pending":
                    mp.kind = "hold"
                    self._adopted.discard(pane_id)
                    log.info("verified pending hold %s from event", pane_id)
                return  # verified ack of our own report/metadata write
        if mp is not None and mp.kind == "idle-meta" and status != "idle":
            if not self._clear_metadata(pane_id, f"left idle ({status})"):
                return
            self._last_probe.pop(pane_id, None)
            mp = None
        if mp is not None and mp.kind == "progress" and status != "working":
            # agent stopped (or blocked): drop the label now, and hold in the
            # same dispatch when the pane is idle with pending work
            if not self._clear_metadata(pane_id, "agent stopped"):
                return
            self._last_probe.pop(pane_id, None)
        if status in ("idle", "done"):
            if prev != status:
                self._last_probe.pop(pane_id, None)  # fresh edge: probe now
            self._probe_pane(pane_id, fast=True)
            self._publish()
            return
        # working/blocked/unknown edge: forget the timer so the next
        # idle/done edge probes immediately (old tick semantics)
        self._last_probe.pop(pane_id, None)

    def _on_pane_moved(self, data: dict) -> None:
        pane = data.get("pane") or {}
        old, new = data.get("previous_pane_id"), pane.get("pane_id")
        if not old or not new:
            self._schedule_resync(debounce=False)
            return
        terminal_id = pane.get("terminal_id")
        old_record = self._registry.get(old)
        new_record = self._registry.get(new)
        tracked = self.managed.get(old) or self._legacy_release.get(old)
        if old_record is None:
            if (
                terminal_id
                and new_record is not None
                and new_record.get("terminal_id") == terminal_id
            ):
                return  # retained replay of a move already in the snapshot
            if (
                not terminal_id
                or tracked is None
                or tracked.terminal_id != terminal_id
            ):
                self._schedule_resync()
                return
        elif not terminal_id or old_record.get("terminal_id") != terminal_id:
            # A stale replay must never remap or tear down the current stream;
            # let the next coalesced snapshot decide what is true now.
            self._schedule_resync()
            return
        self._remap(old, new, pane)
        # The per-pane agent_status_changed subscription is bound to the OLD
        # public pane id and goes silent after a move (herdr matches events
        # by pane_id). A later resync cannot notice -- the registry is
        # already remapped -- so tear the stream down here; the run loop
        # re-bootstraps and resubscribes with the new pane set.
        if self._stream is not None:
            self._stream.close()
            self._stream = None
        self._schedule_resync(debounce=False)

    def _remap(self, old: str, new: str, rec: dict | None) -> None:
        """Follow herdr's pane-id change: the assertion lives on inside herdr,
        so bookkeeping must follow it -- releasing the old id would just
        `not_found` while a permanent `working ⏳` survived under the new one."""
        if rec:
            self._registry[new] = rec
        self._registry.pop(old, None)
        for mapping in (
            self._last_probe,
            self._session_cache,
            self._meta_asserted_at,
        ):
            if old in mapping:
                mapping[new] = mapping.pop(old)
        if rec:
            # Prefer fresh move-event metadata over the remapped old cache.
            self._remember_record(rec)
        if old in self.managed:
            self.managed[new] = self.managed.pop(old)
        if old in self._adopted:
            self._adopted.discard(old)
            self._adopted.add(new)
        if old in self._legacy_release:
            self._legacy_release[new] = self._legacy_release.pop(old)
        mp = self.managed.get(new)
        if mp is not None and not self._eligible(new):
            if mp.kind in SEMANTIC_HOLD_KINDS:
                self._release_hold(new, "moved to ineligible pane")
            else:
                self._clear_metadata(new, "moved to ineligible pane")
        self._publish()

    def _backfill_legacy_terminals(self, records: dict[str, dict]) -> None:
        """Legacy rows predate terminal_id persistence; grab it from the
        first snapshot that still shows the pane, so later moves remap."""
        for pane_id, mp in self._legacy_release.items():
            if not mp.terminal_id and pane_id in records:
                mp.terminal_id = records[pane_id].get("terminal_id") or ""

    def _apply_terminal_moves(
        self,
        records: dict[str, dict],
        by_terminal: dict[str, str],
    ) -> None:
        """Apply direct terminal-id remaps atomically, including swaps."""
        books = (self.managed, self._legacy_release)
        candidates: dict[tuple[int, str], str] = {}
        for book_index, book in enumerate(books):
            for pane_id, mp in book.items():
                if not mp.terminal_id:
                    continue
                new_id = by_terminal.get(mp.terminal_id)
                if new_id and new_id != pane_id:
                    candidates[(book_index, pane_id)] = new_id
        if not candidates:
            return

        target_counts: dict[str, int] = {}
        for target in candidates.values():
            target_counts[target] = target_counts.get(target, 0) + 1
        safe = {
            source
            for source, target in candidates.items()
            if target_counts[target] == 1
        }
        while True:
            blocked = {
                source
                for source in safe
                if any(
                    (book_index, candidates[source]) not in safe
                    for book_index, book in enumerate(books)
                    if candidates[source] in book
                )
            }
            if not blocked:
                break
            safe.difference_update(blocked)
        if not safe:
            return

        rows = {
            source: books[source[0]].pop(source[1]) for source in safe
        }
        public_moves = {
            source[1]: candidates[source]
            for source in safe
        }
        source_counts: dict[str, int] = {}
        for _, pane_id in safe:
            source_counts[pane_id] = source_counts.get(pane_id, 0) + 1
        for mapping in (
            self._last_probe,
            self._session_cache,
            self._meta_asserted_at,
        ):
            captured = {
                old: mapping[old]
                for old in public_moves
                if source_counts[old] == 1 and old in mapping
            }
            for pane_id in set(public_moves) | set(public_moves.values()):
                mapping.pop(pane_id, None)
            for old, value in captured.items():
                mapping[public_moves[old]] = value

        adopted = {
            pane_id
            for book_index, pane_id in safe
            if book_index == 0 and pane_id in self._adopted
        }
        for _, pane_id in safe:
            self._adopted.discard(pane_id)
        moved_managed_targets = []
        for source, mp in rows.items():
            target = candidates[source]
            books[source[0]][target] = mp
            if source[0] == 0:
                moved_managed_targets.append(target)
            if source[0] == 0 and source[1] in adopted:
                self._adopted.add(target)
            log.info("remapped %s -> %s by terminal id", source[1], target)
        for pane_id in moved_managed_targets:
            current = records.get(pane_id)
            if current is not None:
                self._registry[pane_id] = current
                self._remember_record(current)
            mp = self.managed.get(pane_id)
            if mp is None or self._eligible(pane_id):
                continue
            if mp.kind in SEMANTIC_HOLD_KINDS:
                self._release_hold(pane_id, "moved to ineligible pane")
            else:
                self._clear_metadata(pane_id, "moved to ineligible pane")

    def _reconcile_books(
        self, records: dict[str, dict], by_terminal: dict[str, str]
    ) -> None:
        """Move reconciliation + vanish handling for managed and legacy rows
        against snapshot truth. Shared by _resync and bootstrap (so adopted
        rows are reconciled BEFORE the first sweep can release stale ids)."""
        self._apply_terminal_moves(records, by_terminal)
        for book in (self.managed, self._legacy_release):
            for pane_id in list(book):
                mp = book[pane_id]
                current = records.get(pane_id)
                reused_pane_id = (
                    current is not None
                    and bool(mp.terminal_id)
                    and (current.get("terminal_id") or "") != mp.terminal_id
                )
                if current is not None and not reused_pane_id:
                    continue
                new_id = (
                    by_terminal.get(mp.terminal_id) if mp.terminal_id else None
                )
                if (
                    new_id is None
                    and not mp.terminal_id
                    and (
                        book is self._legacy_release
                        or (
                            book is self.managed
                            and pane_id in self._adopted
                            and mp.kind in SEMANTIC_HOLD_KINDS
                        )
                    )
                ):
                    # A pre-migration row can move before the first snapshot
                    # and has no terminal_id. Salvage legacy cleanup rows and
                    # adopted holds only; runtime-managed rows must never be
                    # remapped by a label guess. A unique match among
                    # untracked panes is safe: a mismatch either has no
                    # herdwatch assertion or carries an orphan from our source.
                    matches = [
                        pid
                        for pid, a in records.items()
                        if pid not in self.managed
                        and pid not in self._legacy_release
                        and (a.get("agent") or "") == mp.agent
                        and (a.get("custom_status") or "") == mp.custom_status
                    ]
                    if len(matches) == 1:
                        new_id = matches[0]
                if (
                    new_id
                    and new_id not in self.managed
                    and new_id not in self._legacy_release
                ):
                    self._remap(pane_id, new_id, records.get(new_id))
                    continue
                del book[pane_id]
                if reused_pane_id:
                    for mapping in (
                        self._last_probe,
                        self._session_cache,
                        self._meta_asserted_at,
                    ):
                        mapping.pop(pane_id, None)
                    if book is self.managed:
                        self._adopted.discard(pane_id)
                    log.warning(
                        "dropped stale %s for reused pane id %s",
                        mp.kind,
                        pane_id,
                    )
                    continue
                if book is self.managed:
                    self._adopted.discard(pane_id)
                    # best-effort cleanup; the pane is gone, drop regardless
                    if mp.kind in SEMANTIC_HOLD_KINDS:
                        self._client.release_agent(pane_id, SOURCE, mp.agent)
                    else:
                        self._client.report_metadata(
                            pane_id,
                            SOURCE,
                            agent=mp.agent,
                            clear_custom_status=True,
                        )
                    log.info("dropped vanished pane %s (%s)", pane_id, mp.kind)

    def _resync(self) -> bool:
        """Snapshot is truth: reconcile managed/legacy/registry against it.
        Never raises; herdr being down keeps all state for a later retry."""
        self._resync_due = False
        self._resync_not_before = None
        try:
            snap = self._client.session_snapshot()
            records = self._snapshot_agent_records(snap)
        except HerdrApiError as exc:
            log.error(
                "herdwatch requires herdr >= 0.7.2 with session.snapshot "
                "(server said: %s)",
                exc,
            )
            return False
        except HerdrUnavailable as exc:
            log.warning("resync skipped, herdr unreachable: %s", exc)
            self._schedule_resync()
            return False
        subscribed_pane_ids = self._snapshot_pane_ids(snap)
        by_terminal = {
            a["terminal_id"]: pid
            for pid, a in records.items()
            if a.get("terminal_id")
        }
        self._backfill_legacy_terminals(records)
        self._reconcile_books(records, by_terminal)
        self._registry = records
        for rec in records.values():
            self._remember_record(rec)
        for mapping in (
            self._last_probe,
            self._session_cache,
            self._meta_asserted_at,
        ):
            for pane_id in list(mapping):
                if pane_id not in records:
                    mapping.pop(pane_id, None)
        if (
            subscribed_pane_ids != self._subscribed_pane_ids
            and self._stream is not None
        ):
            self._stream.close()
            self._stream = None  # run loop re-bootstraps with the new pane set
        self._publish()
        return True

    # ---------- assert / release primitives ----------

    def _foreign_session_owner(self, pane_id: str) -> str | None:
        """Return a session owner that herdwatch must not displace.

        Herdr 0.7.3 rejects a custom lifecycle authority while an official
        integration owns the pane session, but still answers `ok`. More
        importantly, releasing our rejected source can clear the official
        pane detection. Treat such panes as metadata-only.
        """
        session = (self._registry.get(pane_id) or {}).get("agent_session")
        if not isinstance(session, dict):
            return None
        owner = session.get("source")
        if isinstance(owner, str) and owner and owner != SOURCE:
            return owner
        return None

    @staticmethod
    def _record_matches_hold(rec: dict, mp: ManagedPane) -> bool:
        return (
            rec.get("agent") == mp.agent
            and rec.get("agent_status") == "working"
            and rec.get("custom_status") == mp.custom_status
        )

    def _assert_hold(self, pane_id: str, agent: str, label: str) -> None:
        owner = self._foreign_session_owner(pane_id)
        if owner is not None:
            log.warning(
                "semantic hold unavailable for %s: herdr session is owned "
                "by %s; showing metadata only",
                pane_id,
                owner,
            )
            self._assert_metadata(pane_id, agent, label, "idle-meta")
            return
        outcome = self._client.report_agent(
            pane_id, SOURCE, agent, "working", label
        )
        if outcome is True:
            self.managed[pane_id] = ManagedPane(
                label,
                agent,
                kind="hold",
                terminal_id=self._terminal_id(pane_id),
            )
            self._adopted.discard(pane_id)
            log.info("hold %s -> %s (%s)", pane_id, label, agent)
        elif outcome is None:
            self.managed[pane_id] = ManagedPane(
                label,
                agent,
                kind="hold-pending",
                terminal_id=self._terminal_id(pane_id),
            )
            self._last_probe.pop(pane_id, None)
            log.warning("hold %s is awaiting readback verification", pane_id)
        else:
            # Do not record a failed write, and let the next sweep retry now.
            self._last_probe.pop(pane_id, None)

    def _assert_metadata(
        self, pane_id: str, agent: str, label: str, kind: str
    ) -> None:
        if self._client.report_metadata(
            pane_id,
            SOURCE,
            agent=agent,
            custom_status=label,
            ttl_ms=self._ttl_ms(kind),
        ):
            self.managed[pane_id] = ManagedPane(
                label,
                agent,
                kind=kind,
                terminal_id=self._terminal_id(pane_id),
            )
            self._adopted.discard(pane_id)
            self._meta_asserted_at[pane_id] = self._clock()
            log.info("%s %s -> %s (%s)", kind, pane_id, label, agent)
        else:
            self._last_probe.pop(pane_id, None)

    def _release_hold(self, pane_id: str, reason: str) -> bool:
        mp = self.managed.get(pane_id)
        if mp is None:
            return True
        if mp.kind == "hold-pending":
            observed = self._client.agent_get(pane_id)
            if observed is None:
                self._last_probe.pop(pane_id, None)
                log.warning(
                    "cannot verify pending hold %s for cleanup yet; keeping "
                    "it to retry",
                    pane_id,
                )
                return False
            self._registry[pane_id] = observed
            self._remember_record(observed)
            if not self._record_matches_hold(observed, mp):
                del self.managed[pane_id]
                self._adopted.discard(pane_id)
                log.info(
                    "dropping rejected pending hold %s without release (%s)",
                    pane_id,
                    reason,
                )
                return True
            mp.kind = "hold"
        owner = self._foreign_session_owner(pane_id)
        if owner is not None:
            # A visible foreign session proves our custom source is not the
            # effective authority under herdr 0.7.3. Releasing the unmatched
            # source can clear the official detection, so only forget the row.
            del self.managed[pane_id]
            self._adopted.discard(pane_id)
            log.warning(
                "dropping unowned hold %s without release; session belongs "
                "to %s (%s)",
                pane_id,
                owner,
                reason,
            )
            return True
        outcome = self._client.release_agent(pane_id, SOURCE, mp.agent)
        if outcome == "ok":
            del self.managed[pane_id]
            self._adopted.discard(pane_id)
            log.info("release %s (%s)", pane_id, reason)
            return True
        if outcome == "gone":
            # not_found can mean moved: keep bookkeeping until resync can
            # remap it by terminal_id or confirm that the pane is truly gone.
            self._schedule_resync(debounce=False)
            log.info(
                "release of %s answered not_found (%s); reconciling",
                pane_id,
                reason,
            )
            return False
        log.warning(
            "release of %s failed (%s); keeping to retry", pane_id, reason
        )
        return False

    def _clear_metadata(self, pane_id: str, reason: str) -> bool:
        mp = self.managed.get(pane_id)
        if mp is None:
            return True
        if self._client.report_metadata(
            pane_id, SOURCE, agent=mp.agent, clear_custom_status=True
        ):
            del self.managed[pane_id]
            self._meta_asserted_at.pop(pane_id, None)
            log.info("clear %s (%s)", pane_id, reason)
            return True
        log.warning(
            "metadata clear of %s failed (%s); keeping to retry", pane_id, reason
        )
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

    def _run_probes(self, ctx: PaneContext, *, fast: bool = False) -> str | None:
        pendings = []
        for probe in self._probes:
            try:
                result = probe.check(ctx)
            except Exception:
                log.warning(
                    "probe %r raised; treating as not pending",
                    getattr(probe, "name", probe),
                    exc_info=True,
                )
                result = None
            if result:
                pendings.append(result)
                if fast and result.source == "marker":
                    break
        return aggregate(pendings)

    def _fast_pending(self, pane_id: str) -> Pending | None:
        """Return a probe's pane-only result without git enrichment."""
        for probe in self._probes:
            check_pane = getattr(probe, "check_pane", None)
            if not callable(check_pane):
                continue
            try:
                result = check_pane(pane_id)
            except Exception:
                log.warning(
                    "fast probe %r raised; treating as not pending",
                    getattr(probe, "name", probe),
                    exc_info=True,
                )
                continue
            if result:
                return result
        return None

    def _fast_pending_candidates(self) -> set[str]:
        """Return panes advertised by cheap pane-only probes."""
        candidates = set()
        for probe in self._probes:
            candidate_panes = getattr(probe, "candidate_panes", None)
            if not callable(candidate_panes):
                continue
            try:
                candidates.update(candidate_panes())
            except Exception:
                log.warning(
                    "fast candidate probe %r raised; ignoring candidates",
                    getattr(probe, "name", probe),
                    exc_info=True,
                )
        return candidates

    def _probe_pane(
        self,
        pane_id: str,
        *,
        fast: bool = False,
        fast_only: bool = False,
        force: bool = False,
    ) -> bool:
        """Make the per-pane decision from the current registry record."""
        if not self._eligible(pane_id):
            return False
        now = self._clock()
        last = self._last_probe.get(pane_id)
        if not force and last is not None and (now - last) < self._reprobe:
            return False

        mp = self.managed.get(pane_id)
        rec = self._client.agent_get(pane_id)
        if rec is not None:
            self._registry[pane_id] = rec
            self._remember_record(rec)
        else:
            if mp is not None and mp.kind == "hold-pending":
                self._last_probe.pop(pane_id, None)
                return False
            rec = self._registry.get(pane_id)
            if rec is None:
                return False

        status = rec.get("agent_status") or "unknown"
        if mp is not None and mp.kind == "hold-pending":
            if self._record_matches_hold(rec, mp):
                mp.kind = "hold"
                self._adopted.discard(pane_id)
                log.info("verified pending hold %s", pane_id)
            else:
                del self.managed[pane_id]
                self._adopted.discard(pane_id)
                mp = None
                log.info("pending hold %s was not applied", pane_id)
        if mp is not None and mp.kind == "progress":
            # Recover from a missed working-to-idle/done event.
            if status != "working":
                if not self._clear_metadata(pane_id, "agent stopped"):
                    return False
                self._last_probe.pop(pane_id, None)
                mp = None
            else:
                return False

        pending = self._fast_pending(pane_id) if fast else None
        if fast_only and pending is None:
            return False
        if pending is not None:
            label = aggregate([pending])
            agent_name = rec.get("agent") or "agent"
        else:
            ctx = self._context(rec)
            label = self._run_probes(ctx, fast=fast)
            agent_name = ctx.agent
        # Throttle from completion, not start. A slow external probe can take
        # the whole interval; using its start time makes the same pane
        # immediately due again before the event stream gets another read.
        self._last_probe[pane_id] = self._clock()

        if mp is not None and mp.kind == "hold":
            if label:
                if mp.custom_status != label or pane_id in self._adopted:
                    self._assert_hold(pane_id, agent_name, label)
                return True
            if not self._release_hold(pane_id, "work cleared"):
                self._last_probe.pop(pane_id, None)
            return True

        if mp is not None and mp.kind == "idle-meta":
            if status == "idle":
                if label:
                    self._assert_metadata(
                        pane_id, agent_name, label, "idle-meta"
                    )
                elif not self._clear_metadata(pane_id, "work cleared"):
                    self._last_probe.pop(pane_id, None)
                return True
            if not self._clear_metadata(pane_id, f"left idle ({status})"):
                self._last_probe.pop(pane_id, None)
                return True
            mp = None

        if mp is not None and mp.kind == "done":
            if status == "done":
                if label:
                    self._assert_metadata(pane_id, agent_name, label, "done")
                elif not self._clear_metadata(pane_id, "work cleared"):
                    self._last_probe.pop(pane_id, None)
                return True
            # Clear the done label, then let an idle pane become a hold now.
            if not self._clear_metadata(pane_id, f"left done ({status})"):
                self._last_probe.pop(pane_id, None)
                return True
            mp = None

        if status == "idle":
            if label:
                self._assert_hold(pane_id, agent_name, label)
            return True
        if status == "done":
            if label:
                self._assert_metadata(pane_id, agent_name, label, "done")
            return True

        # Leave unmanaged working/blocked/unknown panes alone. Forget the
        # timer so the next idle/done edge probes immediately.
        self._last_probe.pop(pane_id, None)
        return True

    # ---------- sweeps ----------

    def _reprobe_sweep(
        self, yield_control: Callable[[], bool | None] | None = None
    ) -> None:
        def yield_now() -> bool:
            return yield_control is None or yield_control() is not False

        if not yield_now():
            self._publish()
            return
        for pane_id, mp in list(self._legacy_release.items()):
            owner = self._foreign_session_owner(pane_id)
            if owner is not None:
                del self._legacy_release[pane_id]
                log.warning(
                    "dropping legacy cleanup %s without release; session "
                    "belongs to %s",
                    pane_id,
                    owner,
                )
                if not yield_now():
                    self._publish()
                    return
                continue
            outcome = self._client.release_agent(pane_id, SOURCE, mp.agent)
            if outcome == "ok":
                del self._legacy_release[pane_id]
                log.info("released legacy assertion on %s", pane_id)
            elif outcome == "gone":
                self._schedule_resync(debounce=False)
            if not yield_now():
                self._publish()
                return

        for pane_id in list(self.managed):
            mp = self.managed.get(pane_id)
            if mp is None:
                if not yield_now():
                    self._publish()
                    return
                continue
            if not self._eligible(pane_id):
                if mp.kind in SEMANTIC_HOLD_KINDS:
                    self._release_hold(pane_id, "pane no longer eligible")
                else:
                    self._clear_metadata(pane_id, "pane no longer eligible")
                if not yield_now():
                    self._publish()
                    return
                continue
            self._probe_pane(pane_id)
            if not yield_now():
                self._publish()
                return

        for pane_id, rec in list(self._registry.items()):
            if pane_id in self.managed or not self._eligible(pane_id):
                if not yield_now():
                    self._publish()
                    return
                continue
            if rec.get("agent_status") in ("idle", "done"):
                self._probe_pane(pane_id)
            if not yield_now():
                self._publish()
                return
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
                continue
            if (
                rec.get("agent_status") != "working"
                or (rec.get("agent") or "") != "claude"
            ):
                continue
            session = (
                (rec.get("agent_session") or {}).get("value")
                or self._session_cache.get(pane_id)
            )
            if not session:
                continue
            try:
                label = self._progress(session)
            except Exception:
                log.warning("progress read failed; skipping", exc_info=True)
                continue
            if label:
                stale = (
                    now - self._meta_asserted_at.get(pane_id, 0.0)
                ) >= self._ttl_ms("progress") / 2000.0
                if mp is None or mp.custom_status != label or stale:
                    self._assert_metadata(
                        pane_id,
                        rec.get("agent") or "agent",
                        label,
                        "progress",
                    )
            elif mp is not None:
                self._clear_metadata(pane_id, "no active task")
        self._publish()

    # ---------- shutdown ----------

    def shutdown(self) -> None:
        """Clean up owned assertions, retaining rows for failed hold cleanup."""
        for pane_id, mp in list(self.managed.items()):
            try:
                if mp.kind in SEMANTIC_HOLD_KINDS:
                    self._release_hold(pane_id, "shutdown")
                else:
                    # Metadata self-expires via TTL; drop the row either way.
                    self._client.report_metadata(
                        pane_id,
                        SOURCE,
                        agent=mp.agent,
                        clear_custom_status=True,
                    )
                    del self.managed[pane_id]
            except Exception:
                log.warning(
                    "failed to clean %s on shutdown", pane_id, exc_info=True
                )

        for pane_id, mp in list(self._legacy_release.items()):
            try:
                owner = self._foreign_session_owner(pane_id)
                if owner is not None:
                    del self._legacy_release[pane_id]
                    continue
                if (
                    self._client.release_agent(pane_id, SOURCE, mp.agent)
                    == "ok"
                ):
                    del self._legacy_release[pane_id]
            except Exception:
                log.warning(
                    "failed to release legacy %s on shutdown",
                    pane_id,
                    exc_info=True,
                )
        self._publish()

    def run(self, sleep: Callable[[float], None] = time.sleep) -> None:
        atexit.register(self.shutdown)

        def _handle_term(signum, frame):
            self.shutdown()
            raise SystemExit(0)

        try:
            signal.signal(signal.SIGTERM, _handle_term)
        except (ValueError, OSError):
            pass

        selector = selectors.DefaultSelector()
        backoff = self._backoff_base
        registered = None
        connected_at = None
        fast_candidate_cursor = 0
        fresh_fast_candidates: set[str] = set()
        known_fast_candidates: set[str] = set()
        next_fast_candidate_scan = 0.0
        managed_reprobe_cursor = 0
        next_reprobe = next_progress = self._clock()
        next_resync = self._clock() + self._resync_interval

        def _drain_probe_events() -> bool:
            """Keep the non-blocking status stream moving between probes."""
            nonlocal backoff, fast_candidate_cursor
            nonlocal fresh_fast_candidates, known_fast_candidates
            nonlocal managed_reprobe_cursor, next_fast_candidate_scan
            stream = self._stream
            if stream is None or stream.closed:
                return False
            before_ids = set(self._registry)
            messages = stream.read_events(max_chunks=1)
            if messages:
                backoff = self._backoff_base
            for message in messages:
                self.dispatch_event(message)
            fast_candidate_checked = False
            if (
                self._stream is stream
                and not stream.closed
                and self._resync_due
                and self._resync_ready(self._clock())
                and self._resync()
            ):
                discovered = set(self._registry) - before_ids
                if self._stream is stream:
                    for pane_id in sorted(discovered):
                        rec = self._registry.get(pane_id) or {}
                        if rec.get("agent_status") in ("idle", "done"):
                            self._probe_pane(pane_id, fast=True)
            candidate_now = self._clock()
            if (
                self._stream is stream
                and not stream.closed
                and candidate_now >= next_fast_candidate_scan
            ):
                next_fast_candidate_scan = candidate_now + 1.0
                candidate_set = self._fast_pending_candidates()
                fresh_fast_candidates.intersection_update(candidate_set)
                fresh_fast_candidates.update(
                    candidate_set - known_fast_candidates
                )
                known_fast_candidates = candidate_set
                fresh = sorted(fresh_fast_candidates)
                regular = sorted(candidate_set - fresh_fast_candidates)
                if regular:
                    start = fast_candidate_cursor % len(regular)
                    rotated_regular = regular[start:] + regular[:start]
                else:
                    rotated_regular = []
                for pane_id in fresh + rotated_regular:
                    rec = self._registry.get(pane_id) or {}
                    if rec.get("agent_status") not in ("idle", "done"):
                        continue
                    force = pane_id in fresh_fast_candidates
                    if not force and regular:
                        fast_candidate_cursor = (
                            regular.index(pane_id) + 1
                        ) % len(regular)
                    fast_candidate_checked = self._probe_pane(
                        pane_id,
                        fast=True,
                        fast_only=True,
                        force=force,
                    )
                    fresh_fast_candidates.discard(pane_id)
                    break
            if (
                self._stream is stream
                and not stream.closed
                and self._reprobe > 0
                and not fast_candidate_checked
            ):
                # A full sweep can outlive the reprobe interval when external
                # probes are slow. Recheck at most one due managed pane per
                # stream drain; rotating the starting point keeps the socket
                # responsive without starving a repeatedly failing pane.
                panes = list(self.managed)
                if panes:
                    now = self._clock()
                    for offset in range(len(panes)):
                        index = (
                            managed_reprobe_cursor + offset
                        ) % len(panes)
                        pane_id = panes[index]
                        last = self._last_probe.get(pane_id)
                        if last is not None and now - last < self._reprobe:
                            continue
                        managed_reprobe_cursor = (index + 1) % len(panes)
                        self._probe_pane(pane_id)
                        break
            return self._stream is stream and not stream.closed

        while True:
            try:
                if self._stream is None:
                    if registered is not None:
                        try:
                            selector.unregister(registered)
                        except (KeyError, ValueError):
                            pass
                        registered = None
                    if self.bootstrap():
                        stream = self._stream
                        selector.register(stream, selectors.EVENT_READ)
                        registered = stream
                        self._last_probe.clear()
                        self._resync_due = False
                        self._resync_not_before = None
                        catchup_outcome, drained = self._drain_startup_replay(
                            selector, stream
                        )
                        if catchup_outcome == "closed":
                            try:
                                selector.unregister(stream)
                            except (KeyError, ValueError):
                                pass
                            registered = None
                            stream.close()
                            if self._stream is stream:
                                self._stream = None
                            sleep(backoff)
                            backoff = min(backoff * 2, self._backoff_max)
                            continue
                        if catchup_outcome == "timeout":
                            log.warning(
                                "startup replay did not become quiet after "
                                "%.1fs; continuing with replay-safe handlers",
                                self._startup_replay_max,
                            )
                        if drained:
                            log.info(
                                "drained %d retained startup events", drained
                            )
                        if catchup_outcome in ("quiet", "timeout"):
                            if not self._resync():
                                try:
                                    selector.unregister(stream)
                                except (KeyError, ValueError):
                                    pass
                                registered = None
                                stream.close()
                                if self._stream is stream:
                                    self._stream = None
                                sleep(backoff)
                                backoff = min(
                                    backoff * 2, self._backoff_max
                                )
                                connected_at = None
                                continue
                            if self._stream is not stream:
                                try:
                                    selector.unregister(stream)
                                except (KeyError, ValueError):
                                    pass
                                registered = None
                                stream.close()
                                connected_at = None
                                continue
                        connected_at = self._clock()
                        self._reprobe_sweep(_drain_probe_events)
                        if self._stream is not stream or stream.closed:
                            unexpected_close = (
                                self._stream is stream and stream.closed
                            )
                            try:
                                selector.unregister(stream)
                            except (KeyError, ValueError):
                                pass
                            registered = None
                            stream.close()
                            if self._stream is stream:
                                self._stream = None
                            if unexpected_close:
                                if (
                                    connected_at is not None
                                    and self._clock() - connected_at
                                    >= self._backoff_base
                                ):
                                    backoff = self._backoff_base
                                sleep(backoff)
                                backoff = min(
                                    backoff * 2, self._backoff_max
                                )
                            connected_at = None
                            continue
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
                deadlines = [next_reprobe, next_progress, next_resync]
                if self._resync_due:
                    deadlines.append(
                        self._resync_not_before
                        if self._resync_not_before is not None
                        else now
                    )
                timeout = max(
                    0.0,
                    min(deadlines) - now,
                )
                ready = selector.select(timeout)
                processed_event = False
                if ready:
                    stream = self._stream
                    messages = stream.read_events(max_chunks=1)
                    processed_event = bool(messages)
                    for message in messages:
                        self.dispatch_event(message)
                    observed_at = self._clock()
                    was_stable = (
                        connected_at is not None
                        and observed_at - connected_at >= self._backoff_base
                    )
                    if processed_event or was_stable:
                        backoff = self._backoff_base
                    if self._stream is not stream or stream.closed:
                        unexpected_close = self._stream is stream and stream.closed
                        try:
                            selector.unregister(stream)
                        except (KeyError, ValueError):
                            pass
                        registered = None
                        stream.close()
                        if self._stream is stream:
                            self._stream = None
                        if unexpected_close:
                            if (
                                connected_at is not None
                                and self._clock() - connected_at
                                >= self._backoff_base
                            ):
                                backoff = self._backoff_base
                            sleep(backoff)
                            backoff = min(backoff * 2, self._backoff_max)
                        connected_at = None
                        continue

                if self._resync_ready(self._clock()):
                    self._resync()
                    if self._stream is None:
                        continue

                now = self._clock()
                if now >= next_reprobe:
                    stream = self._stream
                    self._reprobe_sweep(_drain_probe_events)
                    if self._stream is not stream or stream.closed:
                        unexpected_close = (
                            self._stream is stream and stream.closed
                        )
                        try:
                            selector.unregister(stream)
                        except (KeyError, ValueError):
                            pass
                        registered = None
                        stream.close()
                        if self._stream is stream:
                            self._stream = None
                        if unexpected_close:
                            sleep(backoff)
                            backoff = min(
                                backoff * 2, self._backoff_max
                            )
                        connected_at = None
                        continue
                    next_reprobe = self._clock() + self._reprobe
                if now >= next_progress:
                    self._progress_sweep()
                    next_progress = now + self._progress_interval
                if now >= next_resync:
                    self._resync()
                    next_resync = now + self._resync_interval
                # Merely accepting a subscription is not enough to reset the
                # backoff: a restarting server can ack and immediately close.
                # Require either a successfully processed event or a stream
                # that has stayed up through at least one base-backoff period.
                stream_is_healthy = self._stream is not None and not self._stream.closed
                stable_for_base = (
                    connected_at is not None
                    and now - connected_at >= self._backoff_base
                )
                if stream_is_healthy and (processed_event or stable_for_base):
                    backoff = self._backoff_base
            except SystemExit:
                raise
            except Exception:
                log.exception("daemon loop iteration failed; continuing")
                sleep(1.0)


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
        probes.append(
            BgJobsProbe(
                process_info=client.pane_process_info,
                min_age_s=config.bgjobs_min_age_s,
                extra_ignore=config.bgjobs_ignore,
            )
        )
    return Daemon(
        client,
        probes,
        reprobe_interval_s=config.reprobe_interval_s,
        resync_interval_s=config.resync_interval_s,
        progress_interval_s=config.progress_interval_s,
        allow=config.allow,
        deny=config.deny,
        on_snapshot=StateStore().write,
        progress=progress_label if config.progress_enabled else None,
        stream_factory=lambda subscriptions: herdr_socket.EventStream(subscriptions),
    )
