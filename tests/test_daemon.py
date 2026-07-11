# tests/test_daemon.py
import selectors
import socket as _socket
import time

from herdwatch.daemon import Daemon, ManagedPane, SOURCE, TTL_MAX_MS, TTL_MIN_MS
from herdwatch.gitctx import GitInfo
from herdwatch.herdr_socket import HerdrApiError, HerdrUnavailable
from herdwatch.models import Pending, WorktreeHead


class FakeClient:
    """Socket-era fake: agents keyed by pane_id; snapshot/get read them."""

    def __init__(self, agents=None, report_ok=True, release_ok=True, meta_ok=True):
        self.agents = {a["pane_id"]: a for a in (agents or [])}
        self.reports = []
        self.releases = []
        self.metadata = []
        self._report_ok = report_ok
        self._release_result = "ok" if release_ok else "failed"
        self._meta_ok = meta_ok
        self.snapshot_error = None

    def set_agents(self, agents):
        self.agents = {a["pane_id"]: a for a in agents}

    def session_snapshot(self):
        if self.snapshot_error is not None:
            raise self.snapshot_error
        return {"agents": [dict(a) for a in self.agents.values()]}

    def agent_get(self, pane_id):
        agent = self.agents.get(pane_id)
        return dict(agent) if agent is not None else None

    def report_agent(self, pane_id, source, agent, state, custom_status=None):
        self.reports.append((pane_id, state, custom_status))
        return self._report_ok

    def release_agent(self, pane_id, source, agent):
        self.releases.append(pane_id)
        return self._release_result

    def report_metadata(
        self,
        pane_id,
        source,
        *,
        agent=None,
        custom_status=None,
        clear_custom_status=False,
        ttl_ms=None,
    ):
        self.metadata.append((pane_id, custom_status, clear_custom_status, ttl_ms))
        return self._meta_ok

    def pane_process_info(self, pane_id):
        return {}


class StaticProbe:
    name = "static"

    def __init__(self, result):
        self.result = result

    def check(self, ctx):
        return self.result


_ENRICH = lambda cwd: GitInfo(True, "sha", "main", True)


def _agent(pane="w1:p1", status="idle", agent="claude", term=None):
    return {
        "pane_id": pane,
        "terminal_id": term or f"term-{pane}",
        "agent_status": status,
        "agent": agent,
        "cwd": "/x",
    }


def _owned_agent(
    pane="w1:p1",
    status="idle",
    agent="claude",
    source="herdr:claude",
    session="session-1",
):
    record = _agent(pane, status, agent)
    record["agent_session"] = {
        "source": source,
        "agent": agent,
        "kind": "id",
        "value": session,
    }
    return record


def _claude_agent(
    pane="w1:p1",
    status="working",
    session="c00b128f-68c8-4643-82d6-2835c317517d",
):
    agent = _agent(pane, status)
    if session is not None:
        # herdr exposes agent_session only for idle/done panes, not working
        # ones; pass session=None to mimic a working pane without it
        agent["agent_session"] = {"value": session}
    return agent


def make_daemon(client, probes=(), **kw):
    kw.setdefault("clock", lambda: 0.0)
    kw.setdefault("enrich", _ENRICH)
    kw.setdefault("startup_replay_quiet_s", 0.0)
    return Daemon(client, list(probes), **kw)


def seed(d, client):
    """Load the fake's agents into the registry (what bootstrap/resync do)."""
    d._registry = {pane: dict(agent) for pane, agent in client.agents.items()}
    d._subscribed_pane_ids = set(d._registry)
    for rec in d._registry.values():
        d._remember_record(rec)


def test_context_carries_worktree_heads_to_probes():
    heads = (
        WorktreeHead(head_sha="sha", branch="main"),
        WorktreeHead(head_sha="wt", branch="feat/x"),
    )
    seen = []

    class Capture:
        name = "capture"

        def check(self, ctx):
            seen.append(ctx.worktree_heads)
            return None

    client = FakeClient([_agent(status="idle")])
    d = make_daemon(
        client,
        [Capture()],
        enrich=lambda cwd: GitInfo(True, "sha", "main", True, heads),
    )
    seed(d, client)
    d._reprobe_sweep()
    assert seen == [heads]


def test_asserts_working_when_pending():
    client = FakeClient([_agent(status="idle")])
    d = make_daemon(client, [StaticProbe(Pending("review", 30, "roborev"))])
    seed(d, client)
    d._reprobe_sweep()
    assert client.reports == [("w1:p1", "working", "⏳ review")]
    assert "w1:p1" in d.managed
    assert d.managed["w1:p1"].kind == "hold"
    assert d.managed["w1:p1"].terminal_id == "term-w1:p1"


def test_foreign_session_owner_gets_idle_metadata_without_lifecycle_claim():
    client = FakeClient([_owned_agent()])
    d = make_daemon(
        client,
        [StaticProbe(Pending("review", 30, "roborev"))],
        reprobe_interval_s=0,
    )
    seed(d, client)

    d._reprobe_sweep()

    assert client.reports == []
    assert client.releases == []
    assert client.metadata == [("w1:p1", "⏳ review", False, 1000)]
    assert d.managed["w1:p1"].kind == "idle-meta"
    assert d._rows()[0]["meta"] is True


def test_foreign_session_idle_metadata_clears_without_lifecycle_release():
    client = FakeClient([_owned_agent()])
    probe = StaticProbe(Pending("review", 30, "roborev"))
    d = make_daemon(client, [probe], reprobe_interval_s=0)
    seed(d, client)
    d._reprobe_sweep()

    probe.result = None
    d._reprobe_sweep()

    assert client.releases == []
    assert client.metadata[-1] == ("w1:p1", None, True, None)
    assert "w1:p1" not in d.managed


def test_adopted_hold_with_foreign_session_drops_without_lifecycle_release():
    client = FakeClient([_owned_agent()])
    d = make_daemon(client, [StaticProbe(None)], reprobe_interval_s=0)
    d.adopt(
        [
            {
                "pane_id": "w1:p1",
                "agent": "claude",
                "status": "⏳ review",
                "kind": "hold",
            }
        ]
    )
    seed(d, client)

    d._reprobe_sweep()

    assert client.releases == []
    assert "w1:p1" not in d.managed


def test_ignores_working_pane_not_managed():
    client = FakeClient([_agent(status="working")])
    d = make_daemon(client, [StaticProbe(Pending("x", 10, "marker"))])
    seed(d, client)
    d._reprobe_sweep()
    assert client.reports == []


def test_releases_when_cleared():
    client = FakeClient([_agent(status="idle")])
    probe = StaticProbe(Pending("review", 30, "roborev"))
    d = make_daemon(client, [probe], reprobe_interval_s=0)
    seed(d, client)
    d._reprobe_sweep()
    probe.result = None
    d._reprobe_sweep()
    assert client.releases == ["w1:p1"]
    assert "w1:p1" not in d.managed


def test_reasserts_only_on_label_change():
    client = FakeClient([_agent(status="idle")])
    probe = StaticProbe(Pending("review", 30, "roborev"))
    d = make_daemon(client, [probe], reprobe_interval_s=0)
    seed(d, client)
    d._reprobe_sweep()
    d._reprobe_sweep()
    assert len(client.reports) == 1
    probe.result = Pending("CI: ci", 20, "ci")
    d._reprobe_sweep()
    assert client.reports[-1] == ("w1:p1", "working", "⏳ CI: ci")


def test_raising_probe_does_not_crash_sweep():
    class Boom:
        name = "boom"

        def check(self, ctx):
            raise RuntimeError("probe exploded")

    client = FakeClient([_agent(status="idle")])
    d = make_daemon(client, [Boom()])
    seed(d, client)
    d._reprobe_sweep()
    assert client.reports == []
    assert "w1:p1" not in d.managed


def test_reprobe_throttle_skips_within_interval():
    now = [0.0]
    calls = []

    class Counting:
        name = "counting"

        def check(self, ctx):
            calls.append(now[0])
            return Pending("review", 30, "roborev")

    client = FakeClient([_agent(status="idle")])
    d = make_daemon(
        client, [Counting()], reprobe_interval_s=15, clock=lambda: now[0]
    )
    seed(d, client)
    d._reprobe_sweep()
    assert len(calls) == 1
    now[0] = 5.0
    d._reprobe_sweep()
    assert len(calls) == 1
    now[0] = 20.0
    d._reprobe_sweep()
    assert len(calls) == 2


def test_managed_pane_released_when_cleared_even_if_status_working():
    client = FakeClient([_agent(status="idle")])
    probe = StaticProbe(Pending("review", 30, "roborev"))
    d = make_daemon(client, [probe], reprobe_interval_s=0)
    seed(d, client)
    d._reprobe_sweep()
    d._registry["w1:p1"]["agent_status"] = "working"
    probe.result = None
    d._reprobe_sweep()
    assert client.releases == ["w1:p1"]
    assert "w1:p1" not in d.managed


def test_deny_skips_pane():
    client = FakeClient([_agent(status="idle")])
    d = make_daemon(
        client,
        [StaticProbe(Pending("review", 30, "roborev"))],
        deny=["w1:p1"],
    )
    seed(d, client)
    d._reprobe_sweep()
    assert client.reports == []
    assert d.managed == {}


def test_reprobe_sweep_yields_before_and_between_panes():
    client = FakeClient(
        [_agent(pane="w1:p1", status="idle"), _agent(pane="w1:p2", status="idle")]
    )
    order = []

    class Probe:
        name = "ordered"

        def check(self, ctx):
            order.append(f"probe:{ctx.pane_id}")
            return None

    d = make_daemon(client, [Probe()], reprobe_interval_s=0)
    seed(d, client)

    d._reprobe_sweep(lambda: order.append("yield"))

    assert order == [
        "yield",
        "probe:w1:p1",
        "yield",
        "probe:w1:p2",
        "yield",
    ]


def test_reprobe_sweep_stops_when_yield_reports_closed_stream():
    client = FakeClient(
        [_agent(pane="w1:p1", status="idle"), _agent(pane="w1:p2", status="idle")]
    )
    probed = []
    yields = {"count": 0}

    class Probe:
        name = "ordered"

        def check(self, ctx):
            probed.append(ctx.pane_id)
            return None

    def yield_control():
        yields["count"] += 1
        return yields["count"] < 2

    d = make_daemon(client, [Probe()], reprobe_interval_s=0)
    seed(d, client)

    d._reprobe_sweep(yield_control)

    assert probed == ["w1:p1"]


