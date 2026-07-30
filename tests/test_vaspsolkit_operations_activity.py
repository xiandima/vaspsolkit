from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest


















def test_activity_record_is_frozen_and_strict() -> None:
    from vaspsolkit.operations.activity import ActivityRecord

    record = ActivityRecord("2026-07-24T10:00:00Z", "submit", "neutral", "ok")
    with pytest.raises(FrozenInstanceError):
        record.result = "failed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        ActivityRecord("2026-07-24T10:00:00Z", "submit", "neutral", 1)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ActivityRecord("", "submit", "neutral", "ok")


def test_activity_hash_isolates_cases_and_writes_nothing_inside_them(tmp_path: Path) -> None:
    from vaspsolkit.operations.activity import ActivityRecord, append_activity, read_activities

    state_root = tmp_path / "state"
    first, second = tmp_path / "cases" / "first", tmp_path / "cases" / "second"
    one = ActivityRecord("2026-07-24T10:00:00Z", "open", str(first), "ok")
    two = ActivityRecord("2026-07-24T10:01:00Z", "open", str(second), "ok")
    first_log = append_activity(first, one, state_root)
    second_log = append_activity(second, two, state_root)
    first_hash = hashlib.sha256(str(first.resolve()).encode()).hexdigest()[:16]
    second_hash = hashlib.sha256(str(second.resolve()).encode()).hexdigest()[:16]
    assert first_log == state_root.resolve() / first_hash / "activity.jsonl"
    assert second_log == state_root.resolve() / second_hash / "activity.jsonl"
    assert first_log.parent.parent == state_root.resolve()
    assert read_activities(first, state_root) == (one,)
    assert read_activities(second, state_root) == (two,)
    assert not first.exists() and not second.exists()


def test_read_activities_skips_corruption_and_returns_newest_first(tmp_path: Path) -> None:
    from vaspsolkit.operations.activity import ActivityRecord, append_activity, read_activities

    state_root, case = tmp_path / "state", tmp_path / "case"
    older = ActivityRecord("2026-07-24T10:00:00Z", "submit", "neutral\nrelax", "ok", old_job_id="1")
    newer = ActivityRecord(
        "2026-07-24T10:01:00Z", "resubmit", "neutral", "ok",
        old_job_id="1", new_job_id="2", message="line one\nline two",
    )
    log = append_activity(case, older, state_root)
    with log.open("a", encoding="utf-8") as handle:
        handle.write("not json\n")
    append_activity(case, newer, state_root)
    assert read_activities(case, state_root, limit=1) == (newer,)
    assert read_activities(case, state_root, limit=20) == (newer, older)
    assert len(log.read_text(encoding="utf-8").splitlines()) == 3
    with pytest.raises(ValueError):
        read_activities(case, state_root, limit=0)
    with pytest.raises(TypeError):
        read_activities(case, state_root, limit=True)


