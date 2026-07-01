from herdwatch.models import PaneContext
from herdwatch.probes.bgjobs import BgJobsProbe, _parse_etime


def _ctx():
    return PaneContext("w1:p1", "claude", "/x", "idle", "sha", "main", True, True)


_INFO = {"shell_pid": 100, "foreground_process_group_id": 100}


def test_none_when_no_shell_pid():
    probe = BgJobsProbe(process_info=lambda pid: {}, list_descendants=lambda root: [])
    assert probe.check(_ctx()) is None


def test_ignores_foreground_group():
    desc = [{"pid": 101, "pgid": 100, "etime_s": 60, "comm": "npm"}]
    probe = BgJobsProbe(process_info=lambda pid: _INFO, list_descendants=lambda root: desc)
    assert probe.check(_ctx()) is None  # pgid == foreground group


def test_pending_for_backgrounded_job():
    desc = [{"pid": 202, "pgid": 202, "etime_s": 60, "comm": "pytest"}]
    probe = BgJobsProbe(process_info=lambda pid: _INFO, list_descendants=lambda root: desc)
    p = probe.check(_ctx())
    assert p is not None and p.label == "pytest" and p.priority == 10 and p.source == "bgjobs"


def test_ignores_young_jobs():
    desc = [{"pid": 202, "pgid": 202, "etime_s": 2, "comm": "pytest"}]
    probe = BgJobsProbe(process_info=lambda pid: _INFO, list_descendants=lambda root: desc, min_age_s=5)
    assert probe.check(_ctx()) is None


def test_ignores_known_agent_name():
    desc = [{"pid": 303, "pgid": 303, "etime_s": 60, "comm": "claude"}]
    probe = BgJobsProbe(process_info=lambda pid: _INFO, list_descendants=lambda root: desc)
    assert probe.check(_ctx()) is None


def test_ignores_codex_node_repl_helper():
    # /Applications/Codex.app/Contents/Resources/node_repl is a Codex runtime
    # helper that lives as long as the agent does -> not a background job.
    desc = [{"pid": 404, "pgid": 404, "etime_s": 3600, "comm": "node_repl"}]
    probe = BgJobsProbe(process_info=lambda pid: _INFO, list_descendants=lambda root: desc)
    assert probe.check(_ctx()) is None


def test_extra_ignore_adds_to_defaults():
    # user-supplied names are ignored on top of the built-in defaults
    desc = [{"pid": 505, "pgid": 505, "etime_s": 60, "comm": "vite"}]
    probe = BgJobsProbe(process_info=lambda pid: _INFO, list_descendants=lambda root: desc,
                        extra_ignore=["vite"])
    assert probe.check(_ctx()) is None


def test_extra_ignore_does_not_drop_builtin_defaults():
    # adding an extra name must not lose the built-in ignore set
    desc = [{"pid": 606, "pgid": 606, "etime_s": 60, "comm": "node_repl"}]
    probe = BgJobsProbe(process_info=lambda pid: _INFO, list_descendants=lambda root: desc,
                        extra_ignore=["vite"])
    assert probe.check(_ctx()) is None


def test_raising_process_info_degrades_to_none():
    def boom(pid):
        raise RuntimeError("herdr unavailable")
    probe = BgJobsProbe(process_info=boom, list_descendants=lambda root: [])
    assert probe.check(_ctx()) is None


def test_parse_etime_formats():
    assert _parse_etime("00:05") == 5
    assert _parse_etime("01:30") == 90
    assert _parse_etime("02:00:00") == 7200
    assert _parse_etime("1-00:00:00") == 86400