def test_allow_only_listed():
    client = FakeClient([_agent(status="idle")])
    d = make_daemon(
        client,
        [StaticProbe(Pending("review", 30, "roborev"))],
        allow=["w2:p2"],
    )
    seed(d, client)
    d._reprobe_sweep()
    assert client.reports == []


def test_unmanaged_idle_pane_is_throttled():
    now = [0.0]
    calls = []

    class Counting:
        name = "counting"

        def check(self, ctx):
            calls.append(now[0])
            return None

    client = FakeClient([_agent(status="idle")])
    d = make_daemon(
        client, [Counting()], reprobe_interval_s=15, clock=lambda: now[0]
    )
    seed(d, client)
    d._reprobe_sweep()
    assert len(calls) == 1
    now[0] = 5.0
    d._reprobe_sweep()
    assert len(calls) == 1
    now[0] = 20.0
    d._reprobe_sweep()
    assert len(calls) == 2


def test_failed_release_keeps_pane_for_retry():
    client = FakeClient([_agent(status="idle")], release_ok=False)
    probe = StaticProbe(Pending("review", 30, "roborev"))
    d = make_daemon(client, [probe], reprobe_interval_s=0)
    seed(d, client)
    d._reprobe_sweep()
    probe.result = None
    d._reprobe_sweep()
    assert "w1:p1" in d.managed
    client._release_result = "ok"
    d._reprobe_sweep()
    assert "w1:p1" not in d.managed


def test_failed_report_does_not_record_managed():
    client = FakeClient([_agent(status="idle")], report_ok=False)
    d = make_daemon(client, [StaticProbe(Pending("review", 30, "roborev"))])
    seed(d, client)
    d._reprobe_sweep()
    assert d.managed == {}


def test_failed_report_is_not_throttled():
    client = FakeClient([_agent(status="idle")], report_ok=False)
    d = make_daemon(
        client,
        [StaticProbe(Pending("review", 30, "roborev"))],
        reprobe_interval_s=15,
    )
    seed(d, client)
    d._reprobe_sweep()
    assert d.managed == {}
    client._report_ok = True
    d._reprobe_sweep()
    assert "w1:p1" in d.managed


def test_unverified_report_is_tracked_until_readback_confirms_hold():
    client = FakeClient([_agent(status="idle")], report_ok=None)
    probe = StaticProbe(Pending("review", 30, "roborev"))
    d = make_daemon(client, [probe], reprobe_interval_s=0)
    seed(d, client)

    d._reprobe_sweep()

    assert d.managed["w1:p1"].kind == "hold-pending"
    assert d._rows()[0]["meta"] is False
    assert client.releases == []

    client.agents["w1:p1"].update(
        agent_status="working", custom_status="⏳ review"
    )
    d._reprobe_sweep()
    assert d.managed["w1:p1"].kind == "hold"

    probe.result = None
    d._reprobe_sweep()
    assert client.releases == ["w1:p1"]
    assert d.managed == {}


def test_unverified_report_waits_when_readback_remains_unavailable():
    client = FakeClient([_agent(status="idle")], report_ok=None)
    d = make_daemon(
        client,
        [StaticProbe(Pending("review", 30, "roborev"))],
        reprobe_interval_s=0,
    )
    seed(d, client)
    d._reprobe_sweep()
    client.set_agents([])

    d._reprobe_sweep()

    assert d.managed["w1:p1"].kind == "hold-pending"
    assert client.releases == []


def test_unverified_report_is_confirmed_by_matching_status_event():
    client = FakeClient([_agent(status="idle")], report_ok=None)
    d = make_daemon(
        client,
        [StaticProbe(Pending("review", 30, "roborev"))],
        reprobe_interval_s=0,
    )
    seed(d, client)
    d._reprobe_sweep()
    client.agents["w1:p1"].update(
        agent_status="working", custom_status="⏳ review"
    )

    d.dispatch_event(_status_event(status="working", custom="⏳ review"))

    assert d.managed["w1:p1"].kind == "hold"


def test_unverified_report_rejects_stale_matching_status_event():
    client = FakeClient([_agent(status="idle")], report_ok=None)
    d = make_daemon(
        client,
        [StaticProbe(Pending("review", 30, "roborev"))],
        reprobe_interval_s=0,
    )
    seed(d, client)
    d._reprobe_sweep()

    d.dispatch_event(_status_event(status="working", custom="⏳ review"))

    assert d.managed["w1:p1"].kind == "hold-pending"
    assert d._registry["w1:p1"]["agent_status"] == "idle"
    assert client.releases == []


def test_unverified_report_ignores_matching_label_from_different_agent():
    client = FakeClient([_agent(status="idle")], report_ok=None)
    d = make_daemon(
        client,
        [StaticProbe(Pending("review", 30, "roborev"))],
        reprobe_interval_s=0,
    )
    seed(d, client)
    d._reprobe_sweep()

    d.dispatch_event(
        _status_event(
            status="working", custom="⏳ review", agent="codex"
        )
    )

    assert d.managed["w1:p1"].kind == "hold-pending"
    assert client.releases == []


def test_shutdown_retains_unverified_hold_when_readback_is_unavailable():
    snapshots = []
    client = FakeClient([_agent(status="idle")], report_ok=None)
    d = make_daemon(
        client,
        [StaticProbe(Pending("review", 30, "roborev"))],
        reprobe_interval_s=0,
        on_snapshot=snapshots.append,
    )
    seed(d, client)
    d._reprobe_sweep()
    client.set_agents([])

    d.shutdown()

    assert client.releases == []
    assert d.managed["w1:p1"].kind == "hold-pending"
    assert snapshots[-1][0]["meta"] is False


def test_unverified_report_falls_back_when_foreign_owner_becomes_visible():
    client = FakeClient([_agent(status="idle")], report_ok=None)
    d = make_daemon(
        client,
        [StaticProbe(Pending("review", 30, "roborev"))],
        reprobe_interval_s=0,
    )
    seed(d, client)
    d._reprobe_sweep()
    client.set_agents([_owned_agent()])

    d._reprobe_sweep()

    assert client.releases == []
    assert client.metadata[-1] == ("w1:p1", "⏳ review", False, 1000)
    assert d.managed["w1:p1"].kind == "idle-meta"


def test_failed_work_cleared_release_is_not_throttled():
    now = [0.0]
    client = FakeClient([_agent(status="idle")], release_ok=False)
    probe = StaticProbe(Pending("review", 30, "roborev"))
    d = make_daemon(
        client, [probe], reprobe_interval_s=15, clock=lambda: now[0]
    )
    seed(d, client)
    d._reprobe_sweep()
    probe.result = None
    now[0] = 20.0
    d._reprobe_sweep()
    assert "w1:p1" in d.managed
    client._release_result = "ok"
    d._reprobe_sweep()
    assert "w1:p1" not in d.managed


def test_reprobe_sweep_snapshots_managed_rows():
    client = FakeClient([_agent(status="idle")])
    snaps = []
    d = make_daemon(
        client,
        [StaticProbe(Pending("review", 30, "roborev"))],
        on_snapshot=snaps.append,
    )
    seed(d, client)
    d._reprobe_sweep()
    assert snaps[-1] == [
        {
            "pane_id": "w1:p1",
            "agent": "claude",
            "status": "⏳ review",
            "kind": "hold",
            "terminal_id": "term-w1:p1",
            "meta": False,
        }
    ]


def test_reprobe_sweep_snapshots_empty_when_nothing_held():
    client = FakeClient([_agent(status="idle")])
    snaps = []
    d = make_daemon(client, [StaticProbe(None)], on_snapshot=snaps.append)
    seed(d, client)
    d._reprobe_sweep()
    assert snaps[-1] == []


def test_shutdown_snapshots_empty_after_successful_cleanup():
    client = FakeClient([_agent(status="idle")])
    snaps = []
    d = make_daemon(
        client,
        [StaticProbe(Pending("review", 30, "roborev"))],
        reprobe_interval_s=0,
        on_snapshot=snaps.append,
    )
    seed(d, client)
    d._reprobe_sweep()
    d.shutdown()
    assert snaps[-1] == []


def test_raising_snapshot_does_not_crash_sweep():
    def boom(rows):
        raise RuntimeError("disk full")

    client = FakeClient([_agent(status="idle")])
    d = make_daemon(
        client,
        [StaticProbe(Pending("review", 30, "roborev"))],
        on_snapshot=boom,
    )
    seed(d, client)
    d._reprobe_sweep()
    assert "w1:p1" in d.managed


def test_adopt_ignores_rows_without_pane_id():
    d = make_daemon(FakeClient([]))
    d.adopt(
        [
            {"agent": "claude", "status": "⏳ x"},
            {"pane_id": "", "agent": "claude"},
        ]
    )
    assert d.managed == {}
    assert d._legacy_release == {}


def test_adopt_defaults_kind_to_hold():
    d = make_daemon(FakeClient([]))
    d.adopt([{"pane_id": "w1:p1", "agent": "claude", "status": "⏳ review"}])
    assert d.managed["w1:p1"].kind == "hold"


def test_adopted_pane_released_when_work_already_cleared():
    client = FakeClient([_agent(status="idle")])
    d = make_daemon(client, [StaticProbe(None)], reprobe_interval_s=0)
    d.adopt([{"pane_id": "w1:p1", "agent": "claude", "status": "⏳ review"}])
    seed(d, client)
    d._reprobe_sweep()
    assert client.releases == ["w1:p1"]
    assert "w1:p1" not in d.managed


def test_adopted_pane_kept_when_still_pending():
    client = FakeClient([_agent(status="idle")])
    d = make_daemon(
        client,
        [StaticProbe(Pending("review", 30, "roborev"))],
        reprobe_interval_s=0,
    )
    d.adopt([{"pane_id": "w1:p1", "agent": "claude", "status": "⏳ review"}])
    seed(d, client)
    d._reprobe_sweep()
    assert "w1:p1" in d.managed
    assert client.releases == []


def test_adopted_pending_pane_is_reasserted_once():
    client = FakeClient([_agent(status="idle")])
    d = make_daemon(
        client,
        [StaticProbe(Pending("review", 30, "roborev"))],
        reprobe_interval_s=0,
    )
    d.adopt([{"pane_id": "w1:p1", "agent": "claude", "status": "⏳ review"}])
    seed(d, client)
    d._reprobe_sweep()
    assert client.reports == [("w1:p1", "working", "⏳ review")]
    d._reprobe_sweep()
    assert len(client.reports) == 1


