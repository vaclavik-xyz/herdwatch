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


def test_bgjobs_ignore_defaults_empty(tmp_path):
    cfg = load(str(tmp_path / "nope.toml"))
    assert cfg.bgjobs_ignore == []


def test_bgjobs_ignore_loaded_from_subtable(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[probes.bgjobs]\nignore = ["vite", "webpack"]\n')
    cfg = load(str(p))
    assert cfg.bgjobs_ignore == ["vite", "webpack"]
    assert cfg.probes["bgjobs"] is True


def test_panes_allow_deny_and_reprobe_interval_are_loaded(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text(
        '[daemon]\nreprobe_interval_s = 7\n'
        '[panes]\nallow = ["w1:p1"]\ndeny = ["w2:p2"]\n'
    )
    cfg = load(str(p))
    assert cfg.reprobe_interval_s == 7
    assert cfg.allow == ["w1:p1"]
    assert cfg.deny == ["w2:p2"]
