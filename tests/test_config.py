from herdwatch.config import load


def test_defaults_when_missing(tmp_path):
    cfg = load(str(tmp_path / "nope.toml"))
    assert cfg.poll_interval_s == 4.0
    assert cfg.probes == {"roborev": True, "ci": True, "bgjobs": True, "marker": True}


def test_override(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[daemon]\npoll_interval_s = 2\n[probes]\nbgjobs = false\n')
    cfg = load(str(p))
    assert cfg.poll_interval_s == 2
    assert cfg.probes["bgjobs"] is False
    assert cfg.probes["ci"] is True  # untouched default
