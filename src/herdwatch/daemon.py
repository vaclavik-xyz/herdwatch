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
from .markers import MarkerStore
from .models import PaneContext
from .probes.bgjobs import BgJobsProbe
from .probes.ci import CIProbe
from .probes.marker import MarkerProbe
from .probes.roborev import RoborevProbe
from .state import StateStore

SOURCE = "herdwatch"
MARKER_DIR = os.path.expanduser("~/.local/state/herdwatch/markers")
log = logging.getLogger(__name__)


@dataclass
class ManagedPane:
    custom_status: str
    agent: str
    kind: str = "hold"  # "hold" = ⏳ wait assertion, "progress" = task-list label


class Daemon:
    def __init__(self, client, probes, reprobe_interval_s: float = 15.0,
                 clock: Callable[[], float] = time.time,
                 enrich: Callable[[str], gitctx.GitInfo] = gitctx.enrich,
                 allow: list[str] | None = None,
                 deny: list[str] | None = None,
                 on_snapshot: Callable[[list[dict]], None] = lambda rows: None) -> None:
        self._client = client
        self._probes = probes
        self._reprobe = reprobe_interval_s
        self._clock = clock
        self._enrich = enrich
        self._allow = set(allow or [])
        self._deny = set(deny or [])
        self._on_snapshot = on_snapshot
        self.managed: dict[str, ManagedPane] = {}
        self._last_probe: dict[str, float] = {}
        # panes recovered from a prior run: force one re-assert to herdr on the
        # first probe, since herdr may have lost the old assertion while we were down
        self._adopted: set[str] = set()

    def _rows(self) -> list[dict]:
        return [{"pane_id": pid, "agent": mp.agent, "status": mp.custom_status,
                 "kind": mp.kind}
                for pid, mp in sorted(self.managed.items())]

    def adopt(self, rows: list[dict]) -> None:
        """Recover the managed set from a prior run's state snapshot so a restart
        reconciles the panes we were holding: the next tick re-probes each and
        either re-asserts or releases it, instead of leaving an orphaned
        `working ⏳` assertion behind after an unclean shutdown."""
        for row in rows:
            pane_id = row.get("pane_id")
            if pane_id:
                self.managed[pane_id] = ManagedPane(row.get("status", ""),
                                                    row.get("agent", "agent"),
                                                    kind=row.get("kind", "hold"))
                self._adopted.add(pane_id)

    def _publish(self) -> None:
        try:
            self._on_snapshot(self._rows())
        except Exception:
            log.warning("snapshot publish failed; continuing", exc_info=True)

    def _release(self, pane_id: str, reason: str) -> bool:
        """Release our assertion and drop bookkeeping only if herdr confirmed it,
        so a failed release (herdr down/restarting) is retried instead of
        orphaning a `working ⏳` session. Returns whether the pane is now clear."""
        mp = self.managed.get(pane_id)
        if mp is None:
            return True
        if self._client.release_agent(pane_id, SOURCE, mp.agent):
            del self.managed[pane_id]
            self._adopted.discard(pane_id)
            log.info("release %s (%s)", pane_id, reason)
            return True
        log.warning("release of %s failed (%s); keeping to retry", pane_id, reason)
        return False

    def _eligible(self, pane_id: str) -> bool:
        if self._deny and pane_id in self._deny:
            return False
        if self._allow and pane_id not in self._allow:
            return False
        return True

    def _context(self, agent: dict) -> PaneContext:
        cwd = agent.get("cwd") or agent.get("foreground_cwd") or ""
        gi = self._enrich(cwd)
        return PaneContext(
            pane_id=agent["pane_id"],
            agent=agent.get("agent") or "agent",
            cwd=cwd,
            status=agent.get("agent_status") or "unknown",
            head_sha=gi.head_sha,
            branch=gi.branch,
            is_git_repo=gi.is_git_repo,
            has_github_remote=gi.has_github_remote,
            worktree_heads=gi.worktree_heads,
        )

    def tick(self) -> None:
        seen = set()
        for agent in self._client.agent_list():
            pane_id = agent.get("pane_id")
            if not pane_id or not self._eligible(pane_id):
                continue
            seen.add(pane_id)
            status = agent.get("agent_status") or "unknown"
            managed = pane_id in self.managed
            if not managed and status not in ("idle", "done"):
                # not ours and busy: forget its timer so a fresh idle edge
                # probes immediately
                self._last_probe.pop(pane_id, None)
                continue
            now = self._clock()
            last = self._last_probe.get(pane_id)
            if last is not None and (now - last) < self._reprobe:
                continue
            ctx = self._context(agent)
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
            self._last_probe[pane_id] = now
            label = aggregate(pendings)
            if label:
                agent_name = ctx.agent
                if (managed and self.managed[pane_id].custom_status == label
                        and pane_id not in self._adopted):
                    pass  # already asserting this exact label; nothing to do
                elif self._client.report_agent(pane_id, SOURCE, agent_name, "working", label):
                    self.managed[pane_id] = ManagedPane(label, agent_name)
                    self._adopted.discard(pane_id)
                    log.info("hold %s -> %s (%s)", pane_id, label, agent_name)
                else:
                    # report failed (herdr down): don't record, and don't let the
                    # throttle defer the retry to the next reprobe interval
                    self._last_probe.pop(pane_id, None)
            elif managed:
                if not self._release(pane_id, "work cleared"):
                    self._last_probe.pop(pane_id, None)  # failed release -> retry promptly
        # a managed pane that vanished from agent_list: release our assertion so
        # we never orphan a `working ⏳` session in herdr (agent exited, pane
        # closed, or herdr restarted/blipped). Keep bookkeeping on failure so the
        # release is retried next tick instead of leaking the assertion.
        for pane_id in list(self.managed):
            if pane_id not in seen:
                self._release(pane_id, "vanished from agent list")
        for pane_id in list(self._last_probe):
            if pane_id not in seen:
                self._last_probe.pop(pane_id, None)
        self._publish()

    def release_all(self) -> None:
        """Release every pane herdwatch currently asserts (clean shutdown)."""
        for pane_id, mp in list(self.managed.items()):
            try:
                self._client.release_agent(pane_id, SOURCE, mp.agent)
            except Exception:
                log.warning("failed to release %s on shutdown", pane_id, exc_info=True)
        self.managed.clear()
        self._publish()

    def run(self, poll_interval_s: float, sleep: Callable[[float], None] = time.sleep) -> None:
        atexit.register(self.release_all)

        def _handle_term(signum, frame):
            self.release_all()
            raise SystemExit(0)

        try:
            signal.signal(signal.SIGTERM, _handle_term)
        except (ValueError, OSError):
            pass  # not main thread; atexit still covers shutdown
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("tick failed; continuing")
            sleep(poll_interval_s)


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
    return Daemon(client, probes, reprobe_interval_s=config.reprobe_interval_s,
                  allow=config.allow, deny=config.deny,
                  on_snapshot=StateStore().write)
