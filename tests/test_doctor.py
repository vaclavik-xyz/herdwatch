import json

from herdwatch.doctor import Check, exit_code, format_report, run_checks, to_json
from herdwatch.herdr_socket import HerdrApiError, HerdrUnavailable


def _base_kwargs():
    return dict(
        which=lambda cmd: True,
        run=lambda args: (0, "running"),
        list_procs=lambda: [],
        plist_path="/nonexistent",
    )


def _by_name(checks):
    return {c.name: c for c in checks}


def _run_all_ok(args):
    if args[:2] == ["herdr", "status"]:
        return (0, "server:\n  status: running\n")
    if args[:2] == ["gh", "auth"]:
        return (0, "Logged in to github.com")
    return (0, "")


def test_all_required_pass(tmp_path):
    plist = tmp_path / "svc.plist"
    plist.write_text("x")
    checks = run_checks(
        which=lambda c: True,
        run=_run_all_ok,
        list_procs=lambda: [".venv/bin/herdwatch daemon"],
        plist_path=str(plist),
        snapshot=lambda: {"type": "session_snapshot", "snapshot": {"agents": []}},
    )
    assert exit_code(checks) == 0
    by_name = {c.name: c for c in checks}
    assert by_name["herdr server running"].ok
    assert by_name["herdwatch daemon running"].ok
    assert by_name["launchd service installed"].ok


def test_missing_herdr_fails_required():
    checks = run_checks(
        which=lambda c: c != "herdr",
        run=lambda a: (1, ""),
        list_procs=lambda: [],
        plist_path="/nonexistent",
        snapshot=lambda: {"type": "session_snapshot", "snapshot": {"agents": []}},
    )
    assert exit_code(checks) == 1
    herdr = next(c for c in checks if c.name == "herdr on PATH")
    assert herdr.ok is False and herdr.required is True


def test_optional_missing_is_warn_not_fail():
    def run(a):
        if a[:2] == ["herdr", "status"]:
            return (0, "status: running")
        return (1, "")
    checks = run_checks(
        which=lambda c: c == "herdr",
        run=run,
        list_procs=lambda: [],
        plist_path="/nonexistent",
        snapshot=lambda: {"type": "session_snapshot", "snapshot": {"agents": []}},
    )
    assert exit_code(checks) == 0  # required (herdr) pass; gh/roborev optional
    gh = next(c for c in checks if c.name.startswith("gh"))
    assert gh.ok is False and gh.required is False


def test_format_report_marks():
    r = format_report([Check("a", True, True), Check("b", False, True), Check("c", False, False)])
    assert "✓ a" in r and "✗ b" in r and "⚠ c" in r


def test_to_json_shape():
    data = json.loads(to_json([Check("herdr on PATH", True, True, "d")]))
    assert data[0] == {"name": "herdr on PATH", "ok": True, "required": True, "detail": "d"}


def test_doctor_socket_ok():
    checks = run_checks(
        **_base_kwargs(),
        snapshot=lambda: {"type": "session_snapshot", "snapshot": {"agents": []}},
    )
    by = _by_name(checks)
    assert by["herdr socket reachable"].ok
    assert by["herdr >= 0.7.2 (session.snapshot)"].ok


def test_doctor_rejects_unusable_snapshot_payload():
    by = _by_name(run_checks(**_base_kwargs(), snapshot=lambda: {"agents": []}))
    assert by["herdr socket reachable"].ok
    assert not by["herdr >= 0.7.2 (session.snapshot)"].ok
    assert "snapshot" in by["herdr >= 0.7.2 (session.snapshot)"].detail


def test_doctor_socket_unreachable():
    def snap():
        raise HerdrUnavailable("no socket")

    by = _by_name(run_checks(**_base_kwargs(), snapshot=snap))
    assert not by["herdr socket reachable"].ok
    assert by["herdr socket reachable"].required
    assert not by["herdr >= 0.7.2 (session.snapshot)"].ok


def test_doctor_old_server():
    def snap():
        raise HerdrApiError("unknown_method", "session.snapshot")

    by = _by_name(run_checks(**_base_kwargs(), snapshot=snap))
    assert by["herdr socket reachable"].ok
    assert not by["herdr >= 0.7.2 (session.snapshot)"].ok
    assert "0.7.2" in by["herdr >= 0.7.2 (session.snapshot)"].detail


def test_doctor_server_api_error_preserves_diagnostic():
    def snap():
        raise HerdrApiError("internal_error", "snapshot unavailable")

    by = _by_name(run_checks(**_base_kwargs(), snapshot=snap))
    detail = by["herdr >= 0.7.2 (session.snapshot)"].detail
    assert by["herdr socket reachable"].ok
    assert not by["herdr >= 0.7.2 (session.snapshot)"].ok
    assert "internal_error" in detail
    assert "snapshot unavailable" in detail
    assert "herdr update" not in detail
