import os

from herdwatch import cli
from herdwatch import state as _state
from herdwatch.markers import MarkerStore


def _seed_snapshot(tmp_path, monkeypatch, panes, getpid=os.getpid):
    p = str(tmp_path / "managed.json")
    monkeypatch.setattr(_state, "STATE_PATH", p)
    _state.StateStore(p, getpid=getpid).write(panes)


def test_status_reports_held_panes(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "MARKER_DIR", str(tmp_path / "markers"))
    _seed_snapshot(tmp_path, monkeypatch,
                   [{"pane_id": "w1:p1", "agent": "claude", "status": "⏳ review"}])
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "w1:p1" in out and "⏳ review" in out
    assert "not running" not in out  # our own pid is alive


def test_status_flags_dead_daemon(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "MARKER_DIR", str(tmp_path / "markers"))
    _seed_snapshot(tmp_path, monkeypatch,
                   [{"pane_id": "w1:p1", "agent": "claude", "status": "⏳ CI: ci"}],
                   getpid=lambda: 2_000_000_000)
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "not running" in out
    assert "w1:p1" in out  # still shows the (possibly stale) held pane


def test_status_no_state(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "MARKER_DIR", str(tmp_path / "markers"))
    monkeypatch.setattr(_state, "STATE_PATH", str(tmp_path / "missing.json"))
    assert cli.main(["status"]) == 0
    assert "no state" in capsys.readouterr().out


def test_status_lists_markers_and_panes(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "MARKER_DIR", str(tmp_path / "markers"))
    MarkerStore(tmp_path / "markers").add("w9:p9", "deploy")
    _seed_snapshot(tmp_path, monkeypatch,
                   [{"pane_id": "w1:p1", "agent": "claude", "status": "⏳ review"}])
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "w1:p1" in out          # managed pane
    assert "deploy" in out and "w9:p9" in out  # marker


def test_add_and_list_marker(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "MARKER_DIR", str(tmp_path))
    monkeypatch.setenv("HERDR_PANE_ID", "w1:p1")
    assert cli.main(["add", "deploy"]) == 0
    assert cli.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "deploy" in out and "w1:p1" in out

def test_add_forwards_marker_lifecycle_options(tmp_path, monkeypatch):
    captured = {}

    class FakeMarker:
        id = "m1"

    class FakeStore:
        def add(self, pane, label, **kwargs):
            captured["pane"] = pane
            captured["label"] = label
            captured.update(kwargs)
            return FakeMarker()

    monkeypatch.setattr(cli, "_store", lambda: FakeStore())
    assert cli.main([
        "add", "deploy",
        "--pane", "w2:p2",
        "--until", "make deploy",
        "--pid", "123",
        "--ttl", "30",
    ]) == 0
    assert captured == {
        "pane": "w2:p2",
        "label": "deploy",
        "until": "make deploy",
        "pid": 123,
        "ttl_s": 30,
    }

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

def test_rm_requires_id_or_all(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "MARKER_DIR", str(tmp_path))
    assert cli.main(["rm"]) == 2


def test_doctor_exit_0_when_required_pass(monkeypatch):
    import herdwatch.doctor as doc
    from herdwatch.doctor import Check
    monkeypatch.setattr(doc, "diagnose", lambda: [Check("herdr on PATH", True, True), Check("x", False, False)])
    assert cli.main(["doctor"]) == 0


def test_doctor_exit_1_when_required_fail(monkeypatch):
    import herdwatch.doctor as doc
    from herdwatch.doctor import Check
    monkeypatch.setattr(doc, "diagnose", lambda: [Check("herdr on PATH", False, True)])
    assert cli.main(["doctor"]) == 1


def test_doctor_json(monkeypatch, capsys):
    import json
    import herdwatch.doctor as doc
    from herdwatch.doctor import Check
    monkeypatch.setattr(doc, "diagnose", lambda: [Check("herdr on PATH", True, True)])
    assert cli.main(["doctor", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["name"] == "herdr on PATH"


def test_install_service_dry_run(monkeypatch, capsys):
    import herdwatch.service as svc
    monkeypatch.setattr(svc, "is_macos", lambda: True)
    monkeypatch.setattr(svc, "render_plist", lambda: "PLIST-PREVIEW")
    assert cli.main(["install-service", "--dry-run"]) == 0
    assert "PLIST-PREVIEW" in capsys.readouterr().out


def test_install_service_non_macos_errors(monkeypatch):
    import herdwatch.service as svc
    monkeypatch.setattr(svc, "is_macos", lambda: False)
    assert cli.main(["install-service"]) == 2


def test_install_service_installs(monkeypatch):
    import herdwatch.service as svc
    calls = []
    monkeypatch.setattr(svc, "is_macos", lambda: True)
    monkeypatch.setattr(svc, "install", lambda: calls.append("install") or (0, "done"))
    assert cli.main(["install-service"]) == 0
    assert calls == ["install"]


def test_install_service_propagates_install_failure(monkeypatch):
    import herdwatch.service as svc
    monkeypatch.setattr(svc, "is_macos", lambda: True)
    monkeypatch.setattr(svc, "install", lambda: (1, "load failed"))
    assert cli.main(["install-service"]) == 1


def test_install_service_uninstall(monkeypatch):
    import herdwatch.service as svc
    calls = []
    monkeypatch.setattr(svc, "is_macos", lambda: True)
    monkeypatch.setattr(svc, "uninstall", lambda: calls.append("uninstall") or (0, "removed"))
    assert cli.main(["install-service", "--uninstall"]) == 0
    assert calls == ["uninstall"]
