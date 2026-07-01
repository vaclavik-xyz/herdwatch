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


def test_empty_probe_subtable_does_not_disable(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[probes.ci]\n')
    cfg = load(str(p))
    assert cfg.probes["ci"] is True


def test_ci_subtable_sets_cache_ttl_and_keeps_enabled(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[probes.ci]\ncache_ttl_s = 30\n')
    cfg = load(str(p))
    assert cfg.probes["ci"] is True
    assert cfg.ci_cache_ttl_s == 30


def test_bgjobs_subtable_sets_min_age_and_keeps_enabled(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[probes.bgjobs]\nmin_age_s = 20\n')
    cfg = load(str(p))
    assert cfg.bgjobs_min_age_s == 20
    assert cfg.probes["bgjobs"] is True
