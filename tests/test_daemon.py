# tests/test_daemon.py
from herdwatch.daemon import Daemon, ManagedPane, SOURCE, TTL_MAX_MS, TTL_MIN_MS
from herdwatch.gitctx import GitInfo
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
    return Daemon(client, list(probes), **kw)


def seed(d, client):
    """Load the fake's agents into the registry (what bootstrap/resync do)."""
    d._registry = {pane: dict(agent) for pane, agent in client.agents.items()}
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
