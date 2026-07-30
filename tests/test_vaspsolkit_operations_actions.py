from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest


def _write_base_inputs(root: Path) -> None:
    (root / "POSCAR").write_text(
        "PtO\n1\n1 0 0\n0 1 0\n0 0 1\nPt O\n1 1\nDirect\n0 0 0\n0 0 0\n",
        encoding="utf-8",
    )
    (root / "INCAR").write_text(
        "ENCUT = 450\nIBRION = 2\nNSW = 100\n", encoding="utf-8"
    )
    (root / "KPOINTS").write_text("Gamma\n0\nGamma\n1 1 1\n0 0 0\n", encoding="utf-8")
    (root / "POTCAR").write_text(
        "TITEL = PAW_PBE Pt 01Jan2000\nENMAX = 300 eV\n"
        "TITEL = PAW_PBE O 01Jan2000\nENMAX = 400 eV\n",
        encoding="utf-8",
    )
    (root / "vasp.pbs").write_text("#!/bin/sh\n", encoding="utf-8")


def _write_config(root: Path) -> None:
    from vaspsolkit.config import KitConfig, WorkflowConfig, write_kit_config

    write_kit_config(root / "vaspsolkit.json", KitConfig(workflow=WorkflowConfig(she_reference_confirmed=True)))


def _prepared_case(root: Path) -> None:
    from vaspsolkit.orchestrator import STATE_FILENAME
    from vaspsolkit.state import JobRecord, WorkflowState

    _write_base_inputs(root)
    _write_config(root)
    WorkflowState(neutral=JobRecord(folder=".", status="PREPARED")).save(
        root / STATE_FILENAME
    )


def _resources():
    from vaspsolkit.operations.actions import ResourceRequest

    return ResourceRequest.create(
        allocation="specified",
        nodes=(" node03 ", "node03"),
        cores=32,
        queue="workq",
        walltime="100:05:09",
        script="vasp.pbs",
        persist=True,
    )


def test_resource_request_normalizes_specified_nodes_and_is_frozen():
    from vaspsolkit.operations.actions import ResourceRequest

    resources = ResourceRequest.create(
        allocation="specified", nodes=(" node03 ", "node03"), cores=32,
        queue="workq", walltime="100:05:09", script="vasp.pbs", persist=True,
    )

    assert resources.nodes == ("node03",)
    assert resources.walltime == "100:05:09"
    assert resources.validate() is None
    with pytest.raises(dataclasses.FrozenInstanceError):
        resources.cores = 64


def test_resource_request_allows_cluster_default_queue():
    from vaspsolkit.operations.actions import ResourceRequest

    request = ResourceRequest.create(
        allocation="auto", nodes=(), cores=48, queue="",
        walltime="48:00:00", script="vasp.pbs",
    )

    assert request.queue == ""


def test_resource_request_rejects_more_than_one_named_node():
    from vaspsolkit.operations.actions import ResourceRequest

    with pytest.raises(ValueError, match="one|single|一个|单个"):
        ResourceRequest.create(
            allocation="specified", nodes=("node03", "node05"), cores=48,
            queue="", walltime="48:00:00", script="vasp.pbs",
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"allocation": 1},
        {"nodes": ["node01"]},
        {"nodes": (1,)},
        {"cores": "48"},
        {"queue": None},
        {"walltime": 48},
        {"script": object()},
        {"persist": 1},
    ],
)
def test_resource_request_direct_constructor_rejects_wrong_types(overrides):
    from vaspsolkit.operations.actions import ResourceRequest

    values = dict(
        allocation="specified",
        nodes=("node01",),
        cores=48,
        queue="workq",
        walltime="48:00:00",
        script="vasp.pbs",
        persist=False,
    )
    values.update(overrides)

    with pytest.raises(TypeError):
        ResourceRequest(**values)


def test_resource_request_direct_constructor_normalizes_but_rejects_empty_node():
    from vaspsolkit.operations.actions import ResourceRequest

    request = ResourceRequest(
        allocation="specified",
        nodes=(" node01 ", "node01"),
        cores=48,
        queue=" workq ",
        walltime="48:00:00",
        script=" vasp.pbs ",
    )
    assert request.nodes == ("node01",)
    assert request.queue == "workq"
    assert request.script == "vasp.pbs"

    with pytest.raises(ValueError, match="nodes"):
        ResourceRequest(
            allocation="specified",
            nodes=("node01", "  "),
            cores=48,
            queue="workq",
            walltime="48:00:00",
            script="vasp.pbs",
        )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"allocation": "manual"}, "allocation"),
        ({"allocation": "specified", "nodes": ()}, "nodes"),
        ({"allocation": "auto", "nodes": ("node01",)}, "nodes"),
        ({"cores": 0}, "cores"),
        ({"script": ""}, "script"),
        ({"walltime": "2:60:00"}, "walltime"),
        ({"walltime": "02:10"}, "walltime"),
    ],
)
def test_resource_request_rejects_invalid_values(kwargs, message):
    from vaspsolkit.operations.actions import ResourceRequest

    values = dict(
        allocation="auto",
        nodes=(),
        cores=48,
        queue="workq",
        walltime="48:00:00",
        script="vasp.pbs",
    )
    values.update(kwargs)

    with pytest.raises(ValueError, match=message):
        ResourceRequest.create(**values)


