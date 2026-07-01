# tests/test_daemon.py
from herdwatch.daemon import Daemon, SOURCE
from herdwatch.gitctx import GitInfo
from herdwatch.models import Pending

class FakeClient:
    def __init__(self, agents):
        self.agents = agents
        self.reports = []
        self.releases = []
    def agent_list(self):
        return self.agents
    def report_agent(self, pane_id, source, agent, state, custom_status=None):
        self.reports.append((pane_id, state, custom_status))
    def release_agent(self, pane_id, source, agent):
        self.releases.append(pane_id)

class StaticProbe:
    name = "static"
    def __init__(self, result):
        self.result = result
    def check(self, ctx):
        return self.result

_ENRICH = lambda cwd: GitInfo(True, "sha", "main", True)

def _agent(pane="w1:p1", status="idle"):
    return {"pane_id": pane, "agent_status": status, "agent": "claude", "cwd": "/x"}

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

def test_drops_vanished_pane():
    client = FakeClient([_agent(status="idle")])
    d = Daemon(client, [StaticProbe(Pending("x", 10, "marker"))], clock=lambda: 0.0, enrich=_ENRICH)
    d.tick()
    client.agents = []
    d.tick()
    assert d.managed == {}

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

def test_tick_snapshots_managed_rows():
    client = FakeClient([_agent(status="idle")])
    snaps = []
    d = Daemon(client, [StaticProbe(Pending("review", 30, "roborev"))],
               clock=lambda: 0.0, enrich=_ENRICH, on_snapshot=snaps.append)
    d.tick()
    assert snaps[-1] == [{"pane_id": "w1:p1", "agent": "claude", "status": "⏳ review"}]

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

def test_build_daemon_constructs():
    from herdwatch.config import Config
    from herdwatch.daemon import build_daemon
    class FakeC:
        def pane_process_info(self, pid):
            return {}
    d = build_daemon(Config(), client=FakeC())
    assert len(d._probes) == 4  # all four probes enabled by default
