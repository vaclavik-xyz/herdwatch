# tests/test_daemon.py
from herdwatch.daemon import Daemon, SOURCE
from herdwatch.gitctx import GitInfo
from herdwatch.models import Pending, WorktreeHead

class FakeClient:
    def __init__(self, agents, report_ok=True, release_ok=True, explain="idle"):
        self.agents = agents
        self.reports = []
        self.releases = []
        self.explains = []
        self._report_ok = report_ok
        self._release_ok = release_ok
        self.explain = explain  # state returned by agent_explain, or None
    def agent_list(self):
        return self.agents
    def agent_explain(self, pane_id):
        self.explains.append(pane_id)
        return self.explain
    def report_agent(self, pane_id, source, agent, state, custom_status=None):
        self.reports.append((pane_id, state, custom_status))
        return self._report_ok
    def release_agent(self, pane_id, source, agent):
        self.releases.append(pane_id)
        return self._release_ok

class StaticProbe:
    name = "static"
    def __init__(self, result):
        self.result = result
    def check(self, ctx):
        return self.result

_ENRICH = lambda cwd: GitInfo(True, "sha", "main", True)

def _agent(pane="w1:p1", status="idle"):
    return {"pane_id": pane, "agent_status": status, "agent": "claude", "cwd": "/x"}


def _claude_agent(pane="w1:p1", status="working", session="c00b128f-68c8-4643-82d6-2835c317517d"):
    return {"pane_id": pane, "agent_status": status, "agent": "claude", "cwd": "/x",
            "agent_session": {"value": session}}

def test_context_carries_worktree_heads_to_probes():
    heads = (WorktreeHead(head_sha="sha", branch="main"),
             WorktreeHead(head_sha="wt", branch="feat/x"))
    seen = []
    class Capture:
        name = "capture"
        def check(self, ctx):
            seen.append(ctx.worktree_heads)
            return None
    client = FakeClient([_agent(status="idle")])
    d = Daemon(client, [Capture()], clock=lambda: 0.0,
               enrich=lambda cwd: GitInfo(True, "sha", "main", True, heads))
    d.tick()
    assert seen == [heads]


def test_asserts_working_when_pending():
    client = FakeClient([_agent(status="idle")])
    d = Daemon(client, [StaticProbe(Pending("review", 30, "roborev"))],
               clock=lambda: 0.0, enrich=_ENRICH)
    d.tick()
    assert client.reports == [("w1:p1", "working", "⏳ review")]
    assert "w1:p1" in d.managed

def test_ignores_working_pane_not_managed():
    client = FakeClient([_agent(status="working")])
    d = Daemon(client, [StaticProbe(Pending("x", 10, "marker"))], clock=lambda: 0.0, enrich=_ENRICH)
    d.tick()
    assert client.reports == []

def test_releases_when_cleared():
    client = FakeClient([_agent(status="idle")])
    probe = StaticProbe(Pending("review", 30, "roborev"))
    d = Daemon(client, [probe], reprobe_interval_s=0, clock=lambda: 0.0, enrich=_ENRICH)
    d.tick()
    probe.result = None
    d.tick()
    assert client.releases == ["w1:p1"]
    assert "w1:p1" not in d.managed

def test_reasserts_only_on_label_change():
    client = FakeClient([_agent(status="idle")])
    probe = StaticProbe(Pending("review", 30, "roborev"))
    d = Daemon(client, [probe], reprobe_interval_s=0, clock=lambda: 0.0, enrich=_ENRICH)
    d.tick()
    d.tick()
    assert len(client.reports) == 1  # unchanged label -> no duplicate report

def test_releases_assertion_when_pane_vanishes():
    # a managed pane that drops out of agent_list must be released, not just
    # forgotten -- otherwise herdr keeps showing an orphaned `working ⏳` session.
    client = FakeClient([_agent(status="idle")])
    d = Daemon(client, [StaticProbe(Pending("x", 10, "marker"))], clock=lambda: 0.0, enrich=_ENRICH)
    d.tick()
    assert "w1:p1" in d.managed
    client.agents = []
    d.tick()
    assert d.managed == {}
    assert client.releases == ["w1:p1"]  # assertion released, not orphaned