def test_action_plan_requires_resolved_case_and_valid_action_effect(tmp_path):
    from vaspsolkit.operations.actions import ActionPlan

    values = dict(
        action_id="refresh",
        effect="read-only",
        target_case=tmp_path.resolve(),
        target_jobs=(),
        title="刷新",
        reason="只查询已记录 Job ID",
    )
    plan = ActionPlan(**values)
    assert dataclasses.is_dataclass(plan)
    assert plan.validate() is None
    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.title = "changed"
    with pytest.raises(ValueError, match="resolved"):
        ActionPlan(**{**values, "target_case": tmp_path / ".." / tmp_path.name})
    with pytest.raises(ValueError, match="action"):
        ActionPlan(**{**values, "action_id": "qsub-everything"})
    with pytest.raises(ValueError, match="effect"):
        ActionPlan(**{**values, "effect": "destructive"})


@pytest.mark.parametrize(
    ("action_id", "expected_effect"),
    [
        ("refresh", "read-only"),
        ("fix-inputs", "read-only"),
        ("monitor", "read-only"),
        ("check-prepared", "file-changing"),
        ("check", "read-only"),
        ("init", "file-changing"),
        ("prepare-neutral", "file-changing"),
        ("prepare-charge", "file-changing"),
        ("collect", "file-changing"),
        ("submit-neutral", "external"),
        ("submit-selected", "external"),
    ],
)
def test_action_plan_rejects_action_effect_mismatch(tmp_path, action_id, expected_effect):
    from vaspsolkit.operations.actions import ACTION_EFFECTS, ActionPlan

    assert ACTION_EFFECTS[action_id] == expected_effect
    wrong_effect = next(
        effect for effect in ("read-only", "file-changing", "external")
        if effect != expected_effect
    )
    with pytest.raises(ValueError, match="effect"):
        ActionPlan(
            action_id=action_id,
            effect=wrong_effect,
            target_case=tmp_path.resolve(),
            target_jobs=(),
            title="动作",
            reason="原因",
        )


def test_action_plan_deep_freezes_sequence_inputs(tmp_path):
    from vaspsolkit.operations.actions import ActionPlan, FileDiff

    jobs = ["neutral"]
    diffs = [FileDiff((tmp_path / "INCAR").resolve(), "old", "new", "modify")]
    commands = ["qsub × 1"]
    warnings = ["preview only"]
    plan = ActionPlan(
        action_id="submit-neutral",
        effect="external",
        target_case=tmp_path.resolve(),
        target_jobs=jobs,
        title="提交",
        reason="已准备",
        file_diffs=diffs,
        commands_summary=commands,
        warnings=warnings,
    )
    jobs.append("charge")
    diffs.clear()
    commands.clear()
    warnings.clear()

    assert plan.target_jobs == ("neutral",)
    assert len(plan.file_diffs) == 1
    assert plan.commands_summary == ("qsub × 1",)
    assert plan.warnings == ("preview only",)


def test_file_diffs_must_be_absolute_normalized_and_inside_case(tmp_path):
    from vaspsolkit.operations.actions import ActionPlan, FileDiff

    case = (tmp_path / "case").resolve()
    case.mkdir()
    outside = (tmp_path / "outside").resolve()
    outside.write_text("outside", encoding="utf-8")

    with pytest.raises(ValueError, match="absolute"):
        FileDiff(Path("INCAR"), "old", "new", "update")
    with pytest.raises(ValueError, match="within"):
        ActionPlan(
            action_id="init",
            effect="file-changing",
            target_case=case,
            target_jobs=(),
            title="init",
            reason="preview",
            file_diffs=(FileDiff(outside, "old", "new", "update"),),
        )

    link = case / "linked"
    link.symlink_to(outside)
    with pytest.raises(ValueError, match="within"):
        ActionPlan(
            action_id="init",
            effect="file-changing",
            target_case=case,
            target_jobs=(),
            title="init",
            reason="preview",
            file_diffs=(FileDiff(link, "old", "new", "update"),),
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"action_id": 1},
        {"effect": None},
        {"target_case": "/tmp/case"},
        {"target_jobs": [1]},
        {"file_diffs": ["not-a-diff"]},
        {"scheduler_request": object()},
        {"commands_summary": [""]},
        {"warnings": [1]},
        {"title": None},
        {"reason": ""},
        {"blocked_reason": None},
    ],
)
def test_action_plan_rejects_malformed_fields(tmp_path, overrides):
    from vaspsolkit.operations.actions import ActionPlan

    values = dict(
        action_id="refresh",
        effect="read-only",
        target_case=tmp_path.resolve(),
        target_jobs=(),
        title="刷新",
        reason="只读",
    )
    values.update(overrides)
    with pytest.raises((TypeError, ValueError)):
        ActionPlan(**values)


