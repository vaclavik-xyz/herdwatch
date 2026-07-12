from herdwatch.cache import TTLCache
from herdwatch.aggregate import aggregate
from herdwatch.models import PaneContext, PanePeer, Pending, WorktreeHead
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


def test_same_sha_on_two_branches_queries_both():
    # a fresh worktree branch still sits on the same sha as main; the run
    # exists only under the worktree's branch, so the branch-filtered query
    # for main must not be reused for it (cache key must include the branch)
    same_sha = (WorktreeHead(head_sha="abc", branch="main"),
                WorktreeHead(head_sha="abc", branch="feat/x"))
    def run_gh(cwd, br):
        return [{"headSha": "abc", "status": "in_progress", "workflowName": "ci"}] \
            if br == "feat/x" else []
    probe = CIProbe(_cache(), run_gh=run_gh)
    p = probe.check(_ctx(worktree_heads=same_sha))
    assert p is not None and p.label == "CI: ci"


def test_worktree_run_not_matched_to_wrong_sha():
    # a run on feat/x whose sha matches no local head (e.g. remote moved on)
    # must not hold the pane
    probe = CIProbe(_cache(), run_gh=lambda cwd, br: [
        {"headSha": "zzz", "status": "in_progress", "workflowName": "ci"}])
    assert probe.check(_ctx(worktree_heads=_TWO_HEADS)) is None


def test_pending_run_is_owned_by_only_working_repo_peer():
    peers = (
        PanePeer("w1:p1", "idle", "abc", "main"),
        PanePeer("w1:p2", "working", "abc", "main"),
    )

    def run_gh(cwd, branch):
        if branch == "feat/x":
            return [{
                "headSha": "def",
                "status": "in_progress",
                "workflowName": "CI",
            }]
        return []

    probe = CIProbe(_cache(), run_gh=run_gh)
    common = dict(
        worktree_heads=_TWO_HEADS,
        repo_key="/repo/.git",
        repo_peers=peers,
    )

    assert probe.check(_ctx(pane_id="w1:p1", **common)) is None
    pending = probe.check(
        _ctx(pane_id="w1:p2", status="working", **common)
    )

    assert pending is not None
    assert pending.label == "CI: CI"
    assert pending.show_while_working is True


def test_pending_owner_is_retained_after_becoming_idle():
    same_sha_heads = (
        WorktreeHead(head_sha="abc", branch="main"),
        WorktreeHead(head_sha="abc", branch="feat/x"),
    )
    working_peers = (
        PanePeer("w1:p1", "idle", "abc", "main"),
        PanePeer("w1:p2", "working", "abc", "main"),
    )
    idle_peers = (
        PanePeer("w1:p1", "idle", "abc", "main"),
        PanePeer("w1:p2", "idle", "abc", "main"),
    )

    def run_gh(cwd, branch):
        if branch == "feat/x":
            return [{
                "headSha": "abc",
                "status": "queued",
                "workflowName": "CI",
            }]
        return []

    probe = CIProbe(_cache(), run_gh=run_gh)
    common = dict(worktree_heads=same_sha_heads, repo_key="/repo/.git")

    assert probe.check(_ctx(
        pane_id="w1:p2",
        status="working",
        repo_peers=working_peers,
        **common,
    )) is not None
    assert probe.check(_ctx(
        pane_id="w1:p1",
        repo_peers=idle_peers,
        **common,
    )) is None
    assert probe.check(_ctx(
        pane_id="w1:p2",
        repo_peers=idle_peers,
        **common,
    )) is not None


def test_ambiguous_idle_repo_peers_do_not_receive_ci_badge():
    peers = (
        PanePeer("w1:p1", "idle", "abc", "main"),
        PanePeer("w1:p2", "idle", "abc", "main"),
    )
    probe = CIProbe(_cache(), run_gh=lambda cwd, branch: [{
        "headSha": "def",
        "status": "in_progress",
        "workflowName": "CI",
    }] if branch == "feat/x" else [])
    common = dict(
        worktree_heads=_TWO_HEADS,
        repo_key="/repo/.git",
        repo_peers=peers,
    )

    assert probe.check(_ctx(pane_id="w1:p1", **common)) is None
    assert probe.check(_ctx(pane_id="w1:p2", **common)) is None


