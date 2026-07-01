from herdwatch.cache import TTLCache


def test_caches_within_ttl():
    now = [1000.0]
    calls = []
    c = TTLCache(ttl_s=10, clock=lambda: now[0])

    def fn():
        calls.append(1)
        return "v"

    assert c.get_or("k", fn) == "v"
    assert c.get_or("k", fn) == "v"
    assert len(calls) == 1  # second call served from cache


def test_recomputes_after_ttl():
    now = [1000.0]
    calls = []
    c = TTLCache(ttl_s=10, clock=lambda: now[0])
    c.get_or("k", lambda: calls.append(1))
    now[0] = 1011.0
    c.get_or("k", lambda: calls.append(1))
    assert len(calls) == 2
