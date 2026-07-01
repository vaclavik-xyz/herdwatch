from herdwatch.cache import TTLCache
from herdwatch.models import PaneContext
from herdwatch.probes.ci import CIProbe

def _ctx(**kw):
    d = dict(pane_id="w1:p1", agent="claude", cwd="/x", status="idle",
             head_sha="abc", branch="main", is_git_repo=True, has_github_remote=True)
    d.update(kw)
    return PaneContext(**d)

def _cache():
    return TTLCache(ttl_s=10, clock=lambda: 0.0)

def test_skip_when_no_github_remote():
    probe = CIProbe(_cache(), run_gh=lambda cwd, br: [{"headSha": "abc", "status": "in_progress", "workflowName": "ci"}])
    assert probe.check(_ctx(has_github_remote=False)) is None

def test_pending_when_run_in_progress_for_head():
    probe = CIProbe(_cache(), run_gh=lambda cwd, br: [{"headSha": "abc", "status": "in_progress", "workflowName": "lint"}])
    p = probe.check(_ctx())
    assert p is not None and p.label == "CI: lint" and p.priority == 20 and p.source == "ci"

def test_none_when_only_completed():
    probe = CIProbe(_cache(), run_gh=lambda cwd, br: [{"headSha": "abc", "status": "completed", "workflowName": "ci"}])
    assert probe.check(_ctx()) is None

def test_none_when_run_is_for_other_sha():
    probe = CIProbe(_cache(), run_gh=lambda cwd, br: [{"headSha": "zzz", "status": "queued", "workflowName": "ci"}])
    assert probe.check(_ctx()) is None
