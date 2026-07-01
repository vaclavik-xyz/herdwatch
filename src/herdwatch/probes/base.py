from __future__ import annotations

from typing import Protocol

from ..models import PaneContext, Pending


class Probe(Protocol):
    name: str

    def check(self, ctx: PaneContext) -> Pending | None: ...
