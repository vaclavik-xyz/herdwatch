from herdwatch.doctor import Check, exit_code, format_report, run_checks, to_json
import json


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