def test_exact_checkout_owner_wins_over_working_repo_peer():
    same_sha_heads = (
        WorktreeHead(head_sha="abc", branch="main"),
        WorktreeHead(head_sha="abc", branch="feat/x"),
    )
    peers = (
        PanePeer("w1:p1", "working", "abc", "main"),
        PanePeer("w1:p2", "idle", "abc", "feat/x"),
    )
    probe = CIProbe(_cache(), run_gh=lambda cwd, branch: [{
        "headSha": "abc",
        "status": "in_progress",
        "workflowName": "CI",
    }] if branch == "feat/x" else [])
    common = dict(
        worktree_heads=same_sha_heads,
        repo_key="/repo/.git",
        repo_peers=peers,
    )

    assert probe.check(_ctx(
        pane_id="w1:p1",
        status="working",
        **common,
    )) is None
    assert probe.check(_ctx(
        pane_id="w1:p2",
        head_sha="abc",
        branch="feat/x",
        **common,
    )) is not None


def test_ambiguous_same_branch_peers_do_not_fall_back_to_other_working_pane():
    same_sha_heads = (
        WorktreeHead(head_sha="abc", branch="main"),
        WorktreeHead(head_sha="abc", branch="feat/x"),
    )
    peers = (
        PanePeer("w1:p1", "working", "abc", "main"),
        PanePeer("w1:p2", "idle", "abc", "feat/x"),
        PanePeer("w1:p3", "idle", "abc", "feat/x"),
    )
    probe = CIProbe(_cache(), run_gh=lambda cwd, branch: [{
        "headSha": "abc",
        "status": "in_progress",
        "workflowName": "CI",
    }] if branch == "feat/x" else [])
    common = dict(
        worktree_heads=same_sha_heads,
        repo_key="/repo/.git",
        repo_peers=peers,
    )

    for peer in peers:
        assert probe.check(_ctx(
            pane_id=peer.pane_id,
            status=peer.status,
            branch=peer.branch,
            **common,
        )) is None


def test_herdwatch_hold_is_not_counted_as_real_working_owner():
    peers = (
        PanePeer(
            "w1:p1",
            "working",
            "abc",
            "main",
            "⏳ review",
            herdwatch_hold=True,
        ),
        PanePeer("w1:p2", "working", "abc", "main"),
    )
    probe = CIProbe(_cache(), run_gh=lambda cwd, branch: [{
        "headSha": "def",
        "status": "in_progress",
        "workflowName": "CI",
    }] if branch == "feat/x" else [])
    common = dict(
        worktree_heads=_TWO_HEADS,
        repo_key="/repo/.git",
        repo_peers=peers,
    )

    assert probe.check(_ctx(
        pane_id="w1:p1",
        status="working",
        **common,
    )) is None
    assert probe.check(_ctx(
        pane_id="w1:p2",
        status="working",
        **common,
    )) is not None


def test_truncated_ci_label_recovers_existing_owner():
    workflow = "very-long-workflow-name-that-will-be-truncated"
    label = f"CI: {workflow}"
    displayed = aggregate([Pending(label, 20, "ci", True)])
    peers = (
        PanePeer("w1:p1", "idle", "abc", "main"),
        PanePeer("w1:p2", "idle", "abc", "main", displayed),
    )
    probe = CIProbe(_cache(), run_gh=lambda cwd, branch: [{
        "headSha": "def",
        "status": "in_progress",
        "workflowName": workflow,
    }] if branch == "feat/x" else [])
    common = dict(
        worktree_heads=_TWO_HEADS,
        repo_key="/repo/.git",
        repo_peers=peers,
    )

    assert probe.check(_ctx(pane_id="w1:p1", **common)) is None
    assert probe.check(_ctx(pane_id="w1:p2", **common)) is not None
