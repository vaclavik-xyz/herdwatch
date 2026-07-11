# src/herdwatch/daemon.py
from __future__ import annotations

import atexit
import logging
import os
import signal
import time
from dataclasses import dataclass
from typing import Callable

from . import gitctx
from .aggregate import aggregate
from .cache import TTLCache
from .config import Config
from .herdr import HerdrClient
from .herdr_socket import HerdrApiError, HerdrUnavailable
from .markers import MarkerStore
from .models import PaneContext
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
    {"type": "pane.agent_detected"},
    {"type": "pane.moved"},
    {"type": "workspace.closed"},
    {"type": "tab.closed"},
]
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
log = logging.getLogger(__name__)


@dataclass
class ManagedPane:
    custom_status: str
    agent: str
    kind: str = "hold"  # "hold" | "progress" | "done"
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
        clock: Callable[[], float] = time.time,
        enrich: Callable[[str], gitctx.GitInfo] = gitctx.enrich,
        allow: list[str] | None = None,
        deny: list[str] | None = None,
        on_snapshot: Callable[[list[dict]], None] = lambda rows: None,
        progress: Callable[[str], str | None] | None = None,
        stream_factory=None,
        backoff_base_s: float = 0.5,
        backoff_max_s: float = 30.0,
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
        self.managed: dict[str, ManagedPane] = {}
        # Pre-migration semantic assertions awaiting release (see adopt).
        self._legacy_release: dict[str, ManagedPane] = {}
        # Last known agent records, keyed by pane_id (snapshot is truth).
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
                "meta": mp.kind in ("progress", "done"),
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
            if mp.kind == "hold":
                self.managed[pane_id] = mp
                self._adopted.add(pane_id)
            elif not row.get("meta"):
                self._legacy_release[pane_id] = mp

    # ---------- event dispatch ----------

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
            if (
                status == expected
                and (data.get("custom_status") or "") == mp.custom_status
            ):
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
        # by pane_id). A later resync cannot notice -- the registry is
        # already remapped -- so tear the stream down here; the run loop
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
            if mp.kind == "hold":
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

    def _reconcile_books(
        self, records: dict[str, dict], by_terminal: dict[str, str]
    ) -> None:
        """Move reconciliation + vanish handling for managed and legacy rows
        against snapshot truth. Shared by _resync and bootstrap (so adopted
        rows are reconciled BEFORE the first sweep can release stale ids)."""
        for book in (self.managed, self._legacy_release):
            for pane_id in list(book):
                if pane_id in records:
                    continue
                mp = book[pane_id]
                new_id = (
                    by_terminal.get(mp.terminal_id) if mp.terminal_id else None
                )
                if (
                    new_id is None
                    and not mp.terminal_id
                    and book is self._legacy_release
                ):
                    # label-match salvage (legacy rows only): a move before
                    # our first snapshot left no terminal_id; a unique
                    # (agent, custom_status) match among untracked panes is
                    # safe to adopt -- a mismatched target either has no
                    # herdwatch assertion (release is a no-op) or carries a
                    # herdwatch orphan (releasing it is also correct cleanup)
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
                if book is self.managed:
                    self._adopted.discard(pane_id)
                    # best-effort cleanup; the pane is gone, drop regardless
                    if mp.kind == "hold":
                        self._client.release_agent(pane_id, SOURCE, mp.agent)
                    else:
                        self._client.report_metadata(
                            pane_id,
                            SOURCE,
                            agent=mp.agent,
                            clear_custom_status=True,
                        )
                    log.info("dropped vanished pane %s (%s)", pane_id, mp.kind)

    def _resync(self) -> None:
        """Snapshot is truth: reconcile managed/legacy/registry against it.
        Never raises; herdr being down keeps all state for a later retry."""
        self._resync_due = False
        try:
            snap = self._client.session_snapshot()
        except HerdrApiError as exc:
            log.error(
                "herdwatch requires herdr >= 0.7.2 with session.snapshot "
                "(server said: %s)",
                exc,
            )
            return
        except HerdrUnavailable as exc:
            log.warning("resync skipped, herdr unreachable: %s", exc)
            return
        records = {
            a["pane_id"]: a
            for a in snap.get("agents", [])
            if a.get("pane_id")
        }
        # captured BEFORE reconciliation: _remap mutates the registry, and a
        # post-remap comparison would miss the pane-id change entirely
        before_ids = set(self._registry)
        by_terminal = {
            a["terminal_id"]: pid
            for pid, a in records.items()
            if a.get("terminal_id")
        }
        self._backfill_legacy_terminals(records)
        self._reconcile_books(records, by_terminal)
        pane_set_changed = set(records) != before_ids
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
        if pane_set_changed and self._stream is not None:
            self._stream.close()
            self._stream = None  # run loop re-bootstraps with the new pane set
        self._publish()

    # ---------- assert / release primitives ----------

    def _assert_hold(self, pane_id: str, agent: str, label: str) -> None:
        if self._client.report_agent(
            pane_id, SOURCE, agent, "working", label
        ):
            self.managed[pane_id] = ManagedPane(
                label,
                agent,
                kind="hold",
                terminal_id=self._terminal_id(pane_id),
            )
            self._adopted.discard(pane_id)
            log.info("hold %s -> %s (%s)", pane_id, label, agent)
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
            ttl_ms=self._ttl_ms(),
        ):
            self.managed[pane_id] = ManagedPane(
                label,
                agent,
                kind=kind,
                terminal_id=self._terminal_id(pane_id),
            )
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
            # not_found can mean moved: keep bookkeeping until resync can
            # remap it by terminal_id or confirm that the pane is truly gone.
            self._resync_due = True
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

    def _run_probes(self, ctx: PaneContext) -> str | None:
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
        return aggregate(pendings)

    def _probe_pane(self, pane_id: str) -> None:
        """Make the per-pane decision from the current registry record."""
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
            # Recover from a missed working-to-idle/done event.
            if status != "working":
                if not self._clear_metadata(pane_id, "agent stopped"):
                    return
                self._last_probe.pop(pane_id, None)
                mp = None
            else:
                return

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
            # Clear the done label, then let an idle pane become a hold now.
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

        # Leave unmanaged working/blocked/unknown panes alone. Forget the
        # timer so the next idle/done edge probes immediately.
        self._last_probe.pop(pane_id, None)

    # ---------- sweeps ----------

    def _reprobe_sweep(self) -> None:
        for pane_id, mp in list(self._legacy_release.items()):
            outcome = self._client.release_agent(pane_id, SOURCE, mp.agent)
            if outcome == "ok":
                del self._legacy_release[pane_id]
                log.info("released legacy assertion on %s", pane_id)
            elif outcome == "gone":
                self._resync_due = True

        for pane_id in list(self.managed):
            mp = self.managed.get(pane_id)
            if mp is None:
                continue
            if not self._eligible(pane_id):
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
                ) >= self._ttl_ms() / 2000.0
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
                if mp.kind == "hold":
                    if (
                        self._client.release_agent(pane_id, SOURCE, mp.agent)
                        == "ok"
                    ):
                        del self.managed[pane_id]
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

    def run(self):
        raise NotImplementedError("rebuilt in the run-loop task")


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
        allow=config.allow,
        deny=config.deny,
        on_snapshot=StateStore().write,
        progress=progress_label if config.progress_enabled else None,
    )
