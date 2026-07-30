from __future__ import annotations

import math

import pytest


def test_workflow_config_loads_legacy_reference_as_unconfirmed() -> None:
    from vaspsolkit.config import WorkflowConfig

    workflow = WorkflowConfig.from_dict({"she_reference": 4.70})
    assert workflow.she_reference == 4.70
    assert workflow.she_reference_source == ""
    assert workflow.she_reference_confirmed is False


@pytest.mark.parametrize("value", [0.0, -1.0, math.nan, math.inf, -math.inf])
def test_reference_value_must_be_finite_and_positive(value: float) -> None:
    from vaspsolkit.reference_settings import validate_she_reference

    with pytest.raises(ValueError, match="有限正数"):
        validate_she_reference(value)


def test_reference_value_marks_only_unusual_positive_values() -> None:
    from vaspsolkit.reference_settings import unusual_she_reference

    assert unusual_she_reference(4.70) is False
    assert unusual_she_reference(2.99) is True
    assert unusual_she_reference(6.01) is True


def test_config_write_is_atomic_when_replace_fails(tmp_path, monkeypatch) -> None:
    import os

    from vaspsolkit.config import KitConfig, write_kit_config

    path = tmp_path / "vaspsolkit.json"
    write_kit_config(path, KitConfig())
    before = path.read_bytes()
    changed = KitConfig()
    changed.workflow.she_reference = 4.44

    def fail_replace(source, destination):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        write_kit_config(path, changed)
    assert path.read_bytes() == before


def test_prompt_reference_accepts_default_and_source() -> None:
    from vaspsolkit.reference_settings import prompt_reference_settings

    answers = iter(["", "project convention"])
    settings = prompt_reference_settings(
        default_value=4.70,
        default_source="",
        input_fn=lambda prompt: next(answers),
        output=lambda value: None,
    )
    assert settings.value == 4.70
    assert settings.source == "project convention"
    assert settings.confirmed


def test_prompt_reference_reprompts_invalid_and_confirms_unusual() -> None:
    from vaspsolkit.reference_settings import prompt_reference_settings

    answers = iter(["bad", "6.2", "n", "4.44", "source"])
    messages = []
    settings = prompt_reference_settings(
        default_value=4.70,
        default_source="",
        input_fn=lambda prompt: next(answers),
        output=messages.append,
    )
    assert settings.value == 4.44
    assert any("有限正数" in line for line in messages)
    assert any("非常用范围" in line for line in messages)


def test_explicit_reference_never_prompts() -> None:
    from vaspsolkit.reference_settings import prompt_reference_settings

    settings = prompt_reference_settings(
        default_value=4.70,
        default_source="",
        explicit_value=4.44,
        explicit_source="DOI:example",
        input_fn=lambda prompt: (_ for _ in ()).throw(AssertionError(prompt)),
        output=lambda value: None,
    )
    assert (settings.value, settings.source) == (4.44, "DOI:example")


def test_init_explicit_reference_reaches_existing_incar_fast_path(tmp_path) -> None:
    import json

    from vaspsolkit.cli import main

    (tmp_path / "POSCAR").write_text(
        "sample\n1\n1 0 0\n0 1 0\n0 0 1\nC\n1\nDirect\n0 0 0\n",
        encoding="utf-8",
    )
    (tmp_path / "POTCAR").write_text(
        "TITEL = PAW_PBE C\nENMAX = 400 eV\n", encoding="utf-8"
    )
    (tmp_path / "KPOINTS").write_text(
        "Gamma\n0\nGamma\n1 1 1\n0 0 0\n", encoding="utf-8"
    )
    (tmp_path / "INCAR").write_text(
        "ENCUT = 450\nIBRION = 1\nNSW = 50\n", encoding="utf-8"
    )
    (tmp_path / "vasp.pbs").write_text("#!/bin/bash\n", encoding="utf-8")

    assert main(
        [
            "init", "--workdir", str(tmp_path), "--scheduler", "pbs",
            "--script", "vasp.pbs", "--she-reference", "4.44",
            "--she-reference-source", "DOI:example", "--yes",
        ],
        input_fn=lambda prompt: (_ for _ in ()).throw(AssertionError(prompt)),
    ) == 0
    workflow = json.loads((tmp_path / "vaspsolkit.json").read_text())["workflow"]
    assert workflow["she_reference"] == 4.44
    assert workflow["she_reference_source"] == "DOI:example"
    assert workflow["she_reference_confirmed"] is True


