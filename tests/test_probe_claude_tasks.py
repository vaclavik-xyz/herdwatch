import json
from datetime import datetime, timezone

from herdwatch.models import PaneContext
from herdwatch.probes.claude_tasks import (
    ClaudeTasksProbe,
    TranscriptTasks,
    claude_pid,
    default_find_transcript,
)

SESSION = "4ccfacdb-f177-4047-be05-ba60e5b473c8"
T0 = datetime(2026, 9, 22, 20, 0, tzinfo=timezone.utc).timestamp()


def _ts(offset_s: float) -> str:
    return (
        datetime.fromtimestamp(T0 + offset_s, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _tool_use(use_id, name, **tool_input):
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": use_id, "name": name, "input": tool_input},
        ]},
    }


def _tool_result(use_id, result, text="", at=0.0):
    return {
        "type": "user",
        "timestamp": _ts(at),
        "message": {"role": "user", "content": [
            {"tool_use_id": use_id, "type": "tool_result", "content": text},
        ]},
        "toolUseResult": result,
    }


def _notification(task_id, status="completed", *, queued=False, at=0.0):
    body = (
        f"<task-notification>\n<task-id>{task_id}</task-id>\n"
        f"<status>{status}</status>\n<summary>done</summary>\n"
        "</task-notification>"
    )
    if queued:
        return {"type": "queue-operation", "operation": "enqueue",
                "timestamp": _ts(at), "content": body}
    return {"type": "user", "timestamp": _ts(at),
            "message": {"role": "user", "content": body}}


def _bash_launch(use_id, task_id, description, at=0.0, command="sleep 600"):
    return [
        _tool_use(use_id, "Bash", command=command, description=description,
                  run_in_background=True),
        _tool_result(
            use_id,
            {"stdout": "", "stderr": "", "interrupted": False,
             "backgroundTaskId": task_id},
            f"Command running in background with ID: {task_id}.",
            at=at,
        ),
    ]


def _write(path, records, mode="w"):
    with open(path, mode) as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def _ctx(agent="claude", session=SESSION, pane="w1C:pE"):
    return PaneContext(pane, agent, "/x", "idle", None, None, False, False,
                       agent_session=session)


def _probe(path, *, now=T0 + 60, started_at=None, **kw):
    info = {"foreground_processes": [
        {"argv0": "claude", "name": "2.1.280", "pid": 4242},
    ]}
    return ClaudeTasksProbe(
        process_info=lambda pane_id: info,
        find_transcript=lambda session: str(path),
        process_started_at=lambda pid: started_at,
        clock=lambda: now,
        **kw,
    )


def test_background_bash_is_pending_until_notified(tmp_path):
    path = tmp_path / f"{SESSION}.jsonl"
    _write(path, _bash_launch("toolu_1", "bwjqdatp5", "Run evals"))
    probe = _probe(path)
    pending = probe.check(_ctx())
    assert pending is not None
    assert pending.label == "bg: Run evals"
    assert pending.source == "claude_tasks"
    assert pending.show_while_working is False

    _write(path, [_notification("bwjqdatp5", queued=True)], mode="a")
    assert probe.check(_ctx()) is None


def test_notification_in_user_message_also_finishes(tmp_path):
    path = tmp_path / "t.jsonl"
    _write(path, _bash_launch("toolu_1", "b1", "x") + [_notification("b1", "failed")])
    assert _probe(path).check(_ctx()) is None


def test_task_stop_finishes_task(tmp_path):
    path = tmp_path / "t.jsonl"
    _write(path, _bash_launch("toolu_1", "b1", "dev server") + [
        _tool_use("toolu_2", "TaskStop", task_id="b1"),
        _tool_result("toolu_2", {"message": "Successfully stopped task: b1",
                                 "task_id": "b1", "task_type": "local_bash"}),
    ])
    assert _probe(path).check(_ctx()) is None


