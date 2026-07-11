# tests/test_herdr.py
import pytest

from herdwatch.herdr import HerdrClient
from herdwatch.herdr_socket import HerdrApiError, HerdrUnavailable


class FakeRequest:
    """Records calls; per-method scripted result or exception."""

    def __init__(self, results=None):
        self.results = results or {}
        self.calls = []

    def __call__(self, method, params, *, socket_path=None, timeout_s=10.0):
        self.calls.append((method, params))
        outcome = self.results.get(method, {})
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_session_snapshot_returns_result():
    req = FakeRequest({"session.snapshot": {"agents": [{"pane_id": "w1:p1"}]}})
    c = HerdrClient(request=req)
    assert c.session_snapshot() == {"agents": [{"pane_id": "w1:p1"}]}


def test_session_snapshot_propagates_errors():
    c = HerdrClient(request=FakeRequest({"session.snapshot": HerdrApiError("unknown_method", "x")}))
    with pytest.raises(HerdrApiError):
        c.session_snapshot()
    c = HerdrClient(request=FakeRequest({"session.snapshot": HerdrUnavailable("down")}))
    with pytest.raises(HerdrUnavailable):
        c.session_snapshot()


def test_agent_get_returns_record_and_none_on_failure():
    req = FakeRequest({"agent.get": {"agent": {"pane_id": "w1:p1", "agent_status": "idle"}}})
    assert HerdrClient(request=req).agent_get("w1:p1") == {"pane_id": "w1:p1", "agent_status": "idle"}
    assert req.calls == [("agent.get", {"target": "w1:p1"})]
    assert HerdrClient(request=FakeRequest({"agent.get": HerdrUnavailable("down")})).agent_get("w1:p1") is None
    assert HerdrClient(request=FakeRequest({"agent.get": HerdrApiError("not_found", "x")})).agent_get("w1:p1") is None


def test_report_agent_sends_params_and_maps_result():
    req = FakeRequest({"pane.report_agent": {"type": "ok"}})
    c = HerdrClient(request=req)
    assert c.report_agent("w1:p1", "herdwatch", "claude", "working", "⏳ CI") is True
    assert req.calls == [("pane.report_agent",
                          {"pane_id": "w1:p1", "source": "herdwatch", "agent": "claude",
                           "state": "working", "custom_status": "⏳ CI"})]
    assert HerdrClient(request=FakeRequest({"pane.report_agent": HerdrUnavailable("x")})) \
        .report_agent("w1:p1", "herdwatch", "claude", "working") is False


def test_report_agent_omits_custom_status_when_none():
    req = FakeRequest({"pane.report_agent": {"type": "ok"}})
    HerdrClient(request=req).report_agent("w1:p1", "herdwatch", "claude", "working")
    assert "custom_status" not in req.calls[0][1]


def test_release_agent_returns_tristate():
    # "gone" is NOT success: the pane id may have changed via a move while
    # the assertion lives on -- the daemon reconciles before dropping state
    assert HerdrClient(request=FakeRequest({"pane.release_agent": {"type": "ok"}})) \
        .release_agent("w1:p1", "herdwatch", "claude") == "ok"
    assert HerdrClient(request=FakeRequest({"pane.release_agent": HerdrApiError("not_found", "gone")})) \
        .release_agent("w1:p1", "herdwatch", "claude") == "gone"
    assert HerdrClient(request=FakeRequest({"pane.release_agent": HerdrApiError("invalid_params", "x")})) \
        .release_agent("w1:p1", "herdwatch", "claude") == "failed"
    assert HerdrClient(request=FakeRequest({"pane.release_agent": HerdrUnavailable("down")})) \
        .release_agent("w1:p1", "herdwatch", "claude") == "failed"


def test_report_metadata_set_and_clear():
    req = FakeRequest({"pane.report_metadata": {"type": "ok"}})
    c = HerdrClient(request=req)
    assert c.report_metadata("w1:p1", "herdwatch", agent="claude",
                             custom_status="⏳ CI", ttl_ms=30000) is True
    assert req.calls[-1] == ("pane.report_metadata",
                             {"pane_id": "w1:p1", "source": "herdwatch", "agent": "claude",
                              "custom_status": "⏳ CI", "ttl_ms": 30000})
    assert c.report_metadata("w1:p1", "herdwatch", clear_custom_status=True) is True
    assert req.calls[-1] == ("pane.report_metadata",
                             {"pane_id": "w1:p1", "source": "herdwatch",
                              "clear_custom_status": True})


def test_report_metadata_not_found_true_only_for_clear():
    err = FakeRequest({"pane.report_metadata": HerdrApiError("not_found", "gone")})
    c = HerdrClient(request=err)
    assert c.report_metadata("w1:p1", "herdwatch", clear_custom_status=True) is True
    assert c.report_metadata("w1:p1", "herdwatch", custom_status="⏳ CI") is False


def test_pane_process_info_maps_result_and_failure():
    req = FakeRequest({"pane.process_info": {"process_info": {"shell_pid": 1}}})
    assert HerdrClient(request=req).pane_process_info("w1:p1") == {"shell_pid": 1}
    assert HerdrClient(request=FakeRequest({"pane.process_info": HerdrUnavailable("x")})) \
        .pane_process_info("w1:p1") == {}
