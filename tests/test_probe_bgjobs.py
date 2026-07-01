from herdwatch.models import PaneContext
from herdwatch.probes.bgjobs import BgJobsProbe


def _ctx():
    return PaneContext("w1:p1", "claude", "/x", "idle", "sha", "main", True, True)


_INFO = {"shell_pid": 100, "foreground_process_group_id": 100}


def test_none_when_no_shell_pid():
    probe = BgJobsProbe(process_info=lambda pid: {}, list_descendants=lambda root: [])
    assert probe.check(_ctx()) is None


def test_ignores_foreground_group():
    desc = [{"pid": 101, "pgid": 100, "etime_s": 60, "comm": "npm"}]
    probe = BgJobsProbe(process_info=lambda pid: _INFO, list_descendants=lambda root: desc)
    assert probe.check(_ctx()) is None  # pgid == foreground group


def test_pending_for_backgrounded_job():
    desc = [{"pid": 202, "pgid": 202, "etime_s": 60, "comm": "pytest"}]
    probe = BgJobsProbe(process_info=lambda pid: _INFO, list_descendants=lambda root: desc)
    p = probe.check(_ctx())
    assert p is not None and p.label == "pytest" and p.priority == 10 and p.source == "bgjobs"


def test_ignores_young_jobs():
    desc = [{"pid": 202, "pgid": 202, "etime_s": 2, "comm": "pytest"}]
    probe = BgJobsProbe(process_info=lambda pid: _INFO, list_descendants=lambda root: desc, min_age_s=5)
    assert probe.check(_ctx()) is None
