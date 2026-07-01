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