@pytest.mark.parametrize(
    "overrides",
    [
        {"path": "INCAR"},
        {"before": 1},
        {"after": object()},
        {"change_type": ""},
        {"change_type": None},
    ],
)
def test_file_diff_rejects_malformed_fields(overrides):
    from vaspsolkit.operations.actions import FileDiff

    values = dict(path=Path("INCAR"), before=None, after="new", change_type="modify")
    values.update(overrides)
    with pytest.raises((TypeError, ValueError)):
        FileDiff(**values)


@pytest.mark.parametrize("script", ("/tmp/vasp.pbs", "../vasp.pbs", "jobs/../../vasp.pbs"))
def test_resource_request_rejects_script_outside_case(script):
    from vaspsolkit.operations.actions import ResourceRequest

    with pytest.raises(ValueError, match="script"):
        ResourceRequest(
            allocation="auto",
            nodes=(),
            cores=48,
            queue="workq",
            walltime="48:00:00",
            script=script,
        )


def test_refresh_plan_and_execution_never_construct_scheduler(tmp_path):
    from vaspsolkit.operations.controller import WorkbenchController

    _write_base_inputs(tmp_path)

    def fail_scheduler_factory(_config):
        raise AssertionError("scheduler factory must not be called")

    controller = WorkbenchController(tmp_path, scheduler_factory=fail_scheduler_factory)
    plan = controller.plan("refresh")
    result = controller.execute(plan)

    assert plan.effect == "read-only"
    assert plan.action_id == "refresh"
    assert not hasattr(plan, "action")
    assert "只查询已记录 Job ID" in plan.reason
    assert plan.commands_summary == ()
    assert result.snapshot.workdir == tmp_path.resolve()


def test_submit_neutral_plan_preserves_resources_when_prepared(tmp_path):
    from vaspsolkit.operations.controller import WorkbenchController

    _prepared_case(tmp_path)
    resources = _resources()
    controller = WorkbenchController(
        tmp_path,
        scheduler_factory=lambda _config: pytest.fail("scheduler must not be constructed"),
    )

    plan = controller.plan("submit-neutral", resources)

    assert plan.effect == "external"
    assert plan.action_id == "submit-neutral"
    assert plan.target_case == tmp_path.resolve()
    assert plan.target_jobs == ("neutral",)
    assert plan.commands_summary == ("qsub × 1",)
    assert plan.scheduler_request is resources
    assert plan.blocked_reason == ""


def test_submit_neutral_is_blocked_until_neutral_is_prepared(tmp_path):
    from vaspsolkit.operations.controller import WorkbenchController

    _write_base_inputs(tmp_path)
    _write_config(tmp_path)
    controller = WorkbenchController(tmp_path)

    plan = controller.plan("submit-neutral", _resources())

    assert plan.blocked_reason
    assert "PREPARED" in plan.blocked_reason
    with pytest.raises(RuntimeError, match="PREPARED"):
        controller.execute(plan, confirmed=True)


def test_non_read_only_execution_requires_confirmation(tmp_path):
    from vaspsolkit.state import WorkflowState
    from vaspsolkit.operations.controller import WorkbenchController

    _prepared_case(tmp_path)
    state = WorkflowState.load(tmp_path / "vaspsolkit.state.json")
    assert state.neutral is not None
    state.neutral.metadata["stage"] = "neutral_relax"
    state.save(tmp_path / "vaspsolkit.state.json")

    class FakeScheduler:
        def __init__(self):
            self.calls = 0

        def submit(self, *args, **kwargs):
            self.calls += 1
            return "128042.node01"

    fake = FakeScheduler()
    controller = WorkbenchController(
        tmp_path,
        scheduler_factory=lambda _: fake,
        activity_state_root=tmp_path.parent / f".{tmp_path.name}-activity-state",
    )
    plan = controller.plan("submit-neutral", _resources())

    with pytest.raises(PermissionError, match="确认"):
        controller.execute(plan)
    result = controller.execute(plan, confirmed=True)
    assert result.ok
    assert fake.calls == 1


