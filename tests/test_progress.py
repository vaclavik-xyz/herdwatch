import json
from pathlib import Path

from herdwatch.progress import Progress, format_label, progress_label, read_progress

SESSION = "c00b128f-68c8-4643-82d6-2835c317517d"


def _write_tasks(root: Path, tasks: list[dict | str]) -> None:
    d = root / f"session-{SESSION[:8]}"
    d.mkdir(parents=True, exist_ok=True)
    for i, t in enumerate(tasks, start=1):
        body = t if isinstance(t, str) else json.dumps(t)
        (d / f"{i}.json").write_text(body)


def _task(status, subject="Do thing", active_form=None):
    t = {"subject": subject, "status": status}
    if active_form is not None:
        t["activeForm"] = active_form
    return t


def test_missing_dir_returns_none(tmp_path):
    assert read_progress(SESSION, root=str(tmp_path)) is None


def test_counts_and_active_task(tmp_path):
    _write_tasks(tmp_path, [
        _task("completed"),
        _task("in_progress", subject="Fix auth", active_form="Fixing auth"),
        _task("pending"),
    ])
    p = read_progress(SESSION, root=str(tmp_path))
    assert p == Progress(done=1, total=3, active="Fixing auth")


def test_active_falls_back_to_subject(tmp_path):
    _write_tasks(tmp_path, [_task("in_progress", subject="Fix auth"), _task("pending")])
    p = read_progress(SESSION, root=str(tmp_path))
    assert p is not None and p.active == "Fix auth"


def test_first_in_progress_in_numeric_order(tmp_path):
    # 10 tasks so lexicographic order (1,10,2,...) would pick the wrong one
    tasks = [_task("completed") for _ in range(9)] + [_task("in_progress", subject="Ten")]
    d = tmp_path / f"session-{SESSION[:8]}"
    d.mkdir(parents=True)
    for i, t in enumerate(tasks, start=1):
        (d / f"{i}.json").write_text(json.dumps(t))
    (d / "2.json").write_text(json.dumps(_task("in_progress", subject="Two")))
    p = read_progress(SESSION, root=str(tmp_path))
    assert p is not None and p.active == "Two"


def test_none_when_no_in_progress(tmp_path):
    _write_tasks(tmp_path, [_task("completed"), _task("pending")])
    assert read_progress(SESSION, root=str(tmp_path)) is None


def test_none_when_all_completed(tmp_path):
    _write_tasks(tmp_path, [_task("completed"), _task("completed")])
    assert read_progress(SESSION, root=str(tmp_path)) is None


def test_none_when_single_task(tmp_path):
    _write_tasks(tmp_path, [_task("in_progress")])
    assert read_progress(SESSION, root=str(tmp_path)) is None


def test_malformed_and_non_dict_files_skipped(tmp_path):
    _write_tasks(tmp_path, [
        "not json{",
        json.dumps(["a", "list"]),
        _task("in_progress", subject="Real"),
        _task("pending"),
    ])
    p = read_progress(SESSION, root=str(tmp_path))
    assert p == Progress(done=0, total=2, active="Real")


def test_non_numeric_filenames_ignored(tmp_path):
    _write_tasks(tmp_path, [_task("in_progress"), _task("pending")])
    d = tmp_path / f"session-{SESSION[:8]}"
    (d / ".lock").write_text("")
    (d / "notes.json").write_text(json.dumps(_task("in_progress")))
    p = read_progress(SESSION, root=str(tmp_path))
    assert p is not None and p.total == 2


def test_format_label_counts_current_task():
    assert format_label(Progress(done=2, total=7, active="Fixing auth")) == "3/7 Fixing auth"


def test_format_label_clamps_done_overflow():
    # all-but-active completed: done+1 must not exceed total
    assert format_label(Progress(done=7, total=7, active="Last")) == "7/7 Last"


def test_format_label_truncates_to_32_with_ellipsis():
    label = format_label(Progress(done=0, total=2, active="A" * 60))
    assert len(label) == 32 and label.endswith("…") and label.startswith("1/2 A")


def test_progress_label_composes(tmp_path):
    _write_tasks(tmp_path, [_task("completed"), _task("in_progress", subject="Go")])
    assert progress_label(SESSION, root=str(tmp_path)) == "2/2 Go"
    assert progress_label("00000000-none", root=str(tmp_path)) is None
