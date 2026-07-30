from __future__ import annotations

from pathlib import Path


def test_menu_codes_are_fixed_unique_and_normalized() -> None:
    from vaspsolkit.menu_actions import MENU_ACTIONS, action_by_code, normalize_code

    expected = (
        "01",
        "02",
        "03",
        "10",
        "11",
        "12",
        "13",
        "20",
        "21",
        "22",
        "30",
        "31",
        "32",
        "40",
        "41",
        "42",
        "50",
        "51",
        "60",
        "61",
        "62",
        "90",
        "00",
    )
    assert tuple(action.code for action in MENU_ACTIONS) == expected
    assert len({action.code for action in MENU_ACTIONS}) == len(expected)
    assert normalize_code("2") == "02"
    assert normalize_code("02") == "02"
    assert normalize_code("0") == "00"
    assert action_by_code("2").code == "02"


def test_invalid_menu_code_is_rejected() -> None:
    import pytest

    from vaspsolkit.menu_actions import action_by_code, normalize_code

    with pytest.raises(ValueError, match="任务编号"):
        normalize_code("abc")
    with pytest.raises(KeyError, match="未知任务编号"):
        action_by_code("99")


def test_menu_reprompts_after_invalid_input_and_exits_without_side_effects(
    tmp_path,
) -> None:
    from vaspsolkit.interactive_menu import run_menu

    answers = iter(["99", "abc", "00"])
    output = []
    code = run_menu(
        tmp_path,
        input_fn=lambda prompt: next(answers),
        output=output.append,
        synchronize_fn=lambda *args, **kwargs: None,
    )
    assert code == 0
    assert sum("未知任务编号" in line or "必须是" in line for line in output) == 2
    assert sum("VASPsolKit" in line for line in output) == 3


def test_menu_prints_the_task_prompt_exactly_once(tmp_path) -> None:
    from vaspsolkit.interactive_menu import run_menu

    output = []
    prompts = []

    def answer(prompt: str) -> str:
        prompts.append(prompt)
        return "00"

    assert run_menu(
        tmp_path,
        input_fn=answer,
        output=output.append,
        synchronize_fn=lambda *args, **kwargs: None,
    ) == 0
    visible = output + prompts
    assert sum("输入任务编号" in line for line in visible) == 1


def test_menu_styles_only_the_prompt_arrow_when_color_is_enabled(tmp_path) -> None:
    from vaspsolkit.interactive_menu import run_menu
    from vaspsolkit.terminal_display import TerminalTheme

    prompts = []

    def answer(prompt: str) -> str:
        prompts.append(prompt)
        return "00"

    assert run_menu(
        tmp_path,
        input_fn=answer,
        output=lambda value: None,
        synchronize_fn=lambda *args, **kwargs: None,
        theme=TerminalTheme(True),
    ) == 0
    assert prompts == ["输入任务编号 \x1b[36m>>\x1b[0m "]


def test_no_argument_cli_opens_menu_and_wizard_is_alias(tmp_path, monkeypatch) -> None:
    from vaspsolkit.cli import main

    calls = []
    monkeypatch.setattr("vaspsolkit.cli._has_interactive_tty", lambda: True, raising=False)
    monkeypatch.setattr(
        "vaspsolkit.interactive_menu.run_menu",
        lambda workdir, **kwargs: calls.append(workdir.resolve()) or 0,
        raising=False,
    )
    monkeypatch.chdir(tmp_path)
    assert main([]) == 0
    assert main(["menu", "--workdir", str(tmp_path)]) == 0
    assert main(["wizard", "--workdir", str(tmp_path)]) == 0
    assert calls == [tmp_path.resolve(), tmp_path.resolve(), tmp_path.resolve()]


def test_archived_ui_commands_do_not_import_textual(tmp_path, monkeypatch) -> None:
    import builtins

    from vaspsolkit.cli import main

    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name == "textual" or name.startswith("textual."):
            raise AssertionError("archived UI command imported Textual")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    for command in ("ui", "workbench-ui", "legacy-ui"):
        output = []
        assert main([command, "--workdir", str(tmp_path)], output=output.append) == 2
        assert any("已归档" in line for line in output)


class RecordingScheduler:
    def __init__(self, states=None, error=None):
        self.states = states or {}
        self.error = error
        self.queried = []

    def status(self, job_id):
        from vaspsolkit.scheduler import JobState

        self.queried.append(job_id)
        if self.error is not None:
            raise self.error
        return JobState(job_id, True, self.states.get(job_id, "R"))


def _write_case_state(root: Path) -> None:
    from vaspsolkit.config import KitConfig, write_kit_config
    from vaspsolkit.state import JobRecord, WorkflowState

    write_kit_config(root / "vaspsolkit.json", KitConfig())
    WorkflowState(
        stage="monitor",
        neutral=JobRecord(".", "RUNNING", "neutral-1"),
        jobs={
            "1": JobRecord("charge_sweep/1", "QUEUED", "charge-1"),
            "2": JobRecord("charge_sweep/2", "CONVERGED", "charge-2"),
            "3": JobRecord("charge_sweep/3", "PREPARED", ""),
        },
    ).save(root / "vaspsolkit.state.json")