def test_done_pane_gets_metadata_not_hold():
    client = FakeClient([_agent(status="done")])
    d = make_daemon(client, [StaticProbe(Pending("review", 30, "roborev"))])
    seed(d, client)
    d._reprobe_sweep()
    assert client.reports == []
    assert client.metadata == [("w1:p1", "⏳ review", False, 30000)]
    assert d.managed["w1:p1"].kind == "done"


def test_done_metadata_cleared_when_work_clears():
    client = FakeClient([_agent(status="done")])
    probe = StaticProbe(Pending("review", 30, "roborev"))
    d = make_daemon(client, [probe], reprobe_interval_s=0)
    seed(d, client)
    d._reprobe_sweep()
    probe.result = None
    d._reprobe_sweep()
    assert client.metadata[-1] == ("w1:p1", None, True, None)
    assert "w1:p1" not in d.managed


def test_done_metadata_refreshes_ttl_each_sweep():
    now = [0.0]
    client = FakeClient([_agent(status="done")])
    d = make_daemon(
        client,
        [StaticProbe(Pending("review", 30, "roborev"))],
        clock=lambda: now[0],
    )
    seed(d, client)
    d._reprobe_sweep()
    now[0] = 16.0
    d._reprobe_sweep()
    sets = [metadata for metadata in client.metadata if not metadata[2]]
    assert len(sets) == 2
    assert all(metadata[3] == 30000 for metadata in sets)


def test_done_to_idle_hands_over_to_hold():
    client = FakeClient([_agent(status="done")])
    probe = StaticProbe(Pending("CI: ci", 20, "ci"))
    d = make_daemon(client, [probe], reprobe_interval_s=0)
    seed(d, client)
    d._reprobe_sweep()
    assert d.managed["w1:p1"].kind == "done"
    client.set_agents([_agent(status="idle")])
    seed(d, client)
    d._reprobe_sweep()
    assert client.metadata[-1] == ("w1:p1", None, True, None)
    assert client.reports[-1] == ("w1:p1", "working", "⏳ CI: ci")
    assert d.managed["w1:p1"].kind == "hold"


def test_ttl_clamped_to_valid_range():
    client = FakeClient([_agent(status="done")])
    d = make_daemon(
        client,
        [StaticProbe(Pending("review", 30, "roborev"))],
        reprobe_interval_s=0,
    )
    assert d._ttl_ms() == TTL_MIN_MS
    d2 = make_daemon(client, [], reprobe_interval_s=100_000)
    assert d2._ttl_ms() == TTL_MAX_MS


def test_progress_uses_metadata_not_report_agent():
    client = FakeClient([_claude_agent()])
    d = make_daemon(client, progress=lambda sid: "2/5 Fixing auth")
    seed(d, client)
    d._progress_sweep()
    assert client.reports == []
    assert client.metadata == [("w1:p1", "2/5 Fixing auth", False, 30000)]
    assert d.managed["w1:p1"].kind == "progress"


def test_progress_writes_only_on_label_change_within_half_ttl():
    labels = iter(["2/5 Fixing auth", "2/5 Fixing auth", "3/5 Next step"])
    client = FakeClient([_claude_agent()])
    d = make_daemon(client, progress=lambda sid: next(labels))
    seed(d, client)
    d._progress_sweep()
    d._progress_sweep()
    assert len(client.metadata) == 1
    d._progress_sweep()
    assert client.metadata[-1] == ("w1:p1", "3/5 Next step", False, 30000)
    assert len(client.metadata) == 2
    assert client.reports == []


def test_progress_refreshes_after_half_ttl():
    now = [0.0]
    client = FakeClient([_claude_agent()])
    d = make_daemon(
        client, clock=lambda: now[0], progress=lambda sid: "2/5 X"
    )
    seed(d, client)
    d._progress_sweep()
    now[0] = 16.0
    d._progress_sweep()
    assert len(client.metadata) == 2
    assert client.reports == []


def test_progress_cleared_when_no_active_task():
    labels = iter(["2/5 Fixing auth", None])
    client = FakeClient([_claude_agent()])
    d = make_daemon(client, progress=lambda sid: next(labels))
    seed(d, client)
    d._progress_sweep()
    d._progress_sweep()
    assert client.metadata[-1] == ("w1:p1", None, True, None)
    assert client.reports == []
    assert "w1:p1" not in d.managed


def test_done_metadata_set_failure_retries_next_sweep():
    client = FakeClient([_agent(status="done")], meta_ok=False)
    d = make_daemon(
        client,
        [StaticProbe(Pending("review", 30, "roborev"))],
    )
    seed(d, client)

    d._reprobe_sweep()
    assert "w1:p1" not in d.managed
    client._meta_ok = True
    d._reprobe_sweep()

    assert d.managed["w1:p1"].kind == "done"
    assert len(client.metadata) == 2


def test_done_metadata_clear_failure_keeps_state_for_retry():
    probe = StaticProbe(Pending("review", 30, "roborev"))
    client = FakeClient([_agent(status="done")])
    d = make_daemon(client, [probe], reprobe_interval_s=0)
    seed(d, client)
    d._reprobe_sweep()
    probe.result = None
    client._meta_ok = False

    d._reprobe_sweep()
    assert "w1:p1" in d.managed
    client._meta_ok = True
    d._reprobe_sweep()

    assert "w1:p1" not in d.managed
    assert client.metadata[-1] == ("w1:p1", None, True, None)


def test_progress_uses_cached_session_when_working_omits_it():
    client = FakeClient([_claude_agent(status="idle")])
    d = make_daemon(client, progress=lambda sid: f"2/5 {sid[:4]}")
    seed(d, client)
    d._reprobe_sweep()
    assert client.metadata == []
    assert client.reports == []
    client.set_agents([_claude_agent(status="working", session=None)])
    seed(d, client)
    d._progress_sweep()
    assert client.metadata == [("w1:p1", "2/5 c00b", False, 30000)]
    assert client.reports == []


def test_progress_skipped_when_session_never_seen():
    client = FakeClient([_claude_agent(status="working", session=None)])
    d = make_daemon(client, progress=lambda sid: "2/5 x")
    seed(d, client)
    d._progress_sweep()
    assert client.metadata == []
    assert client.reports == []


def test_progress_skips_non_claude_agents():
    agent = _agent(status="working", agent="codex")
    agent["agent_session"] = {"value": "abc"}
    client = FakeClient([agent])
    d = make_daemon(client, progress=lambda sid: "2/5 X")
    seed(d, client)
    d._progress_sweep()
    assert client.metadata == []
    assert client.reports == []


def test_progress_disabled_leaves_working_panes_alone():
    client = FakeClient([_claude_agent()])
    d = make_daemon(client, progress=None)
    seed(d, client)
    d._progress_sweep()
    assert client.metadata == []
    assert client.reports == []


def test_progress_reader_exception_is_contained():
    def boom(sid):
        raise RuntimeError("bad file")

    client = FakeClient([_claude_agent()])
    d = make_daemon(client, progress=boom)
    seed(d, client)
    d._progress_sweep()
    assert client.metadata == []
    assert client.reports == []


def test_hold_pane_not_claimed_by_progress_sweep():
    client = FakeClient([_claude_agent(status="idle")])
    probe = StaticProbe(Pending("CI: ci", 20, "ci"))
    d = make_daemon(
        client,
        [probe],
        reprobe_interval_s=0,
        progress=lambda sid: "2/5 X",
    )
    seed(d, client)
    d._reprobe_sweep()
    assert d.managed["w1:p1"].kind == "hold"
    d._registry["w1:p1"]["agent_status"] = "working"
    d._progress_sweep()
    assert d.managed["w1:p1"].kind == "hold"
    assert client.metadata == []
    assert client.reports == [("w1:p1", "working", "⏳ CI: ci")]


def test_progress_pane_recovered_when_status_drifted():
    client = FakeClient([_claude_agent(status="idle")])
    probe = StaticProbe(Pending("CI: ci", 20, "ci"))
    d = make_daemon(
        client,
        [probe],
        reprobe_interval_s=0,
        progress=lambda sid: "2/5 X",
    )
    d.managed["w1:p1"] = ManagedPane(
        "2/5 X", "claude", kind="progress", terminal_id="term-w1:p1"
    )
    seed(d, client)
    d._reprobe_sweep()
    assert client.metadata[-1] == ("w1:p1", None, True, None)
    assert d.managed["w1:p1"].kind == "hold"


def test_adopt_hold_rows_and_legacy_rows():
    d = make_daemon(FakeClient([]))
    d.adopt(
        [
            {
                "pane_id": "w1:p1",
                "agent": "claude",
                "status": "⏳ review",
                "kind": "hold",
                "terminal_id": "t1",
            },
            {
                "pane_id": "w2:p1",
                "agent": "claude",
                "status": "2/5 X",
                "kind": "progress",
            },
            {
                "pane_id": "w3:p1",
                "agent": "claude",
                "status": "3/7 Y",
                "kind": "progress",
                "meta": True,
            },
        ]
    )
    assert d.managed["w1:p1"].kind == "hold"
    assert d.managed["w1:p1"].terminal_id == "t1"
    assert "w1:p1" in d._adopted
    assert "w2:p1" in d._legacy_release
    assert "w3:p1" not in d.managed
    assert "w3:p1" not in d._legacy_release


def test_legacy_release_retried_until_confirmed():
    client = FakeClient([_agent(pane="w2:p1", status="idle")], release_ok=False)
    d = make_daemon(client)
    d.adopt(
        [
            {
                "pane_id": "w2:p1",
                "agent": "claude",
                "status": "2/5 X",
                "kind": "progress",
            }
        ]
    )
    seed(d, client)
    d._reprobe_sweep()
    assert client.releases == ["w2:p1"]
    assert "w2:p1" in d._legacy_release
    client._release_result = "ok"
    d._reprobe_sweep()
    assert "w2:p1" not in d._legacy_release


def test_legacy_cleanup_drops_foreign_session_without_release():
    client = FakeClient([_owned_agent()])
    d = make_daemon(client)
    d.adopt(
        [
            {
                "pane_id": "w1:p1",
                "agent": "claude",
                "status": "2/5 X",
                "kind": "progress",
            }
        ]
    )
    seed(d, client)

    d._reprobe_sweep()

    assert client.releases == []
    assert d._legacy_release == {}


