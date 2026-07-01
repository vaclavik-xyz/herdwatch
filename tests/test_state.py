from herdwatch import state


def test_write_then_read_round_trips(tmp_path):
    p = str(tmp_path / "managed.json")
    store = state.StateStore(p, now=lambda: 1234.0, getpid=lambda: 4242)
    rows = [{"pane_id": "w1:p1", "agent": "claude", "status": "⏳ review"}]
    store.write(rows)

    snap = state.StateStore(p).read()
    assert snap is not None
    assert snap.pid == 4242
    assert snap.updated_at == 1234.0
    assert snap.panes == rows


def test_read_missing_file_returns_none(tmp_path):
    assert state.StateStore(str(tmp_path / "nope.json")).read() is None


def test_read_corrupt_file_returns_none(tmp_path):
    p = tmp_path / "managed.json"
    p.write_text("{not json")
    assert state.StateStore(str(p)).read() is None


def test_write_is_atomic_and_leaves_no_tmp(tmp_path):
    p = tmp_path / "managed.json"
    state.StateStore(str(p)).write([])
    assert p.exists()
    assert list(tmp_path.glob("*.tmp")) == []


def test_write_empty_panes(tmp_path):
    p = str(tmp_path / "managed.json")
    state.StateStore(p, now=lambda: 1.0, getpid=lambda: 7).write([])
    snap = state.StateStore(p).read()
    assert snap.pid == 7
    assert snap.panes == []


def test_pid_alive_true_for_self():
    import os
    assert state.pid_alive(os.getpid()) is True


def test_pid_alive_false_for_bogus_pid():
    assert state.pid_alive(0) is False
    assert state.pid_alive(2_000_000_000) is False
