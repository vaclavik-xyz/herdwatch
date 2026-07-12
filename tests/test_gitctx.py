import subprocess
from pathlib import Path
from herdwatch.gitctx import enrich

def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def test_non_repo(tmp_path):
    info = enrich(str(tmp_path))
    assert info.is_git_repo is False
    assert info.head_sha is None
    assert info.worktree_heads == ()
    assert info.repo_key is None

def test_repo_with_github_remote(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    _git(tmp_path, "remote", "add", "origin", "https://github.com/x/y.git")
    (tmp_path / "f").write_text("x")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")
    info = enrich(str(tmp_path))
    assert info.is_git_repo is True
    assert info.has_github_remote is True
    assert info.head_sha and len(info.head_sha) == 40


def _repo_with_commit(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "f").write_text("x")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")


def test_worktree_heads_cover_main_checkout(tmp_path):
    _repo_with_commit(tmp_path)
    info = enrich(str(tmp_path))
    assert [(h.head_sha, h.branch) for h in info.worktree_heads] == \
        [(info.head_sha, "main")]


def test_worktree_heads_include_linked_worktree_on_other_branch(tmp_path):
    _repo_with_commit(tmp_path)
    wt = tmp_path / ".worktrees" / "feat"
    _git(tmp_path, "worktree", "add", "-q", "-b", "feat/x", str(wt))
    (wt / "g").write_text("y")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", "feat work")
    info = enrich(str(tmp_path))
    by_branch = {h.branch: h.head_sha for h in info.worktree_heads}
    assert set(by_branch) == {"main", "feat/x"}
    assert by_branch["feat/x"] != by_branch["main"]
    assert len(by_branch["feat/x"]) == 40
    assert info.repo_key == enrich(str(wt)).repo_key


def test_detached_worktree_has_none_branch(tmp_path):
    _repo_with_commit(tmp_path)
    wt = tmp_path / "detached-wt"
    _git(tmp_path, "worktree", "add", "-q", "--detach", str(wt))
    info = enrich(str(tmp_path))
    branches = [h.branch for h in info.worktree_heads]
    assert None in branches and "main" in branches