def test_async_agent_and_workflow_are_pending(tmp_path):
    path = tmp_path / "t.jsonl"
    _write(path, [
        _tool_use("toolu_a", "Agent", description="Inventory features",
                  run_in_background=True),
        _tool_result("toolu_a", {"isAsync": True, "status": "async_launched",
                                 "agentId": "a995cd2fa28c5e8d9",
                                 "description": "Inventory features"}),
        _tool_use("toolu_w", "Workflow", script="..."),
        _tool_result("toolu_w", {"status": "async_launched", "taskId": "w71ds7pbs",
                                 "taskType": "local_workflow",
                                 "workflowName": "fix-review-findings"}),
    ])
    probe = _probe(path)
    assert probe.check(_ctx()).label == "bg: 2 tasks"

    _write(path, [_notification("a995cd2fa28c5e8d9")], mode="a")
    assert probe.check(_ctx()).label == "bg: fix-review-findings"


def test_synchronous_agent_is_not_a_background_task(tmp_path):
    path = tmp_path / "t.jsonl"
    _write(path, [
        _tool_use("toolu_a", "Agent", description="Research"),
        _tool_result("toolu_a", {"status": "completed", "agentId": "a1",
                                 "content": []}),
    ])
    assert _probe(path).check(_ctx()) is None


def test_monitor_expires_at_its_timeout(tmp_path):
    path = tmp_path / "t.jsonl"
    _write(path, [
        _tool_use("toolu_m", "Monitor", command="tail -f x", description="Watch CI"),
        _tool_result("toolu_m", {"taskId": "bcjnuwl0t", "timeoutMs": 600000,
                                 "persistent": False}),
        # a streamed event must not end the watch
        _notification("bcjnuwl0t", "running"),
    ])
    assert _probe(path, now=T0 + 60).check(_ctx()).label == "bg: Watch CI"
    assert _probe(path, now=T0 + 601).check(_ctx()) is None


def test_persistent_monitor_has_no_deadline(tmp_path):
    path = tmp_path / "t.jsonl"
    _write(path, [
        _tool_use("toolu_m", "Monitor", description="Watch"),
        _tool_result("toolu_m", {"taskId": "m1", "timeoutMs": 600000,
                                 "persistent": True}),
    ])
    assert _probe(path, now=T0 + 3600).check(_ctx()) is not None


def test_launch_before_claude_process_start_is_stale(tmp_path):
    # a resumed session: the old process (and its background tasks) is gone
    path = tmp_path / "t.jsonl"
    _write(path, _bash_launch("toolu_1", "b1", "old", at=0))
    assert _probe(path, started_at=T0 + 300, now=T0 + 400).check(_ctx()) is None
    assert _probe(path, started_at=T0 - 30, now=T0 + 400).check(_ctx()) is not None


def test_max_age_drops_forgotten_tasks(tmp_path):
    path = tmp_path / "t.jsonl"
    _write(path, _bash_launch("toolu_1", "b1", "x"))
    assert _probe(path, now=T0 + 700, max_age_s=600).check(_ctx()) is None


def test_non_claude_or_sessionless_panes_are_skipped(tmp_path):
    path = tmp_path / "t.jsonl"
    _write(path, _bash_launch("toolu_1", "b1", "x"))
    probe = _probe(path)
    assert probe.check(_ctx(agent="codex")) is None
    assert probe.check(_ctx(session=None)) is None


def test_missing_transcript_is_not_pending(tmp_path):
    probe = ClaudeTasksProbe(process_info=lambda p: {},
                             find_transcript=lambda s: None)
    assert probe.check(_ctx()) is None