def test_startup_sync_queries_only_recorded_active_job_ids(tmp_path) -> None:
    from vaspsolkit.interactive_menu import synchronize_case
    from vaspsolkit.state import WorkflowState

    _write_case_state(tmp_path)
    scheduler = RecordingScheduler({"neutral-1": "R", "charge-1": "Q"})
    result = synchronize_case(
        tmp_path,
        scheduler_factory=lambda config: scheduler,
    )

    assert result.ok
    assert result.attempted == 2
    assert scheduler.queried == ["neutral-1", "charge-1"]
    state = WorkflowState.load(tmp_path / "vaspsolkit.state.json")
    assert state.neutral.status == "RUNNING"
    assert state.jobs["1"].status == "QUEUED"
    assert state.jobs["2"].status == "CONVERGED"


def test_startup_sync_skips_scheduler_and_state_lock_without_active_ids(
    tmp_path, monkeypatch
) -> None:
    from vaspsolkit.config import KitConfig, write_kit_config
    from vaspsolkit.interactive_menu import synchronize_case
    from vaspsolkit.state import JobRecord, WorkflowState

    factories = []

    def factory(config):
        factories.append(config)
        return RecordingScheduler()

    assert synchronize_case(tmp_path, scheduler_factory=factory).attempted == 0
    write_kit_config(tmp_path / "vaspsolkit.json", KitConfig())
    assert synchronize_case(tmp_path, scheduler_factory=factory).attempted == 0
    WorkflowState(
        stage="converged",
        neutral=JobRecord(".", "CONVERGED", "old-neutral"),
    ).save(tmp_path / "vaspsolkit.state.json")
    capture_calls = []
    monkeypatch.setattr(
        "vaspsolkit.interactive_menu.capture_recorded_jobs",
        lambda *args, **kwargs: capture_calls.append(args),
    )
    assert synchronize_case(tmp_path, scheduler_factory=factory).attempted == 0
    assert factories == []
    assert capture_calls == []


def test_scheduler_failure_is_a_warning_and_menu_still_exits(tmp_path) -> None:
    from vaspsolkit.interactive_menu import run_menu, synchronize_case

    _write_case_state(tmp_path)
    scheduler = RecordingScheduler(error=RuntimeError("qstat unavailable"))
    output = []

    code = run_menu(
        tmp_path,
        input_fn=lambda prompt: "00",
        output=output.append,
        synchronize_fn=lambda root, config_path=None: synchronize_case(
            root,
            config_path=config_path,
            scheduler_factory=lambda config: scheduler,
        ),
    )

    assert code == 0
    assert any("队列同步失败" in line and "qstat unavailable" in line for line in output)
    assert any("VASPsolKit" in line for line in output)


def test_code_02_uses_guide_recommendation(tmp_path) -> None:
    from vaspsolkit.guide_model import build_snapshot, recommend_action
    from vaspsolkit.interactive_menu import resolve_action

    snapshot = build_snapshot(tmp_path)
    recommended = recommend_action(snapshot)
    resolved = resolve_action("02", snapshot)
    assert resolved.command == recommended.cli_command
    assert resolved.effect == recommended.effect


def test_render_menu_adapts_snapshot_without_changing_action_catalogue(tmp_path) -> None:
    from vaspsolkit.guide_model import build_snapshot, recommend_action
    from vaspsolkit.interactive_menu import _render_menu
    from vaspsolkit.menu_actions import MENU_ACTIONS
    from vaspsolkit.terminal_display import TerminalTheme

    snapshot = build_snapshot(tmp_path)
    recommendation = recommend_action(snapshot)
    output = []

    _render_menu(
        snapshot,
        recommendation,
        output.append,
        theme=TerminalTheme(False),
    )

    text = "\n".join(output)
    assert "Case" in text
    assert "Stage" in text
    assert "Progress" in text
    assert recommendation.reason_zh in text
    assert all(f"{action.code})" in text for action in MENU_ACTIONS)
    assert "不可用" in text
    assert "\x1b[" not in text


def test_submit_and_cancel_require_exact_tokens() -> None:
    from vaspsolkit.interactive_menu import confirm_effect

    assert confirm_effect("external-submit", lambda prompt: "y") is False
    assert confirm_effect("external-submit", lambda prompt: "SUBMIT") is True
    assert confirm_effect("external-cancel", lambda prompt: "submit") is False
    assert confirm_effect("external-cancel", lambda prompt: "CANCEL") is True


def test_file_change_requires_lowercase_or_uppercase_yes() -> None:
    from vaspsolkit.interactive_menu import confirm_effect

    assert confirm_effect("file-changing", lambda prompt: "y") is True
    assert confirm_effect("file-changing", lambda prompt: "Y") is True
    assert confirm_effect("file-changing", lambda prompt: "") is False


