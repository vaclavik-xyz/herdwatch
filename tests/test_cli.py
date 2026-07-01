import json
from herdwatch import cli
from herdwatch.markers import MarkerStore

def test_add_and_list_marker(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "MARKER_DIR", str(tmp_path))
    monkeypatch.setenv("HERDR_PANE_ID", "w1:p1")
    assert cli.main(["add", "deploy"]) == 0
    assert cli.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "deploy" in out and "w1:p1" in out

def test_add_requires_pane(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "MARKER_DIR", str(tmp_path))
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    assert cli.main(["add", "x"]) == 2  # no pane available

def test_rm_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "MARKER_DIR", str(tmp_path))
    store = MarkerStore(tmp_path)
    m = store.add("w1:p1", "deploy")
    assert cli.main(["rm", m.id]) == 0
    assert store.all() == []
