from __future__ import annotations

import argparse
import logging
import os
import sys

from . import doctor as _doctor
from . import service as _service
from . import state as _state
from .config import load as load_config
from .daemon import MARKER_DIR, build_daemon
from .markers import MarkerStore

_VERBS = {
    "hold": "holding",
    "hold-pending": "verifying",
    "idle-meta": "labeling",
    "active-meta": "labeling",
    "progress": "working",
    "done": "labeling",
}


def _store() -> MarkerStore:
    return MarkerStore(MARKER_DIR)


def _state_store() -> _state.StateStore:
    return _state.StateStore(_state.STATE_PATH)


def _cmd_daemon(args) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    cfg = load_config(args.config)
    daemon = build_daemon(cfg)
    # recover holds from a prior run (crash/restart) -- but only if that daemon
    # is dead, so a second instance never steals a live daemon's panes
    snap = _state_store().read()
    if snap and snap.panes and not _state.pid_alive(snap.pid):
        daemon.adopt(snap.panes)
    daemon.run()
    return 0


def _cmd_add(args) -> int:
    pane = args.pane or os.environ.get("HERDR_PANE_ID")
    if not pane:
        print("no pane: pass --pane or run inside a herdr pane", file=sys.stderr)
        return 2
    m = _store().add(pane, args.label, until=args.until, pid=args.pid, ttl_s=args.ttl)
    print(m.id)
    return 0


def _cmd_list(args) -> int:
    for m in _store().all():
        print(f"{m.id}  {m.pane_id}  {m.label}")
    return 0


def _cmd_rm(args) -> int:
    if not args.all and not args.marker_id:
        print("rm: provide a marker id or --all", file=sys.stderr)
        return 2
    store = _store()
    if args.all:
        for m in store.all():
            store.remove(m.id)
    else:
        store.remove(args.marker_id)
    return 0


def _cmd_status(args) -> int:
    snap = _state_store().read()
    if snap is None:
        print("daemon: no state yet (daemon not started, or state dir unwritable)")
    else:
        if not _state.pid_alive(snap.pid):
            print(f"daemon: not running (snapshot pid {snap.pid} is gone); held "
                  "panes below may be stale — check `herdwatch doctor`")
        if snap.panes:
            for p in snap.panes:
                verb = _VERBS.get(p.get("kind", "hold"), "holding")
                print(f"{verb} {p['pane_id']}  {p['status']}  ({p['agent']})")
        else:
            print("holding no panes")
    for m in _store().all():
        print(f"marker {m.id} {m.pane_id} {m.label}")
    return 0


def _cmd_doctor(args) -> int:
    checks = _doctor.diagnose()
    print(_doctor.to_json(checks) if args.json else _doctor.format_report(checks))
    return _doctor.exit_code(checks)


def _cmd_install_service(args) -> int:
    if not _service.is_macos():
        print("install-service supports macOS (launchd) only; on Linux run "
              "`herdwatch daemon` under a supervisor such as a systemd user unit.",
              file=sys.stderr)
        return 2
    if args.uninstall:
        rc, msg = _service.uninstall()
        print(msg)
        return rc
    if args.dry_run:
        print(_service.render_plist())
        return 0
    rc, msg = _service.install()
    print(msg)
    return rc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="herdwatch")
    parser.add_argument("--config", default=None)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("daemon").set_defaults(func=_cmd_daemon)
    sub.add_parser("status").set_defaults(func=_cmd_status)
    sub.add_parser("list").set_defaults(func=_cmd_list)

    p_doctor = sub.add_parser("doctor")
    p_doctor.add_argument("--json", action="store_true")
    p_doctor.set_defaults(func=_cmd_doctor)

    p_svc = sub.add_parser("install-service")
    p_svc.add_argument("--dry-run", action="store_true")
    p_svc.add_argument("--uninstall", action="store_true")
    p_svc.set_defaults(func=_cmd_install_service)

    p_add = sub.add_parser("add")
    p_add.add_argument("label")
    p_add.add_argument("--pane", default=None)
    p_add.add_argument("--until", default=None)
    p_add.add_argument("--pid", type=int, default=None)
    p_add.add_argument("--ttl", type=float, default=None)
    p_add.set_defaults(func=_cmd_add)

    p_rm = sub.add_parser("rm")
    p_rm.add_argument("marker_id", nargs="?", default=None)
    p_rm.add_argument("--all", action="store_true")
    p_rm.set_defaults(func=_cmd_rm)

    args = parser.parse_args(argv)
    return args.func(args)