def test_release_gone_keeps_entry_and_schedules_resync():
    client = FakeClient([_agent(status="idle")])
    probe = StaticProbe(Pending("review", 30, "roborev"))
    d = make_daemon(client, [probe], reprobe_interval_s=0)
    seed(d, client)
    d._reprobe_sweep()
    probe.result = None
    client._release_result = "gone"
    d._reprobe_sweep()
    assert "w1:p1" in d.managed
    assert d._resync_due is True


def test_shutdown_keeps_rows_for_failed_cleanup():
    snaps = []
    client = FakeClient([_agent(status="idle")], release_ok=False)
    d = make_daemon(
        client,
        [StaticProbe(Pending("review", 30, "roborev"))],
        reprobe_interval_s=0,
        on_snapshot=snaps.append,
    )
    seed(d, client)
    d._reprobe_sweep()
    d.shutdown()
    assert "w1:p1" in d.managed
    assert snaps[-1] != []


def test_rows_include_terminal_id_and_meta_flag():
    client = FakeClient([_agent(status="done")])
    d = make_daemon(client, [StaticProbe(Pending("review", 30, "roborev"))])
    seed(d, client)
    d._reprobe_sweep()
    assert d._rows() == [
        {
            "pane_id": "w1:p1",
            "agent": "claude",
            "status": "⏳ review",
            "kind": "done",
            "terminal_id": "term-w1:p1",
            "meta": True,
        }
    ]


def test_shutdown_releases_holds_and_clears_metadata():
    client = FakeClient(
        [
            _agent(pane="w1:p1", status="idle"),
            _agent(pane="w2:p1", status="done"),
        ]
    )
    d = make_daemon(
        client,
        [StaticProbe(Pending("review", 30, "roborev"))],
        reprobe_interval_s=0,
    )
    seed(d, client)
    d._reprobe_sweep()
    assert d.managed["w1:p1"].kind == "hold"
    assert d.managed["w2:p1"].kind == "done"
    d.shutdown()
    assert client.releases == ["w1:p1"]
    assert client.metadata[-1] == ("w2:p1", None, True, None)
    assert d.managed == {}


def _status_event(pane="w1:p1", status="idle", custom=None, agent="claude"):
    return {
        "event": "pane.agent_status_changed",
        "data": {
            "pane_id": pane,
            "workspace_id": "w1",
            "agent_status": status,
            "agent": agent,
            "custom_status": custom,
        },
    }


class FakeStream:
    """Test double backed by a real socketpair so selectors can poll it."""

    def __init__(self, subscriptions=None):
        self.subscriptions = subscriptions or []
        self.closed = False
        self._r, self._w = _socket.socketpair()
        self._r.setblocking(False)
        self._pending = []

    def feed(self, event):
        self._pending.append(event)
        self._w.send(b"x")

    def fileno(self):
        return self._r.fileno()

    @property
    def has_buffered_data(self):
        return False

    def read_events(self, *, max_chunks=None):
        if max_chunks == 0:
            return []
        try:
            while True:
                if not self._r.recv(4096):
                    self.closed = True
                    break
        except BlockingIOError:
            pass
        out, self._pending = self._pending, []
        return out

    def close(self):
        self.closed = True
        for sock in (self._r, self._w):
            try:
                sock.close()
            except OSError:
                pass


class DripStream(FakeStream):
    """Deliver one queued event per readiness, like herdr's replay writer."""

    def __init__(self, subscriptions=None):
        super().__init__(subscriptions)
        self.read_count = 0

    def read_events(self, *, max_chunks=None):
        if max_chunks == 0:
            return []
        try:
            byte = self._r.recv(1)
            if not byte:
                self.closed = True
                return []
        except BlockingIOError:
            return []
        if not self._pending:
            return []
        self.read_count += 1
        return [self._pending.pop(0)]


def test_idle_edge_event_probes_immediately():
    client = FakeClient([_agent(status="working")])
    probe = StaticProbe(Pending("review", 30, "roborev"))
    d = make_daemon(client, [probe], reprobe_interval_s=15)
    seed(d, client)
    d._last_probe["w1:p1"] = 0.0  # stale throttle from an old probe
    client.agents["w1:p1"]["agent_status"] = "idle"
    d.dispatch_event(_status_event(status="idle"))
    assert client.reports == [("w1:p1", "working", "⏳ review")]


def test_idle_edge_marker_skips_slower_probes():
    client = FakeClient([_agent(status="working")])
    slow_calls = []

    class Marker:
        name = "marker"

        def check(self, ctx):
            return Pending("deploy", 40, "marker")

    class Slow:
        name = "slow"

        def check(self, ctx):
            slow_calls.append(ctx.pane_id)
            return Pending("review", 30, "roborev")

    d = make_daemon(
        client, [Marker(), Slow()], reprobe_interval_s=15
    )
    seed(d, client)
    client.agents["w1:p1"]["agent_status"] = "idle"

    d.dispatch_event(_status_event(status="idle"))

    assert client.reports == [("w1:p1", "working", "⏳ deploy")]
    assert slow_calls == []


def test_fast_marker_lookup_skips_git_enrichment():
    client = FakeClient([_agent(status="idle")])
    slow_calls = []

    class Marker:
        name = "marker"

        def check_pane(self, pane_id):
            return Pending("deploy", 40, "marker")

        def check(self, ctx):
            raise AssertionError("context path must not run")

    class Slow:
        name = "slow"

        def check(self, ctx):
            slow_calls.append(ctx.pane_id)
            return None

    d = make_daemon(
        client,
        [Marker(), Slow()],
        reprobe_interval_s=0,
        enrich=lambda cwd: (_ for _ in ()).throw(
            AssertionError("git enrichment must not run")
        ),
    )
    seed(d, client)

    d._probe_pane("w1:p1", fast=True)

    assert client.reports == [("w1:p1", "working", "⏳ deploy")]
    assert slow_calls == []


def test_stale_working_event_reprobes_current_idle_state():
    client = FakeClient([_agent(status="idle")])
    d = make_daemon(
        client,
        [StaticProbe(Pending("review", 30, "roborev"))],
        reprobe_interval_s=0,
    )
    seed(d, client)

    d.dispatch_event(_status_event(status="working"))

    assert d._registry["w1:p1"]["agent_status"] == "idle"
    assert client.reports == [("w1:p1", "working", "⏳ review")]


def test_done_edge_event_labels_immediately():
    client = FakeClient([_agent(status="working")])
    probe = StaticProbe(Pending("review", 30, "roborev"))
    d = make_daemon(client, [probe], reprobe_interval_s=15)
    seed(d, client)
    d._last_probe["w1:p1"] = 0.0
    client.agents["w1:p1"]["agent_status"] = "done"
    d.dispatch_event(_status_event(status="done"))
    assert client.metadata == [("w1:p1", "⏳ review", False, 30000)]


def test_self_echo_event_is_ignored():
    client = FakeClient([_agent(status="idle")])
    probe = StaticProbe(Pending("review", 30, "roborev"))
    d = make_daemon(client, [probe], reprobe_interval_s=0)
    seed(d, client)
    d._reprobe_sweep()  # hold asserted
    assert len(client.reports) == 1
    last_probe = d._last_probe["w1:p1"]
    client.agents["w1:p1"].update(
        agent_status="working", custom_status="⏳ review"
    )
    d.dispatch_event(_status_event(status="working", custom="⏳ review"))
    assert len(client.reports) == 1  # echo: no re-probe, no re-assert
    assert d._registry["w1:p1"]["agent_status"] == "working"
    assert d._last_probe["w1:p1"] == last_probe


def test_foreign_session_idle_metadata_clears_on_working_edge():
    client = FakeClient([_owned_agent()])
    d = make_daemon(
        client,
        [StaticProbe(Pending("review", 30, "roborev"))],
        reprobe_interval_s=0,
    )
    seed(d, client)
    d._reprobe_sweep()

    client.agents["w1:p1"]["agent_status"] = "working"
    d.dispatch_event(_status_event(status="working", custom="⏳ review"))

    assert client.releases == []
    assert client.metadata[-1] == ("w1:p1", None, True, None)
    assert "w1:p1" not in d.managed


def test_foreign_session_idle_metadata_self_echo_is_ignored():
    client = FakeClient([_owned_agent()])
    d = make_daemon(
        client,
        [StaticProbe(Pending("review", 30, "roborev"))],
        reprobe_interval_s=0,
    )
    seed(d, client)
    d._reprobe_sweep()
    last_probe = d._last_probe["w1:p1"]
    client.agents["w1:p1"]["custom_status"] = "⏳ review"

    d.dispatch_event(_status_event(status="idle", custom="⏳ review"))

    assert client.metadata == [("w1:p1", "⏳ review", False, 1000)]
    assert d._last_probe["w1:p1"] == last_probe


def test_progress_stop_event_hands_over_to_hold():
    client = FakeClient([_claude_agent()])
    probe = StaticProbe(None)
    d = make_daemon(
        client,
        [probe],
        reprobe_interval_s=15,
        progress=lambda sid: "2/5 X",
    )
    seed(d, client)
    d._progress_sweep()  # progress label on
    assert d.managed["w1:p1"].kind == "progress"
    probe.result = Pending("CI: ci", 20, "ci")
    client.agents["w1:p1"]["agent_status"] = "idle"
    d.dispatch_event(_status_event(status="idle"))
    assert client.metadata[-1] == ("w1:p1", None, True, None)
    assert client.reports[-1] == ("w1:p1", "working", "⏳ CI: ci")
    assert d.managed["w1:p1"].kind == "hold"


def test_blocked_event_clears_progress_without_hold():
    client = FakeClient([_claude_agent()])
    probe = StaticProbe(Pending("CI: ci", 20, "ci"))
    d = make_daemon(
        client,
        [probe],
        reprobe_interval_s=0,
        progress=lambda sid: "2/5 X",
    )
    seed(d, client)
    d._progress_sweep()
    client.agents["w1:p1"]["agent_status"] = "blocked"
    d.dispatch_event(_status_event(status="blocked"))
    assert client.metadata[-1] == ("w1:p1", None, True, None)
    assert client.reports == []  # no hold over a blocked pane
    assert "w1:p1" not in d.managed


def test_unknown_pane_event_schedules_resync():
    client = FakeClient([])
    d = make_daemon(client)
    d.dispatch_event(_status_event(pane="w9:p9"))
    assert d._resync_due is True


def test_lifecycle_event_schedules_resync():
    d = make_daemon(FakeClient([]))
    d.dispatch_event(
        {
            "event": "pane_created",
            "data": {"type": "pane_created", "pane": {"pane_id": "w1:p1"}},
        }
    )
    assert d._resync_due is True


