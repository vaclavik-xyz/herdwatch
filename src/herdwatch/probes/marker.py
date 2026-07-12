from __future__ import annotations

from ..markers import MarkerStore
from ..models import PaneContext, Pending

PRIORITY = 40


class MarkerProbe:
    name = "marker"

    def __init__(self, store: MarkerStore) -> None:
        self._store = store

    def check_pane(self, pane_id: str) -> Pending | None:
        """Cheap marker-only lookup that needs no git-enriched context."""
        active = self._store.active_for_pane(pane_id)
        if not active:
            return None
        return Pending(label=active[0].label, priority=PRIORITY, source=self.name)

    def candidate_panes(self) -> set[str]:
        """Return panes with marker files without evaluating their commands."""
        return {marker.pane_id for marker in self._store.all()}

    def check(self, ctx: PaneContext) -> Pending | None:
        return self.check_pane(ctx.pane_id)