def test_configure_reference_explicitly_updates_only_reference_fields(tmp_path) -> None:
    from vaspsolkit.cli import main
    from vaspsolkit.config import KitConfig, load_kit_config, write_kit_config

    path = tmp_path / "vaspsolkit.json"
    write_kit_config(path, KitConfig())
    assert main([
        "configure-reference", "--workdir", str(tmp_path),
        "--she-reference", "4.44", "--she-reference-source", "DOI:example", "--yes",
    ]) == 0
    config = load_kit_config(path)
    assert config.workflow.she_reference == 4.44
    assert config.workflow.she_reference_source == "DOI:example"
    assert config.workflow.she_reference_confirmed is True
    assert config.scheduler.cores == 48


def test_reference_freshness_distinguishes_missing_current_stale_and_unknown(tmp_path) -> None:
    import csv

    from vaspsolkit.config import WorkflowConfig
    from vaspsolkit.reference_settings import inspect_reference_freshness

    summary = tmp_path / "summary.csv"
    workflow = WorkflowConfig(
        she_reference=4.70,
        she_reference_source="source",
        she_reference_confirmed=True,
    )
    assert inspect_reference_freshness(summary, workflow).status == "missing"

    def write(value="4.70", source="source", include=True):
        fields = ["folder", "u_vs_she"]
        if include:
            fields += ["she_reference_eV", "she_reference_source"]
        with summary.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            row = {"folder": "1", "u_vs_she": "0.0"}
            if include:
                row.update(she_reference_eV=value, she_reference_source=source)
            writer.writerow(row)

    write()
    assert inspect_reference_freshness(summary, workflow).status == "current"
    write("4.44")
    assert inspect_reference_freshness(summary, workflow).status == "stale"
    write(include=False)
    assert inspect_reference_freshness(summary, workflow).status == "unknown"


def test_collected_charge_row_carries_reference_provenance(tmp_path, monkeypatch) -> None:
    from types import SimpleNamespace

    from vaspsolkit.config import WorkflowConfig
    from vaspsolkit.workflow import _collect_charge_row

    workflow = WorkflowConfig(
        she_reference=4.44,
        she_reference_source="source",
        she_reference_confirmed=True,
    )
    monkeypatch.setattr("vaspsolkit.workflow.parse_outcar", lambda path: SimpleNamespace(nelect=10.0, efermi=-3.0, toten=-20.0, converged=True))
    monkeypatch.setattr("vaspsolkit.workflow.slab_area_from_poscar", lambda path: 10.0)
    row = _collect_charge_row(tmp_path, workflow, "1", 10.0, 5.0, {})
    assert row["she_reference_eV"] == 4.44
    assert row["she_reference_source"] == "source"


def test_case_snapshot_exposes_unconfirmed_reference_and_summary_status(tmp_path) -> None:
    from vaspsolkit.config import KitConfig, write_kit_config
    from vaspsolkit.tui_model import inspect_case

    write_kit_config(tmp_path / "vaspsolkit.json", KitConfig())
    snapshot = inspect_case(tmp_path)
    assert snapshot.reference_confirmed is False
    assert snapshot.reference_results_status == "missing"
    assert any(item.code == "she-reference-unconfirmed" for item in snapshot.diagnostics)


def test_guide_prioritizes_reference_confirmation_for_legacy_case(tmp_path) -> None:
    from vaspsolkit.config import KitConfig, write_kit_config
    from vaspsolkit.guide_model import build_snapshot, recommend_action

    for name in ("POSCAR", "INCAR", "KPOINTS", "POTCAR"):
        (tmp_path / name).write_text("input\n")
    write_kit_config(tmp_path / "vaspsolkit.json", KitConfig())
    action = recommend_action(build_snapshot(tmp_path))
    assert action.cli_command == "configure-reference"


def test_postprocess_menu_is_blocked_for_stale_reference_summary(tmp_path) -> None:
    import csv

    from vaspsolkit.config import KitConfig, WorkflowConfig, write_kit_config
    from vaspsolkit.guide_model import build_snapshot
    from vaspsolkit.interactive_menu import action_availability
    from vaspsolkit.menu_actions import action_by_code

    for name in ("POSCAR", "INCAR", "KPOINTS", "POTCAR", "vasp.pbs"):
        (tmp_path / name).write_text("input\n")
    write_kit_config(tmp_path / "vaspsolkit.json", KitConfig(workflow=WorkflowConfig(she_reference_confirmed=True)))
    summary = tmp_path / "results" / "summary.csv"
    summary.parent.mkdir()
    with summary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["folder", "she_reference_eV", "she_reference_source"])
        writer.writeheader()
        writer.writerow({"folder": "1", "she_reference_eV": 4.44, "she_reference_source": ""})
    available, reason = action_availability(action_by_code("62"), build_snapshot(tmp_path))
    assert available is False
    assert "参考" in reason