def test_redundant_agent_detected_event_does_not_schedule_resync():
    client = FakeClient([_agent(status="idle")])
    d = make_daemon(client)
    seed(d, client)

    d.dispatch_event(
        {
            "event": "pane_agent_detected",
            "data": {
                "type": "pane_agent_detected",
                "pane_id": "w1:p1",
                "agent": "claude",
            },
        }
    )
    d.dispatch_event(
        {
            "event": "pane.agent_detected",
            "data": {
                "type": "pane_agent_detected",
                "pane_id": "w1:p1",
            },
        }
    )

    assert d._resync_due is False
    assert d._resync_not_before is None


def test_unknown_or_changed_agent_detected_schedules_resync():
    now = [10.0]
    client = FakeClient([_agent(status="idle")])
    d = make_daemon(client, clock=lambda: now[0])
    seed(d, client)

    d.dispatch_event(
        {
            "event": "pane_agent_detected",
            "data": {
                "type": "pane_agent_detected",
                "pane_id": "w9:p9",
                "agent": "claude",
            },
        }
    )
    assert d._resync_due is True
    assert d._resync_not_before == 10.25

    d._resync_due = False
    d._resync_not_before = None
    d.dispatch_event(
        {
            "event": "pane_agent_detected",
            "data": {
                "type": "pane_agent_detected",
                "pane_id": "w1:p1",
                "agent": "codex",
            },
        }
    )
    assert d._resync_due is True
    assert d._resync_not_before == 10.25


def test_lifecycle_resync_waits_for_coalescing_deadline():
    now = [0.0]
    d = make_daemon(FakeClient([]), clock=lambda: now[0])

    for index in range(20):
        now[0] = index * 0.01
        d.dispatch_event(
            {
                "event": "pane_created",
                "data": {
                    "type": "pane_created",
                    "pane": {"pane_id": f"w1:p{index}"},
                },
            }
        )
        assert d._resync_ready(now[0]) is False

    now[0] = 0.24
    assert d._resync_ready(now[0]) is False
    now[0] = 0.26
    assert d._resync_ready(now[0]) is True


def test_lifecycle_resync_coalescing_deadline_does_not_slide():
    now = [10.0]
    d = make_daemon(FakeClient([]), clock=lambda: now[0])
    event = {
        "event": "pane_created",
        "data": {"type": "pane_created", "pane": {"pane_id": "w1:p1"}},
    }

    d.dispatch_event(event)
    assert d._resync_not_before == 10.25
    now[0] = 10.1
    d.dispatch_event(event)
    assert d._resync_not_before == 10.25


def test_pane_moved_remaps_bookkeeping():
    client = FakeClient([_agent(status="idle")])
    probe = StaticProbe(Pending("review", 30, "roborev"))
    d = make_daemon(client, [probe], reprobe_interval_s=0)
    seed(d, client)
    d._reprobe_sweep()
    moved = _agent(pane="w2:p9", status="idle", term="term-w1:p1")
    client.set_agents([moved])
    d.dispatch_event(
        {
            "event": "pane_moved",
            "data": {
                "type": "pane_moved",
                "previous_pane_id": "w1:p1",
                "previous_workspace_id": "w1",
                "previous_tab_id": "w1:t1",
                "pane": dict(moved),
            },
        }
    )
    assert "w1:p1" not in d.managed
    assert d.managed["w2:p9"].kind == "hold"
    assert d.managed["w2:p9"].terminal_id == "term-w1:p1"
    assert "w2:p9" in d._registry and "w1:p1" not in d._registry
    assert d._resync_due is True


def test_pane_moved_tears_down_stream_for_resubscribe():
    # the old per-pane subscription is bound to the dead pane id; only a
    # new stream (re-bootstrap) restores event coverage for the moved pane
    client = FakeClient([_agent(status="idle")])
    d = make_daemon(client)
    seed(d, client)
    stream = FakeStream()
    d._stream = stream
    moved = _agent(pane="w2:p9", status="idle", term="term-w1:p1")
    d.dispatch_event(
        {
            "event": "pane_moved",
            "data": {
                "type": "pane_moved",
                "previous_pane_id": "w1:p1",
                "previous_workspace_id": "w1",
                "previous_tab_id": "w1:t1",
                "pane": dict(moved),
            },
        }
    )
    assert stream.closed and d._stream is None


def test_replayed_pane_move_for_remapped_terminal_is_ignored():
    current = _agent(pane="w2:p9", status="idle", term="term-1")
    client = FakeClient([current])
    d = make_daemon(client)
    seed(d, client)
    stream = FakeStream()
    d._stream = stream

    d.dispatch_event(
        {
            "event": "pane_moved",
            "data": {
                "type": "pane_moved",
                "previous_pane_id": "w1:p1",
                "pane": dict(current),
            },
        }
    )

    assert d._stream is stream and stream.closed is False
    assert set(d._registry) == {"w2:p9"}
    assert d._resync_due is False


def test_stale_pane_move_terminal_mismatch_defers_to_snapshot():
    old = _agent(pane="w1:p1", status="idle", term="term-current")
    client = FakeClient([old])
    d = make_daemon(client)
    seed(d, client)
    stream = FakeStream()
    d._stream = stream
    stale = _agent(pane="w2:p9", status="idle", term="term-stale")

    d.dispatch_event(
        {
            "event": "pane_moved",
            "data": {
                "type": "pane_moved",
                "previous_pane_id": "w1:p1",
                "pane": stale,
            },
        }
    )

    assert d._stream is stream and stream.closed is False
    assert set(d._registry) == {"w1:p1"}
    assert d._resync_due is True


def test_pane_moved_to_denied_pane_releases():
    client = FakeClient([_agent(status="idle")])
    probe = StaticProbe(Pending("review", 30, "roborev"))
    d = make_daemon(client, [probe], reprobe_interval_s=0, deny=["w2:p9"])
    seed(d, client)
    d._reprobe_sweep()
    moved = _agent(pane="w2:p9", status="idle", term="term-w1:p1")
    client.set_agents([moved])
    d.dispatch_event(
        {
            "event": "pane_moved",
            "data": {
                "type": "pane_moved",
                "previous_pane_id": "w1:p1",
                "previous_workspace_id": "w1",
                "previous_tab_id": "w1:t1",
                "pane": dict(moved),
            },
        }
    )
    assert client.releases == ["w2:p9"]  # released under the NEW id
    assert "w2:p9" not in d.managed


def test_pane_moved_remaps_legacy_entry():
    client = FakeClient([], release_ok=False)
    d = make_daemon(client)
    d.adopt(
        [
            {
                "pane_id": "w1:p1",
                "agent": "claude",
                "status": "2/5 X",
                "kind": "progress",
                "terminal_id": "term-w1:p1",
            }
        ]
    )
    moved = _agent(pane="w2:p9", status="working", term="term-w1:p1")
    d.dispatch_event(
        {
            "event": "pane_moved",
            "data": {
                "type": "pane_moved",
                "previous_pane_id": "w1:p1",
                "previous_workspace_id": "w1",
                "previous_tab_id": "w1:t1",
                "pane": dict(moved),
            },
        }
    )
    assert "w2:p9" in d._legacy_release and "w1:p1" not in d._legacy_release


def test_pane_moved_prefers_fresh_session_from_event():
    old = _claude_agent(status="idle", session="old-session")
    client = FakeClient([old])
    d = make_daemon(client)
    seed(d, client)
    moved = _claude_agent(
        pane="w2:p9", status="idle", session="fresh-session"
    )
    moved["terminal_id"] = "term-w1:p1"

    d.dispatch_event(
        {
            "event": "pane_moved",
            "data": {
                "type": "pane_moved",
                "previous_pane_id": "w1:p1",
                "previous_workspace_id": "w1",
                "previous_tab_id": "w1:t1",
                "pane": dict(moved),
            },
        }
    )

    assert "w1:p1" not in d._session_cache
    assert d._session_cache["w2:p9"] == "fresh-session"


def test_resync_releases_vanished_pane_and_drops_bookkeeping():
    client = FakeClient([_agent(status="idle")])
    d = make_daemon(
        client,
        [StaticProbe(Pending("review", 30, "roborev"))],
        reprobe_interval_s=0,
    )
    seed(d, client)
    d._reprobe_sweep()
    client.set_agents([])
    client._release_result = "gone"  # herdr can't release a gone pane
    d._resync()
    assert client.releases == ["w1:p1"]  # best-effort attempted
    assert d.managed == {}  # dropped regardless of outcome
    assert d._registry == {}


def test_resync_remaps_moved_pane_by_terminal_id():
    # the pane.moved event was missed; the snapshot still reconciles it
    client = FakeClient([_agent(status="idle")])
    d = make_daemon(
        client,
        [StaticProbe(Pending("review", 30, "roborev"))],
        reprobe_interval_s=0,
    )
    seed(d, client)
    d._reprobe_sweep()
    client.set_agents(
        [_agent(pane="w2:p9", status="idle", term="term-w1:p1")]
    )
    d._resync()
    assert d.managed.get("w2:p9") is not None
    assert d.managed["w2:p9"].kind == "hold"
    assert client.releases == []  # nothing dropped, nothing released


def test_resync_keeps_state_when_herdr_down():
    client = FakeClient([_agent(status="idle")])
    d = make_daemon(
        client,
        [StaticProbe(Pending("review", 30, "roborev"))],
        reprobe_interval_s=0,
    )
    seed(d, client)
    d._reprobe_sweep()
    client.snapshot_error = HerdrUnavailable("down")
    d._resync()
    assert "w1:p1" in d.managed  # a blip must not drop assertions
    assert "w1:p1" in d._registry


def test_resync_logs_old_server_and_keeps_state(caplog):
    client = FakeClient([_agent(status="idle")])
    d = make_daemon(
        client,
        [StaticProbe(Pending("review", 30, "roborev"))],
        reprobe_interval_s=0,
    )
    seed(d, client)
    d._reprobe_sweep()
    client.snapshot_error = HerdrApiError(
        "unknown_method", "session.snapshot"
    )
    with caplog.at_level("ERROR"):
        d._resync()
    assert any("0.7.2" in r.message for r in caplog.records)
    assert "w1:p1" in d.managed


