from herdwatch.cache import TTLCache
from herdwatch.models import PaneContext
from herdwatch.probes.roborev import RoborevProbe


def _ctx(**kw):
    d = dict(pane_id="w1:p1", agent="claude", cwd="/x", status="idle",
             head_sha="abc", branch="main", is_git_repo=True, has_github_remote=True)
    d.update(kw)
    return PaneContext(**d)


def _cache():
    return TTLCache(ttl_s=10, clock=lambda: 0.0)


_BUSY = {"daemon": {"queued_jobs": 1, "running_jobs": 0}}
_IDLE = {"daemon": {"queued_jobs": 0, "running_jobs": 0}}


def test_gate_skips_when_queue_empty():
    probe = RoborevProbe(_cache(), run_status=lambda: _IDLE,
                         run_list=lambda cwd: [{"git_ref": "abc", "status": "running"}])
    assert probe.check(_ctx()) is None


def test_pending_when_job_running_for_head():
    probe = RoborevProbe(_cache(), run_status=lambda: _BUSY,
                         run_list=lambda cwd: [{"git_ref": "abc", "status": "queued"}])
    p = probe.check(_ctx())
    assert p is not None and p.label == "review" and p.priority == 30 and p.source == "roborev"


def test_none_when_job_done():
    probe = RoborevProbe(_cache(), run_status=lambda: _BUSY,
                         run_list=lambda cwd: [{"git_ref": "abc", "status": "done"}])
    assert probe.check(_ctx()) is None


def test_non_dict_job_is_skipped():
    probe = RoborevProbe(_cache(), run_status=lambda: _BUSY,
                         run_list=lambda cwd: ["garbage", {"git_ref": "abc", "status": "running"}])
    assert probe.check(_ctx()) is not None
