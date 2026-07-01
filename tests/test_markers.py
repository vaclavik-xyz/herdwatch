from herdwatch.markers import MarkerStore

def _store(tmp_path, **kw):
    return MarkerStore(tmp_path, now=kw.get("now", lambda: 1000.0),
                       run_cmd=kw.get("run_cmd", lambda c: 1),
                       pid_alive=kw.get("pid_alive", lambda p: True))

def test_plain_marker_is_pending_until_removed(tmp_path):
    s = _store(tmp_path)
    m = s.add("w1:p1", "deploy")
    assert s.is_pending(m) is True
    s.remove(m.id)
    assert s.all() == []

def test_ttl_expiry(tmp_path):
    now = [1000.0]
    s = _store(tmp_path, now=lambda: now[0])
    m = s.add("w1:p1", "x", ttl_s=10)
    assert s.is_pending(m) is True
    now[0] = 1011.0
    assert s.is_pending(m) is False

def test_until_pending_while_cmd_fails(tmp_path):
    rc = [1]
    s = _store(tmp_path, run_cmd=lambda c: rc[0])
    m = s.add("w1:p1", "x", until="check.sh")
    assert s.is_pending(m) is True   # cmd non-zero -> still waiting
    rc[0] = 0
    assert s.is_pending(m) is False  # cmd success -> done

def test_active_for_pane_prunes(tmp_path):
    now = [1000.0]
    s = _store(tmp_path, now=lambda: now[0])
    s.add("w1:p1", "x", ttl_s=5)
    now[0] = 1010.0
    assert s.active_for_pane("w1:p1") == []
    assert s.all() == []  # pruned from disk

def test_dead_pid_not_pending(tmp_path):
    s = _store(tmp_path, pid_alive=lambda p: False)
    m = s.add("w1:p1", "x", pid=123)
    assert s.is_pending(m) is False

def test_live_pid_still_pending(tmp_path):
    s = _store(tmp_path, pid_alive=lambda p: True)
    m = s.add("w1:p1", "x", pid=123)
    assert s.is_pending(m) is True

def test_active_for_pane_leaves_other_pane(tmp_path):
    s = _store(tmp_path)
    s.add("w1:p1", "keep")
    s.add("w2:p2", "other")
    assert len(s.active_for_pane("w1:p1")) == 1
    assert any(m.pane_id == "w2:p2" for m in s.all())

def test_corrupt_marker_file_is_skipped(tmp_path):
    s = _store(tmp_path)
    s.add("w1:p1", "good")
    (tmp_path / "bad.json").write_text("{not valid json")
    labels = [m.label for m in s.all()]
    assert "good" in labels
