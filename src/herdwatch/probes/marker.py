from __future__ import annotations

from ..markers import MarkerStore
from ..models import PaneContext, Pending

PRIORITY = 40


class MarkerProbe:
    name = "marker"

    def __init__(self, store: MarkerStore) -> None:
        self._store = store

    def check(self, ctx: PaneContext) -> Pending | None:
        active = self._store.active_for_pane(ctx.pane_id)
        if not active:
            return None
        return Pending(label=active[0].label, priority=PRIORITY, source=self.name)
