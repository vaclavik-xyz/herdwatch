import sys

from herdwatch import service


def test_render_plist_has_label_exe_path():
    p = service.render_plist(exe="/opt/hw/.venv/bin/herdwatch",
                             path_env="/opt/homebrew/bin:/usr/bin",
                             out="/tmp/o", err="/tmp/e")
    assert "<string>dev.herdwatch.daemon</string>" in p
    assert "<string>/opt/hw/.venv/bin/herdwatch</string>" in p
    assert "<string>daemon</string>" in p
    assert "/opt/homebrew/bin:/usr/bin" in p
    assert "<key>KeepAlive</key><true/>" in p


def test_install_writes_and_loads():
    writes = {}
    calls = []
    msg = service.install(
        plist_path="/x/dev.herdwatch.daemon.plist",
        run=lambda a: calls.append(a) or (0, ""),
        render=lambda: "PLIST",
        write=lambda path, content: writes.__setitem__(path, content),
    )
    assert writes["/x/dev.herdwatch.daemon.plist"] == "PLIST"
    assert ["launchctl", "load", "/x/dev.herdwatch.daemon.plist"] in calls
    assert "/x/dev.herdwatch.daemon.plist" in msg


def test_uninstall_unloads_and_removes():
    removed = []
    calls = []
    service.uninstall(
        plist_path="/x/p.plist",
        run=lambda a: calls.append(a) or (0, ""),
        remove=lambda p: removed.append(p),
    )
    assert ["launchctl", "unload", "/x/p.plist"] in calls
    assert removed == ["/x/p.plist"]


def test_is_macos_matches_platform():
    assert service.is_macos() == (sys.platform == "darwin")


def test_default_path_env_includes_local_bin():
    path = service.default_path_env()
    assert "/opt/homebrew/bin" in path
