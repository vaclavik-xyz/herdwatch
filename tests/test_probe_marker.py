from herdwatch.models import PaneContext
from herdwatch.markers import MarkerStore
from herdwatch.probes.marker import MarkerProbe


def _ctx(pane="w1:p1"):
    return PaneContext(pane, "claude", "/x", "idle", "sha", "main", True, True)


def test_no_marker_none(tmp_path):
    assert MarkerProbe(MarkerStore(tmp_path, run_cmd=lambda c: 1)).check(_ctx()) is None


def test_marker_pending(tmp_path):
    store = MarkerStore(tmp_path, run_cmd=lambda c: 1)
    store.add("w1:p1", "deploy")
    p = MarkerProbe(store).check(_ctx())
    assert p is not None and p.label == "deploy" and p.source == "marker" and p.priority == 40