def test_raising_probe_does_not_crash_tick():
    client = FakeClient([_agent(status="idle")])
    class Boom:
        name = "boom"
        def check(self, ctx):
            raise RuntimeError("probe exploded")
    d = Daemon(client, [Boom()], clock=lambda: 0.0, enrich=_ENRICH)
    d.tick()  # must not raise
    assert client.reports == []  # raising probe treated as no pending
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
    d = Daemon(client, [Counting()], reprobe_interval_s=15, clock=lambda: now[0], enrich=_ENRICH)
    d.tick()
    assert len(calls) == 1          # first probe
    now[0] = 5.0
    d.tick()
    assert len(calls) == 1          # within interval -> throttled, not re-probed
    now[0] = 20.0
    d.tick()
    assert len(calls) == 2          # past interval -> re-probed

def test_managed_pane_released_when_cleared_even_if_status_working():
    client = FakeClient([_agent(status="idle")])
    probe = StaticProbe(Pending("review", 30, "roborev"))
    d = Daemon(client, [probe], reprobe_interval_s=0, clock=lambda: 0.0, enrich=_ENRICH)
    d.tick()                              # managed + asserted
    client.agents = [_agent(status="working")]  # pane now shows real work
    probe.result = None                   # background cleared
    d.tick()
    assert client.releases == ["w1:p1"]   # managed pane still re-probed and released
    assert "w1:p1" not in d.managed

def test_deny_skips_pane():
    client = FakeClient([_agent(status="idle")])
    d = Daemon(client, [StaticProbe(Pending("review", 30, "roborev"))],
               clock=lambda: 0.0, enrich=_ENRICH, deny=["w1:p1"])
    d.tick()
    assert client.reports == []
    assert d.managed == {}

def test_allow_only_listed():
    client = FakeClient([_agent(status="idle")])
    d = Daemon(client, [StaticProbe(Pending("review", 30, "roborev"))],
               clock=lambda: 0.0, enrich=_ENRICH, allow=["w2:p2"])
    d.tick()
    assert client.reports == []  # w1:p1 not in allow-list

def test_unmanaged_idle_pane_is_throttled():
    now = [0.0]
    calls = []
    class Counting:
        name = "counting"
        def check(self, ctx):
            calls.append(now[0])
            return None  # never pending -> pane stays unmanaged
    client = FakeClient([_agent(status="idle")])
    d = Daemon(client, [Counting()], reprobe_interval_s=15, clock=lambda: now[0], enrich=_ENRICH)
    d.tick()
    assert len(calls) == 1
    now[0] = 5.0
    d.tick()
    assert len(calls) == 1   # unmanaged idle pane throttled, not re-probed every tick
    now[0] = 20.0
    d.tick()
    assert len(calls) == 2

def test_release_all_releases_managed():
    client = FakeClient([_agent(status="idle")])
    d = Daemon(client, [StaticProbe(Pending("review", 30, "roborev"))],
               reprobe_interval_s=0, clock=lambda: 0.0, enrich=_ENRICH)
    d.tick()
    assert "w1:p1" in d.managed
    d.release_all()
    assert client.releases == ["w1:p1"]
    assert d.managed == {}

def test_failed_release_keeps_pane_for_retry():
    # herdr down -> release_agent returns False -> keep bookkeeping so the next
    # tick retries instead of orphaning the assertion.
    client = FakeClient([_agent(status="idle")], release_ok=False)
    probe = StaticProbe(Pending("review", 30, "roborev"))
    d = Daemon(client, [probe], reprobe_interval_s=0, clock=lambda: 0.0, enrich=_ENRICH)
    d.tick()
    client.agents = []          # pane vanishes
    d.tick()
    assert "w1:p1" in d.managed  # release failed -> still managed
    client._release_ok = True
    client.agents = []
    d.tick()
    assert "w1:p1" not in d.managed  # retry succeeded

