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

def test_agent_list_empty_on_error():
    assert HerdrClient(run=lambda args: (1, "")).agent_list() == []