def test_read_activities_uses_bounded_binary_tail_reads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from vaspsolkit.operations.activity import (
        MAX_ACTIVITY_READ_BYTES,
        ActivityRecord,
        append_activity,
        read_activities,
    )

    state_root, case = tmp_path / "state", tmp_path / "case"
    newest = ActivityRecord("2026-07-24T11:00:00Z", "open", "case", "ok")
    log = append_activity(case, newest, state_root)
    tail = log.read_bytes()
    log.write_bytes(b"invalid\n" * (MAX_ACTIVITY_READ_BYTES // 4) + tail)
    real_open = Path.open
    bytes_read = 0

    class CountingReader:
        def __init__(self, handle: object) -> None:
            self.handle = handle

        def __enter__(self) -> "CountingReader":
            self.handle.__enter__()  # type: ignore[attr-defined]
            return self

        def __exit__(self, *args: object) -> object:
            return self.handle.__exit__(*args)  # type: ignore[attr-defined]

        def __getattr__(self, name: str) -> object:
            return getattr(self.handle, name)

        def read(self, size: int = -1) -> bytes:
            nonlocal bytes_read
            assert 0 <= size <= MAX_ACTIVITY_READ_BYTES
            data = self.handle.read(size)  # type: ignore[attr-defined]
            bytes_read += len(data)
            return data

    def counted_open(path: Path, mode: str = "r", *args: object, **kwargs: object) -> object:
        if path == log:
            assert mode == "rb"
            return CountingReader(real_open(path, mode, *args, **kwargs))
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counted_open)

    assert read_activities(case, state_root, limit=1) == (newest,)
    assert bytes_read <= MAX_ACTIVITY_READ_BYTES


def test_read_activities_skips_overlong_and_non_utf8_lines_and_joins_chunks(tmp_path: Path) -> None:
    from vaspsolkit.operations.activity import (
        MAX_ACTIVITY_LINE_BYTES,
        ActivityRecord,
        append_activity,
        read_activities,
    )

    state_root, case = tmp_path / "state", tmp_path / "case"
    cross_chunk = ActivityRecord(
        "2026-07-24T10:00:00Z", "note", "case", "ok", message="m" * 50_000
    )
    newest = ActivityRecord("2026-07-24T10:01:00Z", "open", "case", "ok")
    log = append_activity(case, cross_chunk, state_root)
    cross_chunk_line = log.read_bytes()
    log.unlink()
    newest_log = append_activity(case, newest, state_root)
    newest_line = newest_log.read_bytes()
    newest_log.write_bytes(
        cross_chunk_line
        + b"\xff\xfe\n"
        + b"x" * (MAX_ACTIVITY_LINE_BYTES + 1)
        + b"\n"
        + newest_line
    )

    assert read_activities(case, state_root) == (newest, cross_chunk)


def test_read_activities_returns_empty_on_binary_io_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from vaspsolkit.operations.activity import read_activities

    def broken_open(*args: object, **kwargs: object) -> object:
        raise OSError("disk unavailable")

    monkeypatch.setattr(Path, "open", broken_open)

    assert read_activities(tmp_path / "case", tmp_path / "state") == ()


def test_append_activity_accepts_utf8_json_line_at_and_below_byte_limit(tmp_path: Path) -> None:
    from dataclasses import asdict

    from vaspsolkit.operations.activity import (
        MAX_ACTIVITY_LINE_BYTES,
        ActivityRecord,
        append_activity,
        read_activities,
    )

    state_root = tmp_path / "state"

    def record_for_total_bytes(total_bytes: int) -> ActivityRecord:
        empty = ActivityRecord("2026-07-24T12:00:00Z", "note", "case", "ok")
        empty_size = len(
            (json.dumps(asdict(empty), ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        )
        return ActivityRecord(
            empty.timestamp,
            empty.action,
            empty.target,
            empty.result,
            message="m" * (total_bytes - empty_size),
        )

    below = record_for_total_bytes(MAX_ACTIVITY_LINE_BYTES - 1)
    at_limit = record_for_total_bytes(MAX_ACTIVITY_LINE_BYTES)
    assert len(
        (json.dumps(asdict(below), ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    ) == MAX_ACTIVITY_LINE_BYTES - 1
    assert len(
        (json.dumps(asdict(at_limit), ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    ) == MAX_ACTIVITY_LINE_BYTES

    append_activity(tmp_path / "below", below, state_root)
    append_activity(tmp_path / "at-limit", at_limit, state_root)

    assert read_activities(tmp_path / "below", state_root) == (below,)
    assert read_activities(tmp_path / "at-limit", state_root) == (at_limit,)


def test_append_activity_rejects_oversized_serialized_line_without_changing_file(tmp_path: Path) -> None:
    from dataclasses import asdict

    from vaspsolkit.operations.activity import MAX_ACTIVITY_LINE_BYTES, ActivityRecord, append_activity

    state_root, case = tmp_path / "state", tmp_path / "case"
    existing = ActivityRecord("2026-07-24T12:00:00Z", "open", "case", "ok")
    log = append_activity(case, existing, state_root)
    before = log.read_bytes()
    empty = ActivityRecord("2026-07-24T12:01:00Z", "note", "case", "ok")
    empty_size = len(
        (json.dumps(asdict(empty), ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    )
    oversized = ActivityRecord(
        empty.timestamp,
        empty.action,
        empty.target,
        empty.result,
        message="界" * ((MAX_ACTIVITY_LINE_BYTES - empty_size) // 3 + 1),
    )
    assert len(
        (json.dumps(asdict(oversized), ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    ) > MAX_ACTIVITY_LINE_BYTES

    with pytest.raises(ValueError, match="activity record exceeds"):
        append_activity(case, oversized, state_root)

    assert log.read_bytes() == before


def test_default_state_root_honors_explicit_and_xdg_environment(monkeypatch, tmp_path: Path) -> None:
    from vaspsolkit.operations.activity import default_state_root

    explicit = tmp_path / "explicit"
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg))
    monkeypatch.setenv("VASPSOLKIT_STATE_ROOT", str(explicit))
    assert default_state_root() == explicit.resolve()

    monkeypatch.delenv("VASPSOLKIT_STATE_ROOT")
    assert default_state_root() == (xdg / "vaspsolkit" / "cases").resolve()