def test_failed_report_does_not_record_managed():
    # herdr down -> report_agent returns False -> don't claim we hold the pane.
    client = FakeClient([_agent(status="idle")], report_ok=False)
    d = Daemon(client, [StaticProbe(Pending("review", 30, "roborev"))],
               clock=lambda: 0.0, enrich=_ENRICH)
    d.tick()
    assert d.managed == {}

def test_failed_report_is_not_throttled():
    # a failed report must be retried promptly, not deferred a full reprobe interval
    client = FakeClient([_agent(status="idle")], report_ok=False)
    d = Daemon(client, [StaticProbe(Pending("review", 30, "roborev"))],
               reprobe_interval_s=15, clock=lambda: 0.0, enrich=_ENRICH)
    d.tick()
    assert d.managed == {}
    client._report_ok = True
    d.tick()  # frozen clock, within reprobe window: must still retry, not throttle
    assert "w1:p1" in d.managed

def test_failed_work_cleared_release_is_not_throttled():
    now = [0.0]
    client = FakeClient([_agent(status="idle")], release_ok=False)
    probe = StaticProbe(Pending("review", 30, "roborev"))
    d = Daemon(client, [probe], reprobe_interval_s=15, clock=lambda: now[0], enrich=_ENRICH)
    d.tick()                       # t=0: hold
    probe.result = None            # work cleared
    now[0] = 20.0                  # past reprobe -> re-probe, release attempted, fails
    d.tick()
    assert "w1:p1" in d.managed    # release failed -> retained
    client._release_ok = True
    d.tick()                       # still t=20: throttle was cleared -> retry now
    assert "w1:p1" not in d.managed

def test_tick_snapshots_managed_rows():
    client = FakeClient([_agent(status="idle")])
    snaps = []
    d = Daemon(client, [StaticProbe(Pending("review", 30, "roborev"))],
               clock=lambda: 0.0, enrich=_ENRICH, on_snapshot=snaps.append)
    d.tick()
    assert snaps[-1] == [{"pane_id": "w1:p1", "agent": "claude", "status": "⏳ review", "kind": "hold"}]

def test_tick_snapshots_empty_when_nothing_held():
    client = FakeClient([_agent(status="idle")])
    snaps = []
    d = Daemon(client, [StaticProbe(None)], clock=lambda: 0.0, enrich=_ENRICH,
               on_snapshot=snaps.append)
    d.tick()
    assert snaps[-1] == []

def test_release_all_snapshots_empty():
    client = FakeClient([_agent(status="idle")])
    snaps = []
    d = Daemon(client, [StaticProbe(Pending("review", 30, "roborev"))],
               reprobe_interval_s=0, clock=lambda: 0.0, enrich=_ENRICH,
               on_snapshot=snaps.append)
    d.tick()
    d.release_all()
    assert snaps[-1] == []

def test_raising_snapshot_does_not_crash_tick():
    client = FakeClient([_agent(status="idle")])
    def boom(rows):
        raise RuntimeError("disk full")
    d = Daemon(client, [StaticProbe(Pending("review", 30, "roborev"))],
               clock=lambda: 0.0, enrich=_ENRICH, on_snapshot=boom)
    d.tick()  # must not raise
    assert "w1:p1" in d.managed  # managed state still correct despite snapshot failure

def test_adopt_seeds_managed_from_rows():
    d = Daemon(FakeClient([]), [], clock=lambda: 0.0, enrich=_ENRICH)
    d.adopt([{"pane_id": "w1:p1", "agent": "claude", "status": "⏳ review"}])
    assert "w1:p1" in d.managed
    assert d.managed["w1:p1"].agent == "claude"
    assert d.managed["w1:p1"].custom_status == "⏳ review"

def test_adopt_ignores_rows_without_pane_id():
    d = Daemon(FakeClient([]), [], clock=lambda: 0.0, enrich=_ENRICH)
    d.adopt([{"agent": "claude", "status": "⏳ x"}, {"pane_id": "", "agent": "c"}])
    assert d.managed == {}


