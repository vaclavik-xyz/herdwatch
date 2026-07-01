from herdwatch.cache import TTLCache
from herdwatch.models import PaneContext, WorktreeHead
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

def test_pending_when_run_queued_for_head():
    probe = CIProbe(_cache(), run_gh=lambda cwd, br: [{"headSha": "abc", "status": "queued", "workflowName": "ci"}])
    p = probe.check(_ctx())
    assert p is not None and p.label == "CI: ci" and p.priority == 20 and p.source == "ci"

def test_none_when_only_completed():
    probe = CIProbe(_cache(), run_gh=lambda cwd, br: [{"headSha": "abc", "status": "completed", "workflowName": "ci"}])
    assert probe.check(_ctx()) is None

def test_none_when_run_is_for_other_sha():
    probe = CIProbe(_cache(), run_gh=lambda cwd, br: [{"headSha": "zzz", "status": "queued", "workflowName": "ci"}])
    assert probe.check(_ctx()) is None

def test_non_dict_run_is_skipped():
    probe = CIProbe(_cache(), run_gh=lambda cwd, br: ["garbage", {"headSha": "abc", "status": "in_progress", "workflowName": "ci"}])
    p = probe.check(_ctx())
    assert p is not None and p.label == "CI: ci"

def test_malformed_top_level_runs_return_none():
    probe = CIProbe(_cache(), run_gh=lambda cwd, br: 1)
    assert probe.check(_ctx()) is None


_TWO_HEADS = (WorktreeHead(head_sha="abc", branch="main"),
              WorktreeHead(head_sha="def", branch="feat/x"))


def test_pending_when_run_matches_worktree_head_not_cwd_head():
    # cwd checkout sits on main@abc with no CI, the agent's worktree is on
    # feat/x@def where a PR run is in progress -> the pane must be held
    def run_gh(cwd, br):
        return [{"headSha": "def", "status": "in_progress", "workflowName": "CI"}] \
            if br == "feat/x" else []
    probe = CIProbe(_cache(), run_gh=run_gh)
    p = probe.check(_ctx(worktree_heads=_TWO_HEADS))
    assert p is not None and p.label == "CI: CI"


def test_queries_each_worktree_head_branch():
    calls = []
    probe = CIProbe(_cache(), run_gh=lambda cwd, br: calls.append(br) or [])
    assert probe.check(_ctx(worktree_heads=_TWO_HEADS)) is None
    assert calls == ["main", "feat/x"]


def test_none_when_all_heads_only_completed():
    probe = CIProbe(_cache(), run_gh=lambda cwd, br: [
        {"headSha": "abc", "status": "completed", "workflowName": "ci"},
        {"headSha": "def", "status": "completed", "workflowName": "ci"}])
    assert probe.check(_ctx(worktree_heads=_TWO_HEADS)) is None


def test_malformed_runs_for_one_head_do_not_mask_another():
    probe = CIProbe(_cache(), run_gh=lambda cwd, br: 1 if br == "main" else [
        {"headSha": "def", "status": "queued", "workflowName": "ci"}])
    p = probe.check(_ctx(worktree_heads=_TWO_HEADS))
    assert p is not None and p.label == "CI: ci"


def test_worktree_run_not_matched_to_wrong_sha():
    # a run on feat/x whose sha matches no local head (e.g. remote moved on)
    # must not hold the pane
    probe = CIProbe(_cache(), run_gh=lambda cwd, br: [
        {"headSha": "zzz", "status": "in_progress", "workflowName": "ci"}])
    assert probe.check(_ctx(worktree_heads=_TWO_HEADS)) is None
