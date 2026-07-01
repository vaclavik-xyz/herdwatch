from herdwatch.models import Pending, PaneContext


def test_pending_fields():
    p = Pending(label="CI: ci", priority=20, source="ci")
    assert (p.label, p.priority, p.source) == ("CI: ci", 20, "ci")


def test_pane_context_fields():
    c = PaneContext(pane_id="w1:p1", agent="claude", cwd="/x", status="idle",
                    head_sha="abc", branch="main", is_git_repo=True, has_github_remote=True)
    assert c.pane_id == "w1:p1" and c.is_git_repo is True
