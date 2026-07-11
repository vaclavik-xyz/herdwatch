from __future__ import annotations

import logging
import math
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_PATH = os.path.expanduser("~/.config/herdwatch/config.toml")
# bgjobs is opt-in: on an agent multiplexer every pane is an agent, and agents
# constantly spawn subprocesses, so descendant-scanning yields false positives.
# The reliable signals (CI, roborev, markers) are on by default.
_DEFAULT_PROBES = {"roborev": True, "ci": True, "bgjobs": False, "marker": True}
_MAX_RUNTIME_INTERVAL_S = 43_200.0


def _positive_interval(value, default: float, key: str) -> float:
    try:
        interval = math.nan if isinstance(value, bool) else float(value)
    except (TypeError, ValueError):
        interval = math.nan
    if (
        math.isfinite(interval)
        and interval > 0
        and interval <= _MAX_RUNTIME_INTERVAL_S
    ):
        return interval
    log.warning(
        "config: %s must be a finite positive number no greater than %.1f; "
        "using default %.1f",
        key,
        _MAX_RUNTIME_INTERVAL_S,
        default,
    )
    return default


@dataclass
class Config:
    resync_interval_s: float = 60.0
    progress_interval_s: float = 4.0
    reprobe_interval_s: float = 15.0
    socket_path: str = ""
    probes: dict[str, bool] = field(default_factory=lambda: dict(_DEFAULT_PROBES))
    ci_cache_ttl_s: float = 10.0
    bgjobs_min_age_s: float = 5.0
    bgjobs_ignore: list[str] = field(default_factory=list)
    progress_enabled: bool = True
    allow: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)


def load(path: str | None = None) -> Config:
    p = Path(path or DEFAULT_PATH)
    cfg = Config()
    if not p.exists():
        return cfg
    data = tomllib.loads(p.read_text())
    daemon = data.get("daemon", {})
    if "poll_interval_s" in daemon:
        log.warning(
            "config: daemon.poll_interval_s is deprecated and ignored "
            "(the daemon is event-driven; see resync_interval_s)"
        )
    cfg.resync_interval_s = _positive_interval(
        daemon.get("resync_interval_s", cfg.resync_interval_s),
        cfg.resync_interval_s,
        "daemon.resync_interval_s",
    )
    cfg.reprobe_interval_s = _positive_interval(
        daemon.get("reprobe_interval_s", cfg.reprobe_interval_s),
        cfg.reprobe_interval_s,
        "daemon.reprobe_interval_s",
    )
    cfg.socket_path = str(daemon.get("socket_path", cfg.socket_path))
    probes_data = data.get("probes", {})
    for name in _DEFAULT_PROBES:
        v = probes_data.get(name)
        if isinstance(v, bool):
            cfg.probes[name] = v
        elif isinstance(v, dict) and isinstance(v.get("enabled"), bool):
            # a probe can be enabled/disabled from inside its own [probes.X]
            # table (TOML forbids `X = true` AND `[probes.X]` together)
            cfg.probes[name] = v["enabled"]
    ci_cfg = probes_data.get("ci")
    if isinstance(ci_cfg, dict):
        cfg.ci_cache_ttl_s = float(ci_cfg.get("cache_ttl_s", cfg.ci_cache_ttl_s))
    bg = probes_data.get("bgjobs")
    if isinstance(bg, dict):
        cfg.bgjobs_min_age_s = float(bg.get("min_age_s", cfg.bgjobs_min_age_s))
        cfg.bgjobs_ignore = list(bg.get("ignore", cfg.bgjobs_ignore))
    prog = data.get("progress", {})
    if isinstance(prog, dict) and isinstance(prog.get("enabled"), bool):
        cfg.progress_enabled = prog["enabled"]
    if isinstance(prog, dict) and "interval_s" in prog:
        cfg.progress_interval_s = _positive_interval(
            prog["interval_s"],
            cfg.progress_interval_s,
            "progress.interval_s",
        )
    panes = data.get("panes", {})
    cfg.allow = list(panes.get("allow", []))
    cfg.deny = list(panes.get("deny", []))
    return cfg
