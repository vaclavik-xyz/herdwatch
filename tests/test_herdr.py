import json
from herdwatch.herdr import HerdrClient

def test_agent_list_parses_agents():
    payload = json.dumps({"result": {"agents": [{"pane_id": "w1:p1", "agent_status": "idle"}]}})
    client = HerdrClient(run=lambda args: (0, payload))
    agents = client.agent_list()
    assert agents == [{"pane_id": "w1:p1", "agent_status": "idle"}]

def test_report_agent_builds_command():
    calls = []
    client = HerdrClient(run=lambda args: calls.append(args) or (0, ""))
    client.report_agent("w1:p1", "herdwatch", "claude", "working", "⏳ CI")
    assert calls[0] == ["herdr", "pane", "report-agent", "w1:p1", "--source", "herdwatch",
                        "--agent", "claude", "--state", "working", "--custom-status", "⏳ CI"]

def test_release_agent_builds_command():
    calls = []
    client = HerdrClient(run=lambda args: calls.append(args) or (0, ""))
    client.release_agent("w1:p1", "herdwatch", "claude")
    assert calls[0] == ["herdr", "pane", "release-agent", "w1:p1", "--source", "herdwatch", "--agent", "claude"]

def test_report_agent_returns_success_flag():
    assert HerdrClient(run=lambda a: (0, "")).report_agent("w1:p1", "s", "claude", "working") is True
    assert HerdrClient(run=lambda a: (1, "boom")).report_agent("w1:p1", "s", "claude", "working") is False

def test_release_agent_returns_success_flag():
    assert HerdrClient(run=lambda a: (0, "")).release_agent("w1:p1", "s", "claude") is True
    assert HerdrClient(run=lambda a: (1, "boom")).release_agent("w1:p1", "s", "claude") is False

def test_agent_list_empty_on_error():
    assert HerdrClient(run=lambda args: (1, "")).agent_list() == []

def test_agent_list_handles_result_null():
    import json
    payload = json.dumps({"result": None})
    assert HerdrClient(run=lambda a: (0, payload)).agent_list() == []

def test_pane_process_info_handles_result_null():
    import json
    payload = json.dumps({"result": None})
    assert HerdrClient(run=lambda a: (0, payload)).pane_process_info("w1:p1") == {}

def test_agent_explain_returns_detected_state():
    payload = json.dumps({"agent": "claude", "state": "working", "evaluated_rules": []})
    calls = []
    client = HerdrClient(run=lambda args: calls.append(args) or (0, payload))
    assert client.agent_explain("w1:p1") == "working"
    assert calls[0] == ["herdr", "agent", "explain", "w1:p1", "--json"]


def test_agent_explain_none_on_error():
    assert HerdrClient(run=lambda a: (1, "")).agent_explain("w1:p1") is None


def test_agent_explain_none_on_missing_or_non_string_state():
    assert HerdrClient(run=lambda a: (0, json.dumps({"agent": "claude"}))).agent_explain("w1:p1") is None
    assert HerdrClient(run=lambda a: (0, json.dumps({"state": 5}))).agent_explain("w1:p1") is None
