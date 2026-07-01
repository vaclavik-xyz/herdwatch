from herdwatch.aggregate import aggregate
from herdwatch.models import Pending

def test_empty_is_none():
    assert aggregate([]) is None

def test_highest_priority_wins():
    out = aggregate([Pending("CI: ci", 20, "ci"), Pending("review", 30, "roborev")])
    assert out == "⏳ review +1"

def test_multiple_shows_plus_count():
    out = aggregate([Pending("review", 30, "roborev"), Pending("CI: ci", 20, "ci")])
    assert out == "⏳ review +1"

def test_truncated_to_32_chars():
    out = aggregate([Pending("x" * 50, 10, "marker")])
    assert len(out) <= 32