def test_unavailable_action_is_not_dispatched(tmp_path) -> None:
    from vaspsolkit.guide_model import build_snapshot
    from vaspsolkit.interactive_menu import run_menu_action

    calls = []
    run_menu_action(
        "32",
        build_snapshot(tmp_path),
        cli_main=lambda argv, **kwargs: calls.append(argv) or 0,
        input_fn=lambda prompt: "SUBMIT",
        output=lambda value: None,
    )
    assert calls == []


def test_fixed_submit_dispatches_selected_prepared_jobs_after_exact_confirmation(
    tmp_path,
) -> None:
    from vaspsolkit.config import KitConfig, write_kit_config
    from vaspsolkit.guide_model import build_snapshot
    from vaspsolkit.interactive_menu import run_menu_action
    from vaspsolkit.operations.actions import ResourceRequest
    from vaspsolkit.state import JobRecord, WorkflowState

    write_kit_config(tmp_path / "vaspsolkit.json", KitConfig())
    WorkflowState(
        stage="charge_ready",
        neutral=JobRecord(".", "CONVERGED", "neutral-old"),
        jobs={
            "1": JobRecord("charge_sweep/1", "PREPARED"),
            "2": JobRecord("charge_sweep/2", "CONVERGED", "charge-old"),
        },
        prepared_checked=True,
    ).save(tmp_path / "vaspsolkit.state.json")
    answers = iter(["1", "SUBMIT"])
    calls = []
    output = []
    resources = ResourceRequest.create(
        allocation="specified",
        nodes=("node24",),
        cores=40,
        queue="normal",
        walltime="48:00:00",
        script="vasp.pbs",
    )

    code = run_menu_action(
        "32",
        build_snapshot(tmp_path),
        cli_main=lambda argv, **kwargs: calls.append(argv) or 0,
        resource_selector=lambda config, **kwargs: resources,
        input_fn=lambda prompt: next(answers),
        output=output.append,
    )

    assert code == 0
    assert calls == [
        [
            "submit-selected",
            "--workdir",
            str(tmp_path.resolve()),
            "--config",
            str((tmp_path / "vaspsolkit.json").resolve()),
            "--yes",
            "1",
            "--resource-allocation",
            "specified",
            "--resource-node",
            "node24",
            "--resource-cores",
            "40",
        ]
    ]
    assert any("最终提交配置" in line for line in output)
    assert any("node24" in line for line in output)
    assert any("核心数：40" in line for line in output)


def _write_neutral_submission_case(root: Path) -> None:
    from vaspsolkit.config import KitConfig, write_kit_config
    from vaspsolkit.state import JobRecord, WorkflowState

    for name in ("POSCAR", "INCAR", "KPOINTS", "POTCAR", "vasp.pbs"):
        (root / name).write_text("input\n", encoding="utf-8")
    write_kit_config(root / "vaspsolkit.json", KitConfig())
    WorkflowState(
        stage="neutral_prepared",
        neutral=JobRecord(".", "PREPARED"),
    ).save(root / "vaspsolkit.state.json")


def test_resource_prompt_cancel_never_dispatches_cli(tmp_path) -> None:
    from vaspsolkit.guide_model import build_snapshot
    from vaspsolkit.interactive_menu import run_menu_action

    _write_neutral_submission_case(tmp_path)
    calls = []
    code = run_menu_action(
        "21",
        build_snapshot(tmp_path),
        cli_main=lambda argv, **kwargs: calls.append(argv) or 0,
        resource_selector=lambda config, **kwargs: None,
        input_fn=lambda prompt: "SUBMIT",
        output=lambda value: None,
    )

    assert code == 1
    assert calls == []


def test_rejected_submit_does_not_dispatch_or_persist_resources(tmp_path) -> None:
    from vaspsolkit.guide_model import build_snapshot
    from vaspsolkit.interactive_menu import run_menu_action
    from vaspsolkit.operations.actions import ResourceRequest

    _write_neutral_submission_case(tmp_path)
    config_path = tmp_path / "vaspsolkit.json"
    before = config_path.read_bytes()
    resources = ResourceRequest.create(
        allocation="auto",
        nodes=(),
        cores=32,
        queue="",
        walltime="48:00:00",
        script="vasp.pbs",
        persist=True,
    )
    calls = []
    code = run_menu_action(
        "21",
        build_snapshot(tmp_path),
        cli_main=lambda argv, **kwargs: calls.append(argv) or 0,
        resource_selector=lambda config, **kwargs: resources,
        input_fn=lambda prompt: "NO",
        output=lambda value: None,
    )

    assert code == 1
    assert calls == []
    assert config_path.read_bytes() == before


def test_formal_package_has_no_ui_runtime_or_textual_dependency() -> None:
    root = Path(__file__).parents[1]
    project = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert not (root / "vaspsolkit" / "workbench").exists()
    assert not (root / "vaspsolkit" / "textual_ui.py").exists()
    assert not (root / "vaspsolkit" / "tui.py").exists()
    assert '"textual' not in project
    assert 'testpaths = ["tests"]' in project


def test_public_tree_excludes_retired_ui_development_archive() -> None:
    root = Path(__file__).parents[1]

    assert not (root / "archive" / "terminal-ui").exists()
    assert not (root / "docs" / "superpowers").exists()