def test_resync_tears_down_stream_on_pane_set_change():
    client = FakeClient([_agent(status="idle")])
    d = make_daemon(client)
    seed(d, client)
    d._stream = FakeStream()
    client.set_agents([_agent(), _agent(pane="w2:p1")])
    d._resync()
    assert d._stream is None  # run loop re-bootstraps/resubscribes


def test_resync_keeps_stream_when_pane_set_unchanged():
    client = FakeClient([_agent(status="idle")])
    d = make_daemon(client)
    seed(d, client)
    stream = FakeStream()
    d._stream = stream
    d._resync()
    assert d._stream is stream


def test_resync_backfills_legacy_terminal_id():
    client = FakeClient([_agent(status="idle")], release_ok=False)
    d = make_daemon(client)
    d.adopt(
        [
            {
                "pane_id": "w1:p1",
                "agent": "claude",
                "status": "2/5 X",
                "kind": "progress",
            }
        ]
    )  # pre-migration row: no terminal_id
    d._resync()
    assert d._legacy_release["w1:p1"].terminal_id == "term-w1:p1"


def test_resync_remaps_legacy_row_by_terminal_id():
    client = FakeClient([_agent(status="idle")], release_ok=False)
    d = make_daemon(client)
    d.adopt(
        [
            {
                "pane_id": "w1:p1",
                "agent": "claude",
                "status": "2/5 X",
                "kind": "progress",
                "terminal_id": "term-w1:p1",
            }
        ]
    )
    client.set_agents(
        [_agent(pane="w2:p9", status="idle", term="term-w1:p1")]
    )
    d._resync()
    assert "w2:p9" in d._legacy_release and "w1:p1" not in d._legacy_release


def test_resync_salvages_legacy_row_by_unique_label_match():
    # moved between old daemon's crash and our first snapshot: no terminal_id;
    # exactly one pane carries our stored label -> remap to it
    moved = _agent(pane="w2:p9", status="working")
    moved["custom_status"] = "2/5 X"
    client = FakeClient([moved], release_ok=False)
    d = make_daemon(client)
    d.adopt(
        [
            {
                "pane_id": "w1:p1",
                "agent": "claude",
                "status": "2/5 X",
                "kind": "progress",
            }
        ]
    )
    d._resync()
    assert "w2:p9" in d._legacy_release and "w1:p1" not in d._legacy_release


def test_resync_salvages_adopted_hold_by_unique_label_match():
    moved = _agent(pane="w2:p9", status="working")
    moved["custom_status"] = "⏳ review"
    client = FakeClient([moved])
    d = make_daemon(client)
    d.adopt(
        [
            {
                "pane_id": "w1:p1",
                "agent": "claude",
                "status": "⏳ review",
                "kind": "hold",
            }
        ]
    )

    d._resync()

    assert "w2:p9" in d.managed and "w1:p1" not in d.managed
    assert "w2:p9" in d._adopted
    assert client.releases == []


def test_resync_drops_unmatchable_legacy_row():
    client = FakeClient([_agent(pane="w3:p1", status="idle")])
    d = make_daemon(client)
    d.adopt(
        [
            {
                "pane_id": "w1:p1",
                "agent": "claude",
                "status": "2/5 X",
                "kind": "progress",
            }
        ]
    )
    d._resync()
    assert d._legacy_release == {}  # no terminal, no label match -> gone


def test_resync_drops_stale_session_cache():
    client = FakeClient([_claude_agent(status="idle")])
    d = make_daemon(client, progress=lambda sid: "2/5 x")
    seed(d, client)
    assert d._session_cache.get("w1:p1")
    client.set_agents([])
    d._resync()
    assert "w1:p1" not in d._session_cache


def test_bootstrap_subscribes_then_snapshots():
    client = FakeClient([_agent(status="idle")])
    made = []

    def factory(subs):
        made.append(subs)
        return FakeStream(subs)

    d = make_daemon(client, stream_factory=factory)
    assert d.bootstrap() is True
    assert d._stream is not None
    assert "w1:p1" in d._registry
    per_pane = [
        sub
        for sub in made[0]
        if sub.get("type") == "pane.agent_status_changed"
    ]
    assert per_pane == [
        {"type": "pane.agent_status_changed", "pane_id": "w1:p1"}
    ]
    globals_ = {sub["type"] for sub in made[0] if "pane_id" not in sub}
    assert {
        "pane.created",
        "pane.closed",
        "pane.exited",
        "pane.moved",
        "workspace.closed",
        "tab.closed",
    } <= globals_
    assert "pane.agent_detected" not in globals_


def test_bootstrap_subscribes_status_for_unknown_panes():
    known = _agent(status="idle")
    client = FakeClient([known])
    client.session_snapshot = lambda: {
        "agents": [dict(known)],
        "panes": [
            {"pane_id": "w1:p1"},
            {"pane_id": "w1:p2"},
        ],
    }
    made = []
    d = make_daemon(
        client,
        stream_factory=lambda subs: made.append(subs) or FakeStream(subs),
    )

    assert d.bootstrap() is True

    per_pane = {
        sub["pane_id"]
        for sub in made[0]
        if sub.get("type") == "pane.agent_status_changed"
    }
    assert per_pane == {"w1:p1", "w1:p2"}
    assert set(d._registry) == {"w1:p1"}


def test_resync_discovers_agent_in_already_subscribed_unknown_pane():
    known = _agent(status="idle")
    discovered = _agent(pane="w1:p2", status="working")
    snapshot = {
        "agents": [dict(known)],
        "panes": [{"pane_id": "w1:p1"}, {"pane_id": "w1:p2"}],
    }
    client = FakeClient([known])
    client.session_snapshot = lambda: snapshot
    d = make_daemon(client, stream_factory=lambda subs: FakeStream(subs))
    assert d.bootstrap() is True
    stream = d._stream

    snapshot = {
        "agents": [dict(known), dict(discovered)],
        "panes": [{"pane_id": "w1:p1"}, {"pane_id": "w1:p2"}],
    }
    d._resync()

    assert set(d._registry) == {"w1:p1", "w1:p2"}
    assert d._stream is stream
    assert stream.closed is False


def test_bootstrap_retries_when_pane_set_drifts():
    client = FakeClient([])
    calls = {"n": 0}
    made = []

    def drifting_snapshot():
        calls["n"] += 1
        if calls["n"] == 1:
            return {"agents": [_agent()]}
        return {"agents": [_agent(), _agent(pane="w2:p1")]}

    client.session_snapshot = drifting_snapshot

    def factory(subs):
        made.append(subs)
        return FakeStream(subs)

    d = make_daemon(client, stream_factory=factory)
    assert d.bootstrap() is True
    assert set(d._registry) == {"w1:p1", "w2:p1"}
    per_pane = [sub["pane_id"] for sub in made[-1] if "pane_id" in sub]
    assert per_pane == ["w1:p1", "w2:p1"]


def test_bootstrap_fails_when_unavailable():
    client = FakeClient([])
    client.snapshot_error = HerdrUnavailable("down")
    d = make_daemon(client, stream_factory=lambda subs: FakeStream(subs))
    assert d.bootstrap() is False


def test_bootstrap_old_server_logs_and_fails(caplog):
    client = FakeClient([])
    client.snapshot_error = HerdrApiError("unknown_method", "session.snapshot")
    d = make_daemon(client, stream_factory=lambda subs: FakeStream(subs))
    with caplog.at_level("ERROR"):
        assert d.bootstrap() is False
    assert any("0.7.2" in record.message for record in caplog.records)


def test_bootstrap_reconciles_adopted_rows_before_first_sweep():
    client = FakeClient(
        [_agent(pane="w2:p9", status="idle", term="t-old")]
    )
    d = make_daemon(client, stream_factory=lambda subs: FakeStream(subs))
    d.adopt(
        [
            {
                "pane_id": "w1:p1",
                "agent": "claude",
                "status": "⏳ review",
                "kind": "hold",
                "terminal_id": "t-old",
            }
        ]
    )
    assert d.bootstrap() is True
    assert "w2:p9" in d.managed and "w1:p1" not in d.managed
    assert client.releases == []


def test_bootstrap_logs_each_failure_reason_once(caplog):
    client = FakeClient([])
    client.snapshot_error = HerdrUnavailable("down")
    d = make_daemon(client, stream_factory=lambda subs: FakeStream(subs))
    with caplog.at_level("WARNING"):
        assert d.bootstrap() is False
        assert d.bootstrap() is False
    assert sum("down" in record.message for record in caplog.records) == 1


