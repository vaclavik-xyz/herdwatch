from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PATH = os.path.expanduser("~/.config/herdwatch/config.toml")
_DEFAULT_PROBES = {"roborev": True, "ci": True, "bgjobs": True, "marker": True}


@dataclass
class Config:
    poll_interval_s: float = 4.0
    reprobe_interval_s: float = 15.0
    socket_path: str = ""
    probes: dict = field(default_factory=lambda: dict(_DEFAULT_PROBES))
    ci_cache_ttl_s: float = 10.0
    bgjobs_min_age_s: float = 5.0
    allow: list = field(default_factory=list)
    deny: list = field(default_factory=list)


def load(path: str | None = None) -> Config:
    p = Path(path or DEFAULT_PATH)
    cfg = Config()
    if not p.exists():
        return cfg
    data = tomllib.loads(p.read_text())
    daemon = data.get("daemon", {})
    cfg.poll_interval_s = float(daemon.get("poll_interval_s", cfg.poll_interval_s))
    cfg.reprobe_interval_s = float(daemon.get("reprobe_interval_s", cfg.reprobe_interval_s))
    cfg.socket_path = str(daemon.get("socket_path", cfg.socket_path))
    for name in _DEFAULT_PROBES:
        cfg.probes[name] = bool(data.get("probes", {}).get(name, cfg.probes[name]))
    cfg.ci_cache_ttl_s = float(data.get("probes", {}).get("ci", {}).get("cache_ttl_s", cfg.ci_cache_ttl_s)) \
        if isinstance(data.get("probes", {}).get("ci"), dict) else cfg.ci_cache_ttl_s
    bg = data.get("probes", {}).get("bgjobs")
    if isinstance(bg, dict):
        cfg.bgjobs_min_age_s = float(bg.get("min_age_s", cfg.bgjobs_min_age_s))
    panes = data.get("panes", {})
    cfg.allow = list(panes.get("allow", []))
    cfg.deny = list(panes.get("deny", []))
    return cfg