def test_adopt_defaults_kind_to_hold():
    d = Daemon(FakeClient([]), [], clock=lambda: 0.0, enrich=_ENRICH)
    d.adopt([{"pane_id": "w1:p1", "agent": "claude", "status": "⏳ review"}])
    assert d.managed["w1:p1"].kind == "hold"


def test_adopt_preserves_progress_kind():
    d = Daemon(FakeClient([]), [], clock=lambda: 0.0, enrich=_ENRICH)
    d.adopt([{"pane_id": "w1:p1", "agent": "claude",
              "status": "2/5 Fixing auth", "kind": "progress"}])
    assert d.managed["w1:p1"].kind == "progress"

def test_adopted_pane_released_when_work_already_cleared():
    # a pane we held before a crash, whose background work finished while we were
    # down, must be released on restart -- not left as an orphan.
    client = FakeClient([_agent(status="idle")])
    d = Daemon(client, [StaticProbe(None)], reprobe_interval_s=0, clock=lambda: 0.0, enrich=_ENRICH)
    d.adopt([{"pane_id": "w1:p1", "agent": "claude", "status": "⏳ review"}])
    d.tick()
    assert client.releases == ["w1:p1"]
    assert "w1:p1" not in d.managed

def test_adopted_pane_gone_is_released_on_restart():
    # pane closed while the daemon was down -> not in agent_list -> released.
    client = FakeClient([])
    d = Daemon(client, [StaticProbe(None)], clock=lambda: 0.0, enrich=_ENRICH)
    d.adopt([{"pane_id": "w9:p9", "agent": "codex", "status": "⏳ CI: ci"}])
    d.tick()
    assert client.releases == ["w9:p9"]
    assert d.managed == {}

def test_adopted_pane_kept_when_still_pending():
    client = FakeClient([_agent(status="idle")])
    d = Daemon(client, [StaticProbe(Pending("review", 30, "roborev"))],
               reprobe_interval_s=0, clock=lambda: 0.0, enrich=_ENRICH)
    d.adopt([{"pane_id": "w1:p1", "agent": "claude", "status": "⏳ review"}])
    d.tick()
    assert "w1:p1" in d.managed
    assert client.releases == []

def test_adopted_pending_pane_is_reasserted_once():
    # herdr may have lost the old assertion while we were down, so the first tick
    # must re-report even though the label matches the adopted one -- then settle.
    client = FakeClient([_agent(status="idle")])
    d = Daemon(client, [StaticProbe(Pending("review", 30, "roborev"))],
               reprobe_interval_s=0, clock=lambda: 0.0, enrich=_ENRICH)
    d.adopt([{"pane_id": "w1:p1", "agent": "claude", "status": "⏳ review"}])
    d.tick()
    assert client.reports == [("w1:p1", "working", "⏳ review")]  # re-asserted despite same label
    d.tick()
    assert len(client.reports) == 1  # adopted flag cleared -> no duplicate re-report

def test_build_daemon_constructs():
    from herdwatch.config import Config
    from herdwatch.daemon import build_daemon
    class FakeC:
        def pane_process_info(self, pid):
            return {}
    d = build_daemon(Config(), client=FakeC())
    assert len(d._probes) == 3  # roborev, ci, marker on by default (bgjobs opt-in)


def test_progress_asserts_label_for_working_claude_pane():
    client = FakeClient([_claude_agent()])
    d = Daemon(client, [], clock=lambda: 0.0, enrich=_ENRICH,
               progress=lambda sid: "2/5 Fixing auth")
    d.tick()
    assert client.reports == [("w1:p1", "working", "2/5 Fixing auth")]
    assert d.managed["w1:p1"].kind == "progress"
    assert client.explains == []  # unmanaged pane: agent_status is the truth