def test_partial_trailing_line_is_read_on_next_poll(tmp_path):
    path = tmp_path / "t.jsonl"
    launch = _bash_launch("toolu_1", "b1", "x")
    tracker = TranscriptTasks()
    _write(path, launch[:1])
    with open(path, "a") as handle:
        handle.write(json.dumps(launch[1])[:40])
    assert tracker.pending(str(path)) == []
    with open(path, "w") as handle:  # rewrite: same size prefix + rest
        handle.write(json.dumps(launch[0]) + "\n" + json.dumps(launch[1]) + "\n")
    assert [t.task_id for t in tracker.pending(str(path))] == ["b1"]


def test_incremental_reads_only_new_lines(tmp_path):
    path = tmp_path / "t.jsonl"
    tracker = TranscriptTasks()
    _write(path, _bash_launch("toolu_1", "b1", "x"))
    assert len(tracker.pending(str(path))) == 1
    _write(path, _bash_launch("toolu_2", "b2", "y", at=5), mode="a")
    assert [t.task_id for t in tracker.pending(str(path))] == ["b1", "b2"]


def test_truncated_transcript_is_reparsed(tmp_path):
    path = tmp_path / "t.jsonl"
    tracker = TranscriptTasks()
    _write(path, _bash_launch("toolu_1", "b1", "a" * 200))
    assert len(tracker.pending(str(path))) == 1
    _write(path, [{"type": "user"}])
    assert tracker.pending(str(path)) == []


def test_garbage_lines_are_ignored(tmp_path):
    path = tmp_path / "t.jsonl"
    with open(path, "w") as handle:
        handle.write('{"toolUseResult": not json\n')
        handle.write('"tool_use"\n')
    _write(path, _bash_launch("toolu_1", "b1", "x"), mode="a")
    assert _probe(path).check(_ctx()).label == "bg: x"


def test_claude_pid_picks_claude_argv0():
    info = {"foreground_processes": [
        {"argv0": "caffeinate", "pid": 1},
        {"argv0": "/usr/local/bin/claude", "name": "2.1.280", "pid": 60021},
    ]}
    assert claude_pid(info) == 60021
    assert claude_pid({}) is None


def test_default_find_transcript(tmp_path):
    project = tmp_path / "-Users-admin-projects-x"
    project.mkdir()
    target = project / f"{SESSION}.jsonl"
    target.write_text("")
    assert default_find_transcript(SESSION, root=str(tmp_path)) == str(target)
    assert default_find_transcript("missing", root=str(tmp_path)) is None
    assert default_find_transcript("../../etc/passwd", root=str(tmp_path)) is None


def test_long_running_services_are_not_waits(tmp_path):
    path = tmp_path / "t.jsonl"
    _write(path, [
        *_bash_launch("t1", "b1", "Start preview", command="npx next dev -p 3210"),
        *_bash_launch("t2", "b2", "Start dev server for PR 2 review",
                      command="cd wt && ./run.sh"),
        *_bash_launch("t3", "b3", "Serve", command="npm run dev -- --host"),
        *_bash_launch("t4", "b4", "Docs", command="python3 -m http.server 8000"),
        *_bash_launch("t5", "b5", "Wait for reviews",
                      command="until roborev list | grep -q done; do sleep 15; done"),
    ])
    assert _probe(path).check(_ctx()).label == "bg: Wait for reviews"


def test_extra_service_patterns_extend_defaults(tmp_path):
    path = tmp_path / "t.jsonl"
    _write(path, _bash_launch("t1", "b1", "Cluster", command="tilt up"))
    assert _probe(path).check(_ctx()) is not None
    probe = _probe(path, extra_service_patterns=[r"\btilt up\b", "("])
    assert probe.check(_ctx()) is None


def test_service_patterns_only_apply_to_bash(tmp_path):
    path = tmp_path / "t.jsonl"
    _write(path, [
        _tool_use("toolu_a", "Agent", description="Review dev server logs",
                  run_in_background=True),
        _tool_result("toolu_a", {"status": "async_launched", "agentId": "a1"}),
    ])
    assert _probe(path).check(_ctx()).label == "bg: Review dev server logs"