def test_bootstrap_subscribe_rejection_retries_with_fresh_set():
    client = FakeClient([_agent(status="idle")])
    attempts = {"n": 0}

    def factory(subs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise HerdrApiError("not_found", "pane closed during setup")
        return FakeStream(subs)

    d = make_daemon(client, stream_factory=factory)
    assert d.bootstrap() is True
    assert attempts["n"] == 2


def test_bootstrap_closes_stream_when_second_snapshot_fails():
    client = FakeClient([])
    calls = {"n": 0}
    stream = FakeStream()

    def snapshot():
        calls["n"] += 1
        if calls["n"] == 1:
            return {"agents": [_agent()]}
        raise HerdrUnavailable("restart during bootstrap")

    client.session_snapshot = snapshot
    d = make_daemon(client, stream_factory=lambda subs: stream)

    assert d.bootstrap() is False
    assert stream.closed is True


class Stop(BaseException):
    pass


def _stop_sleep(_):
    raise Stop()


def test_run_loop_processes_idle_event_from_stream():
    client = FakeClient([_agent(status="working")])
    probe = StaticProbe(Pending("review", 30, "roborev"))
    stream = FakeStream()
    d = make_daemon(
        client,
        [probe],
        stream_factory=lambda subs: stream,
        reprobe_interval_s=0.001,
        resync_interval_s=999.0,
        progress_interval_s=999.0,
    )
    ticks = {"n": 0}

    def clock():
        ticks["n"] += 1
        if ticks["n"] == 50:
            client.agents["w1:p1"]["agent_status"] = "idle"
            stream.feed(_status_event(status="idle"))
        if ticks["n"] > 400:
            raise Stop()
        return float(ticks["n"]) * 0.001

    d._clock = clock
    try:
        d.run(sleep=lambda delay: None)
    except Stop:
        pass
    assert ("w1:p1", "working", "⏳ review") in client.reports


def test_run_drains_status_event_between_reprobe_panes():
    order = []
    stream = FakeStream()

    class StopClient(FakeClient):
        def report_agent(
            self, pane_id, source, agent, state, custom_status=None
        ):
            super().report_agent(
                pane_id, source, agent, state, custom_status
            )
            raise Stop()

    client = StopClient(
        [
            _agent(pane="w1:p1", status="idle"),
            _agent(pane="w1:p2", status="working"),
            _agent(pane="w1:p3", status="idle"),
        ]
    )

    class Probe:
        name = "interleaved"

        def check(self, ctx):
            order.append(ctx.pane_id)
            if ctx.pane_id == "w1:p1":
                client.agents["w1:p2"]["agent_status"] = "idle"
                stream.feed(_status_event(pane="w1:p2", status="idle"))
                return None
            if ctx.pane_id == "w1:p2":
                return Pending("marker", 40, "marker")
            return None

    d = make_daemon(
        client,
        [Probe()],
        stream_factory=lambda subs: stream,
        reprobe_interval_s=999.0,
        resync_interval_s=999.0,
        progress_interval_s=999.0,
    )

    try:
        d.run(sleep=_stop_sleep)
    except Stop:
        pass

    assert client.reports == [("w1:p2", "working", "⏳ marker")]
    assert "w1:p3" not in order


def test_run_fast_probes_new_agent_discovered_between_panes():
    order = []

    class BoundedStream(FakeStream):
        def __init__(self):
            super().__init__()
            self.read_limits = []

        def read_events(self, *, max_chunks=None):
            self.read_limits.append(max_chunks)
            if max_chunks is None:
                raise AssertionError("event drains must have a chunk budget")
            return super().read_events(max_chunks=max_chunks)

    stream = BoundedStream()

    class StopClient(FakeClient):
        def report_agent(
            self, pane_id, source, agent, state, custom_status=None
        ):
            super().report_agent(
                pane_id, source, agent, state, custom_status
            )
            raise Stop()

    client = StopClient(
        [
            _agent(pane="w1:p1", status="idle"),
            _agent(pane="w1:p3", status="idle"),
        ]
    )

    def snapshot():
        return {
            "agents": [dict(agent) for agent in client.agents.values()],
            "panes": [
                {"pane_id": "w1:p1"},
                {"pane_id": "w1:p2"},
                {"pane_id": "w1:p3"},
            ],
        }

    client.session_snapshot = snapshot

    class Marker:
        name = "marker"

        def check_pane(self, pane_id):
            if pane_id == "w1:p2":
                return Pending("marker", 40, "marker")
            return None

        def check(self, ctx):
            order.append(ctx.pane_id)
            if ctx.pane_id == "w1:p1":
                client.agents["w1:p2"] = _agent(
                    pane="w1:p2", status="idle"
                )
                stream.feed(_status_event(pane="w1:p2", status="idle"))
            return None

    d = make_daemon(
        client,
        [Marker()],
        stream_factory=lambda subs: stream,
        reprobe_interval_s=999.0,
        resync_interval_s=999.0,
        progress_interval_s=999.0,
    )

    try:
        d.run(sleep=_stop_sleep)
    except Stop:
        pass

    assert client.reports == [("w1:p2", "working", "⏳ marker")]
    assert "w1:p3" not in order
    assert stream.read_limits and all(
        limit in (0, 1) for limit in stream.read_limits
    )


def test_run_drains_startup_replay_before_running_slow_probes():
    client = FakeClient([_agent(status="idle")])
    streams = []
    observed_read_counts = []

    class StopAfterProbe:
        name = "stop-after-probe"

        def check(self, ctx):
            observed_read_counts.append(streams[0].read_count)
            raise Stop()

    def factory(subs):
        stream = DripStream(subs)
        streams.append(stream)
        for index in range(40):
            stream.feed(
                {
                    "event": "pane_agent_detected",
                    "data": {
                        "type": "pane_agent_detected",
                        "pane_id": "w1:p1",
                        "agent": "claude",
                        "replay_index": index,
                    },
                }
            )
        return stream

    d = make_daemon(
        client,
        [StopAfterProbe()],
        stream_factory=factory,
        clock=time.monotonic,
        startup_replay_quiet_s=0.01,
        startup_replay_max_s=0.1,
    )

    try:
        d.run(sleep=_stop_sleep)
    except Stop:
        pass

    assert len(streams) == 1
    assert observed_read_counts == [40]


def test_run_reconnects_when_stream_closes_during_startup_replay():
    client = FakeClient([_agent(status="idle")])
    streams = []
    probe_calls = []

    class Probe:
        name = "probe"

        def check(self, ctx):
            probe_calls.append(ctx.pane_id)
            return None

    def factory(subs):
        stream = FakeStream(subs)
        streams.append(stream)
        stream.feed({"event": "replayed", "data": {}})
        stream._w.close()
        return stream

    d = make_daemon(
        client,
        [Probe()],
        stream_factory=factory,
        clock=time.monotonic,
        startup_replay_quiet_s=0.01,
        startup_replay_max_s=0.1,
    )

    try:
        d.run(sleep=_stop_sleep)
    except Stop:
        pass

    assert len(streams) == 1
    assert streams[0].closed is True
    assert probe_calls == []


def test_startup_replay_drain_stops_at_hard_max():
    stream = DripStream()
    for index in range(40):
        stream.feed({"event": "replayed", "data": {"index": index}})
    ticks = {"now": 0.0}

    def clock():
        ticks["now"] += 0.01
        return ticks["now"]

    d = make_daemon(
        FakeClient(),
        clock=clock,
        startup_replay_quiet_s=1.0,
        startup_replay_max_s=0.05,
    )
    selector = selectors.DefaultSelector()
    selector.register(stream, selectors.EVENT_READ)
    try:
        result = d._drain_startup_replay(selector, stream)
    finally:
        selector.close()
        stream.close()

    outcome, drained = result
    assert outcome == "timeout"
    assert 0 < drained < 40


def test_startup_replay_partial_buffer_cannot_be_declared_quiet():
    class PartialBufferedStream(FakeStream):
        @property
        def has_buffered_data(self):
            return True

    stream = PartialBufferedStream()
    d = make_daemon(
        FakeClient(),
        clock=time.monotonic,
        startup_replay_quiet_s=0.005,
        startup_replay_max_s=0.02,
    )
    selector = selectors.DefaultSelector()
    selector.register(stream, selectors.EVENT_READ)
    try:
        outcome, drained = d._drain_startup_replay(selector, stream)
    finally:
        selector.close()
        stream.close()

    assert outcome == "timeout"
    assert drained == 0


def test_startup_replay_drains_event_ready_at_quiet_boundary():
    stream = DripStream()
    stream.feed({"event": "boundary", "data": {}})
    now = [0.0]
    zero_polls = {"count": 0}

    class BoundarySelector:
        def select(self, timeout):
            if timeout > 0:
                now[0] += timeout
                return []
            zero_polls["count"] += 1
            return [(stream, None)] if zero_polls["count"] == 1 else []

    d = make_daemon(
        FakeClient(),
        clock=lambda: now[0],
        startup_replay_quiet_s=1.0,
        startup_replay_max_s=10.0,
    )
    try:
        outcome, drained = d._drain_startup_replay(
            BoundarySelector(), stream
        )
    finally:
        stream.close()

    assert outcome == "quiet"
    assert drained == 1


def test_run_takes_snapshot_without_reconnecting_when_startup_replay_times_out():
    client = FakeClient([_agent(status="idle")])
    streams = []
    observed_read_counts = []

    class StopAfterProbe:
        name = "stop-after-probe"

        def check(self, ctx):
            observed_read_counts.append(streams[0].read_count)
            raise Stop()

    def factory(subs):
        stream = DripStream(subs)
        streams.append(stream)
        for _ in range(40):
            stream.feed(_status_event(status="idle"))
        return stream

    ticks = {"now": 0.0}

    def clock():
        ticks["now"] += 0.01
        return ticks["now"]

    d = make_daemon(
        client,
        [StopAfterProbe()],
        stream_factory=factory,
        clock=clock,
        startup_replay_quiet_s=1.0,
        startup_replay_max_s=0.05,
    )

    try:
        d.run(sleep=_stop_sleep)
    except Stop:
        pass

    assert len(streams) == 1
    assert streams[0].closed is False
    assert streams[0]._pending
    assert observed_read_counts
    assert client.reports == []


def test_run_reconnects_when_startup_resync_fails():
    client = FakeClient([_agent(status="idle")])
    snapshot_calls = {"count": 0}
    streams = []
    probe_calls = []

    def snapshot():
        snapshot_calls["count"] += 1
        if snapshot_calls["count"] == 3:
            raise HerdrUnavailable("down after replay")
        return {"agents": [dict(agent) for agent in client.agents.values()]}

    class Probe:
        name = "probe"

        def check(self, ctx):
            probe_calls.append(ctx.pane_id)
            return None

    def factory(subs):
        stream = FakeStream(subs)
        streams.append(stream)
        return stream

    client.session_snapshot = snapshot
    d = make_daemon(
        client,
        [Probe()],
        stream_factory=factory,
        clock=time.monotonic,
        startup_replay_quiet_s=0.01,
        startup_replay_max_s=0.1,
    )

    try:
        d.run(sleep=_stop_sleep)
    except Stop:
        pass

    assert snapshot_calls["count"] == 3
    assert len(streams) == 1
    assert streams[0].closed is True
    assert d._stream is None
    assert probe_calls == []


def test_run_drains_replayed_lifecycle_storm_before_resync():
    client = FakeClient([_agent(status="idle")])
    snapshot_calls = {"count": 0, "first_resync_read_count": None}
    streams = []

    def snapshot():
        snapshot_calls["count"] += 1
        if snapshot_calls["count"] == 3:
            snapshot_calls["first_resync_read_count"] = streams[0].read_count
        return {"agents": [dict(agent) for agent in client.agents.values()]}

    client.session_snapshot = snapshot

    def factory(subs):
        stream = DripStream(subs)
        streams.append(stream)
        for index in range(40):
            stream.feed(
                {
                    "event": "pane_created",
                    "data": {
                        "type": "pane_created",
                        "pane": {"pane_id": f"w9:p{index}"},
                    },
                }
            )
        return stream

    ticks = {"count": 0}

    def clock():
        ticks["count"] += 1
        if snapshot_calls["count"] >= 3:
            raise Stop()
        if streams and not streams[0]._pending:
            return 0.3
        return ticks["count"] * 0.001

    d = make_daemon(
        client,
        [StaticProbe(None)],
        stream_factory=factory,
        clock=clock,
        reprobe_interval_s=999.0,
        resync_interval_s=999.0,
        progress_interval_s=999.0,
    )
    try:
        d.run(sleep=lambda delay: None)
    except Stop:
        pass

    assert len(streams) == 1
    assert snapshot_calls["first_resync_read_count"] == 40


def test_run_rebootstraps_after_stream_close():
    client = FakeClient([_agent(status="idle")])
    streams = []

    def factory(subs):
        stream = FakeStream(subs)
        streams.append(stream)
        return stream

    d = make_daemon(
        client,
        [StaticProbe(None)],
        stream_factory=factory,
        reprobe_interval_s=0.001,
        resync_interval_s=0.001,
        progress_interval_s=0.001,
    )
    ticks = {"n": 0}

    def clock():
        ticks["n"] += 1
        if ticks["n"] == 20 and streams:
            streams[0].feed({"event": "noop", "data": {}})
            streams[0]._w.close()
        if ticks["n"] > 400:
            raise Stop()
        return float(ticks["n"]) * 0.001

    d._clock = clock
    try:
        d.run(sleep=lambda delay: None)
    except Stop:
        pass
    assert len(streams) >= 2


def test_run_rebootstraps_after_moved_event_closes_registered_stream():
    client = FakeClient([_agent(status="working")])
    streams = []

    def factory(subs):
        stream = FakeStream(subs)
        streams.append(stream)
        return stream

    d = make_daemon(
        client,
        [StaticProbe(Pending("review", 30, "roborev"))],
        stream_factory=factory,
        reprobe_interval_s=0.001,
        resync_interval_s=999.0,
        progress_interval_s=999.0,
    )
    ticks = {"n": 0}

    def clock():
        ticks["n"] += 1
        if ticks["n"] == 20 and streams:
            moved = _agent(pane="w2:p9", status="idle", term="term-w1:p1")
            client.set_agents([moved])
            streams[0].feed(
                {
                    "event": "pane.moved",
                    "data": {
                        "previous_pane_id": "w1:p1",
                        "pane": dict(moved),
                    },
                }
            )
        if ticks["n"] > 400:
            raise Stop()
        return float(ticks["n"]) * 0.001

    d._clock = clock
    try:
        d.run(sleep=_stop_sleep)
    except Stop:
        pass
    assert ("w2:p9", "working", "⏳ review") in client.reports


def test_run_caps_exponential_backoff_while_bootstrap_fails():
    client = FakeClient([])
    client.snapshot_error = HerdrUnavailable("down")
    delays = []

    def sleep(delay):
        delays.append(delay)
        if len(delays) == 5:
            raise SystemExit(0)

    d = make_daemon(
        client,
        stream_factory=lambda subs: FakeStream(subs),
        backoff_base_s=0.25,
        backoff_max_s=1.0,
    )
    try:
        d.run(sleep=sleep)
    except SystemExit:
        pass

    assert delays == [0.25, 0.5, 1.0, 1.0, 1.0]


def test_run_backs_off_when_streams_close_immediately():
    client = FakeClient([_agent(status="idle")])
    attempts = {"n": 0}

    def factory(subs):
        attempts["n"] += 1
        if attempts["n"] == 4:
            raise SystemExit(0)
        stream = FakeStream(subs)
        stream._w.close()
        return stream

    delays = []
    d = make_daemon(
        client,
        [StaticProbe(None)],
        stream_factory=factory,
        backoff_base_s=0.25,
        backoff_max_s=1.0,
    )
    try:
        d.run(sleep=delays.append)
    except SystemExit:
        pass

    assert delays == [0.25, 0.5, 1.0]


def test_startup_resync_does_not_reset_unproven_stream_backoff():
    client = FakeClient([])
    snapshot_calls = {"n": 0}
    streams = []

    def snapshot():
        snapshot_calls["n"] += 1
        if snapshot_calls["n"] <= 2:
            raise HerdrUnavailable("down")
        if snapshot_calls["n"] == 5:
            streams[0]._w.close()
        return {"agents": [_agent(status="working")]}

    def factory(subs):
        stream = FakeStream(subs)
        streams.append(stream)
        return stream

    client.session_snapshot = snapshot
    delays = []

    def sleep(delay):
        delays.append(delay)
        if len(delays) == 3:
            raise SystemExit(0)

    d = make_daemon(
        client,
        [StaticProbe(None)],
        stream_factory=factory,
        clock=time.monotonic,
        backoff_base_s=0.25,
        backoff_max_s=1.0,
        startup_replay_quiet_s=0.001,
        startup_replay_max_s=0.1,
    )
    try:
        d.run(sleep=sleep)
    except SystemExit:
        pass

    assert delays == [0.25, 0.5, 1.0]


def test_run_resets_backoff_when_event_and_eof_arrive_together():
    client = FakeClient([])
    snapshot_calls = {"n": 0}

    def snapshot():
        snapshot_calls["n"] += 1
        if snapshot_calls["n"] <= 2:
            raise HerdrUnavailable("down")
        return {"agents": [_agent()]}

    def factory(subs):
        stream = FakeStream(subs)
        stream.feed({"event": "noop", "data": {}})
        stream._w.close()
        return stream

    client.session_snapshot = snapshot
    delays = []

    def sleep(delay):
        delays.append(delay)
        if len(delays) == 3:
            raise SystemExit(0)

    d = make_daemon(
        client,
        [StaticProbe(None)],
        stream_factory=factory,
        backoff_base_s=0.25,
        backoff_max_s=1.0,
    )
    try:
        d.run(sleep=sleep)
    except SystemExit:
        pass

    assert delays == [0.25, 0.5, 0.25]


def test_run_resets_backoff_when_stream_lived_past_base_interval():
    client = FakeClient([])
    snapshot_calls = {"n": 0}
    streams = []

    def snapshot():
        snapshot_calls["n"] += 1
        if snapshot_calls["n"] <= 2:
            raise HerdrUnavailable("down")
        return {"agents": [_agent(status="working")]}

    def factory(subs):
        stream = FakeStream(subs)
        streams.append(stream)
        return stream

    client.session_snapshot = snapshot
    ticks = {"n": 0}

    def clock():
        ticks["n"] += 1
        if streams and ticks["n"] == 4:
            streams[0]._w.close()
        return 1.0 if ticks["n"] >= 4 else 0.0

    delays = []

    def sleep(delay):
        delays.append(delay)
        if len(delays) == 3:
            raise SystemExit(0)

    d = make_daemon(
        client,
        [StaticProbe(None)],
        stream_factory=factory,
        clock=clock,
        backoff_base_s=0.25,
        backoff_max_s=1.0,
    )
    try:
        d.run(sleep=sleep)
    except SystemExit:
        pass

    assert delays == [0.25, 0.5, 0.25]


def test_run_resets_backoff_when_event_intentionally_replaces_stream():
    client = FakeClient([])
    snapshot_calls = {"n": 0}
    old = _agent(status="working")
    moved = _agent(pane="w2:p9", status="working", term="term-w1:p1")

    def snapshot():
        snapshot_calls["n"] += 1
        if snapshot_calls["n"] <= 2 or snapshot_calls["n"] == 5:
            raise HerdrUnavailable("down")
        return {"agents": [dict(old)]}

    def factory(subs):
        stream = FakeStream(subs)
        stream.feed(
            {
                "event": "pane.moved",
                "data": {
                    "previous_pane_id": "w1:p1",
                    "pane": dict(moved),
                },
            }
        )
        return stream

    client.session_snapshot = snapshot
    delays = []

    def sleep(delay):
        delays.append(delay)
        if len(delays) == 3:
            raise SystemExit(0)

    d = make_daemon(
        client,
        [StaticProbe(None)],
        stream_factory=factory,
        backoff_base_s=0.25,
        backoff_max_s=1.0,
    )
    try:
        d.run(sleep=sleep)
    except SystemExit:
        pass

    assert delays == [0.25, 0.5, 0.25]


def test_run_resets_backoff_after_reconnected_stream_is_healthy():
    client = FakeClient([])
    snapshot_calls = {"n": 0}
    streams = []

    def snapshot():
        snapshot_calls["n"] += 1
        if snapshot_calls["n"] == 1:
            raise HerdrUnavailable("down")
        return {"agents": [_agent()]}

    def factory(subs):
        stream = FakeStream(subs)
        streams.append(stream)
        return stream

    client.session_snapshot = snapshot
    delays = []

    def sleep(delay):
        delays.append(delay)
        if len(delays) == 2:
            raise SystemExit(0)

    ticks = {"n": 0}

    def clock():
        ticks["n"] += 1
        if streams and ticks["n"] == 3:
            streams[0].feed({"event": "noop", "data": {}})
        if streams and ticks["n"] == 6:
            streams[0]._w.close()
        return float(ticks["n"]) * 0.001

    d = make_daemon(
        client,
        [StaticProbe(None)],
        stream_factory=factory,
        clock=clock,
        backoff_base_s=0.25,
        backoff_max_s=1.0,
    )
    try:
        d.run(sleep=sleep)
    except SystemExit:
        pass

    assert delays == [0.25, 0.25]


def test_run_registers_shutdown_and_sigterm_cleans_owned_hold(monkeypatch):
    client = FakeClient([_agent(status="idle")])
    d = make_daemon(
        client,
        [StaticProbe(Pending("review", 30, "roborev"))],
        reprobe_interval_s=0,
    )
    seed(d, client)
    d._reprobe_sweep()
    registered = []
    handlers = {}
    monkeypatch.setattr("herdwatch.daemon.atexit.register", registered.append)
    monkeypatch.setattr(
        "herdwatch.daemon.signal.signal",
        lambda signum, handler: handlers.setdefault(signum, handler),
    )

    try:
        d.run(sleep=lambda delay: (_ for _ in ()).throw(SystemExit(0)))
    except SystemExit:
        pass

    assert registered == [d.shutdown]
    handler = next(iter(handlers.values()))
    try:
        handler(None, None)
    except SystemExit:
        pass
    assert client.releases == ["w1:p1"]
    assert d.managed == {}


def test_build_daemon_constructs_with_new_wiring():
    from herdwatch.config import Config
    from herdwatch.daemon import build_daemon

    class FakeC:
        def pane_process_info(self, pid):
            return {}

    cfg = Config(resync_interval_s=90.0, progress_interval_s=2.0)
    d = build_daemon(cfg, client=FakeC())
    assert len(d._probes) == 3
    assert d._resync_interval == 90.0
    assert d._progress_interval == 2.0
    assert d._stream_factory is not None