def test_progress_reasserts_only_on_label_change():
    client = FakeClient([_claude_agent()], explain="working")
    d = Daemon(client, [], clock=lambda: 0.0, enrich=_ENRICH,
               progress=lambda sid: "2/5 Fixing auth")
    d.tick()
    d.tick()
    assert len(client.reports) == 1
    labels = iter(["3/5 Writing tests"])
    d._progress = lambda sid: next(labels)
    d.tick()
    assert client.reports[-1] == ("w1:p1", "working", "3/5 Writing tests")


def test_progress_released_when_detection_says_stopped():
    client = FakeClient([_claude_agent()], explain="working")
    d = Daemon(client, [], reprobe_interval_s=0, clock=lambda: 0.0, enrich=_ENRICH,
               progress=lambda sid: "2/5 Fixing auth")
    d.tick()
    client.explain = "idle"
    d.tick()
    assert client.releases == ["w1:p1"]
    assert "w1:p1" not in d.managed


def test_progress_hands_over_to_hold_in_same_tick():
    # agent stops with CI still running: release progress, assert ⏳ hold now
    client = FakeClient([_claude_agent()], explain="working")
    probe = StaticProbe(None)
    d = Daemon(client, [probe], reprobe_interval_s=0, clock=lambda: 0.0,
               enrich=_ENRICH, progress=lambda sid: "2/5 Fixing auth")
    d.tick()
    client.explain = "idle"
    probe.result = Pending("CI: ci", 20, "ci")
    d.tick()
    assert client.releases == ["w1:p1"]
    assert client.reports[-1] == ("w1:p1", "working", "⏳ CI: ci")
    assert d.managed["w1:p1"].kind == "hold"


def test_progress_released_when_no_active_task():
    client = FakeClient([_claude_agent()], explain="working")
    labels = iter(["2/5 Fixing auth", None])
    d = Daemon(client, [], clock=lambda: 0.0, enrich=_ENRICH,
               progress=lambda sid: next(labels))
    d.tick()
    d.tick()
    assert client.releases == ["w1:p1"]


def test_progress_released_when_explain_fails():
    client = FakeClient([_claude_agent()], explain="working")
    d = Daemon(client, [], clock=lambda: 0.0, enrich=_ENRICH,
               progress=lambda sid: "2/5 Fixing auth")
    d.tick()
    client.explain = None  # herdr hiccup: prefer releasing over masking
    d.tick()
    assert client.releases == ["w1:p1"]


def test_progress_skips_non_claude_agents():
    agent = {"pane_id": "w1:p1", "agent_status": "working", "agent": "codex",
             "cwd": "/x", "agent_session": {"value": "abc"}}
    client = FakeClient([agent])
    d = Daemon(client, [], clock=lambda: 0.0, enrich=_ENRICH,
               progress=lambda sid: "2/5 X")
    d.tick()
    assert client.reports == []


def test_progress_disabled_leaves_working_panes_alone():
    client = FakeClient([_claude_agent()])
    d = Daemon(client, [], clock=lambda: 0.0, enrich=_ENRICH, progress=None)
    d.tick()
    assert client.reports == []


def test_progress_reader_exception_is_contained():
    def boom(sid):
        raise RuntimeError("bad file")
    client = FakeClient([_claude_agent()])
    d = Daemon(client, [], clock=lambda: 0.0, enrich=_ENRICH, progress=boom)
    d.tick()  # must not raise
    assert client.reports == []


def test_hold_pane_not_probed_by_progress_path():
    # an idle pane held for CI must stay a hold even if its session has an
    # in_progress task (holds own the pane until their work clears)
    client = FakeClient([_claude_agent(status="idle")], explain="idle")
    probe = StaticProbe(Pending("CI: ci", 20, "ci"))
    d = Daemon(client, [probe], reprobe_interval_s=0, clock=lambda: 0.0,
               enrich=_ENRICH, progress=lambda sid: "2/5 X")
    d.tick()
    d.tick()
    assert d.managed["w1:p1"].kind == "hold"
    assert all(s == "⏳ CI: ci" for (_, _, s) in client.reports)
