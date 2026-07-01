from __future__ import annotations

from .models import Pending

HOURGLASS = "⏳"  # ⏳
MAX_LEN = 32


def aggregate(pendings: list[Pending]) -> str | None:
    if not pendings:
        return None
    ordered = sorted(pendings, key=lambda p: p.priority, reverse=True)
    top = ordered[0]
    extra = len(ordered) - 1
    label = f"{HOURGLASS} {top.label}"
    if extra > 0:
        label = f"{label} +{extra}"
    if len(label) > MAX_LEN:
        label = label[:MAX_LEN]
    return label