def test_execute_rejects_plan_from_other_controller_or_case(tmp_path):
    from vaspsolkit.operations.controller import WorkbenchController

    first_case = tmp_path / "first"
    second_case = tmp_path / "second"
    first_case.mkdir()
    second_case.mkdir()
    _write_base_inputs(first_case)
    _write_base_inputs(second_case)
    first = WorkbenchController(first_case)
    same_case_other_controller = WorkbenchController(first_case)
    other_case = WorkbenchController(second_case)
    plan = first.plan("refresh")

    with pytest.raises(RuntimeError, match="controller"):
        same_case_other_controller.execute(plan)
    with pytest.raises(RuntimeError, match="Case"):
        other_case.execute(plan)


def test_cross_case_guard_precedes_blocked_reason(tmp_path):
    from vaspsolkit.operations.controller import WorkbenchController

    first_case = tmp_path / "first"
    second_case = tmp_path / "second"
    first_case.mkdir()
    second_case.mkdir()
    _write_base_inputs(first_case)
    _write_config(first_case)
    _write_base_inputs(second_case)
    blocked = WorkbenchController(first_case).plan("submit-neutral", _resources())

    with pytest.raises(RuntimeError, match="Case"):
        WorkbenchController(second_case).execute(blocked, confirmed=True)


def test_execute_rejects_plan_replaced_by_a_newer_preview(tmp_path):
    from vaspsolkit.operations.controller import WorkbenchController

    _write_base_inputs(tmp_path)
    controller = WorkbenchController(tmp_path)
    stale_plan = controller.plan("refresh")
    controller.plan("refresh")

    with pytest.raises(RuntimeError, match="失效"):
        controller.execute(stale_plan)


def test_refresh_plan_is_consumed_after_success(tmp_path):
    from vaspsolkit.operations.controller import WorkbenchController

    _write_base_inputs(tmp_path)
    controller = WorkbenchController(tmp_path)
    plan = controller.plan("refresh")
    controller.execute(plan)

    with pytest.raises(RuntimeError, match="失效"):
        controller.execute(plan)


def test_submit_neutral_rejects_script_symlink_outside_case(tmp_path):
    from vaspsolkit.operations.actions import ResourceRequest
    from vaspsolkit.operations.controller import WorkbenchController

    case = tmp_path / "case"
    case.mkdir()
    _prepared_case(case)
    outside = tmp_path / "outside.pbs"
    outside.write_text("#!/bin/sh\n", encoding="utf-8")
    (case / "linked.pbs").symlink_to(outside)
    resources = ResourceRequest(
        allocation="auto",
        nodes=(),
        cores=48,
        queue="workq",
        walltime="48:00:00",
        script="linked.pbs",
    )

    with pytest.raises(ValueError, match="script"):
        WorkbenchController(case).plan("submit-neutral", resources)


@pytest.mark.parametrize("action", ("init", "prepare-neutral"))
def test_file_changing_recommendation_has_preview_metadata(tmp_path, action):
    from vaspsolkit.operations.controller import WorkbenchController

    _write_base_inputs(tmp_path)
    if action == "prepare-neutral":
        _write_config(tmp_path)
    controller = WorkbenchController(tmp_path)
    assert controller.snapshot().recommendation.name == action

    plan = controller.plan(action, _resources() if action == "init" else None)

    assert plan.action_id == action
    assert plan.effect == "file-changing"
    assert plan.title
    assert plan.reason
    assert plan.target_case == tmp_path.resolve()
    if action == "init":
        assert len(plan.file_diffs) == 3
        assert plan.warnings == ()
    else:
        assert {item.path.name for item in plan.file_diffs} >= {
            "POSCAR.initial",
            "vaspsolkit.state.json",
        }
        assert plan.warnings == ("旧计算输出如存在将归档",)


@pytest.mark.parametrize(
    ("action", "effect"),
    [
        ("init", "file-changing"),
        ("prepare-neutral", "file-changing"),
        ("monitor", "read-only"),
        ("prepare-charge", "file-changing"),
        ("check-prepared", "file-changing"),
        ("collect", "file-changing"),
        ("check", "read-only"),
    ],
)
def test_each_requested_action_uses_its_own_metadata(tmp_path, action, effect):
    from vaspsolkit.operations.controller import WorkbenchController

    _write_base_inputs(tmp_path)
    controller = WorkbenchController(tmp_path)
    assert controller.snapshot().recommendation.name == "init"

    plan = controller.plan(action, _resources() if action == "init" else None)

    assert plan.action_id == action
    assert plan.effect == effect
    assert plan.title
    assert plan.reason
    if action != "init":
        assert "初始化" not in plan.title
