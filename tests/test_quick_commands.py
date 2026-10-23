from pathlib import Path

import pytest

from tests.test_cli_user_experience import _write_synthetic_inputs
from vaspsolkit import cli
from vaspsolkit.guide_model import build_snapshot, recommend_action
from vaspsolkit.interactive_menu import run_menu_action


def test_missing_inputs_recommend_an_executable_action(tmp_path):
    _write_synthetic_inputs(tmp_path)
    (tmp_path / "KPOINTS").unlink()
    action = recommend_action(build_snapshot(tmp_path))
    assert action.cli_command == "inputs"
    assert action.effect == "file-changing"


def test_menu_input_completion_routes_to_same_cli_without_extra_confirmation(tmp_path):
    _write_synthetic_inputs(tmp_path)
    (tmp_path / "KPOINTS").unlink()
    calls = []
    result = run_menu_action("14", build_snapshot(tmp_path), cli_main=lambda argv, **kw: calls.append(argv) or 0, input_fn=lambda p: pytest.fail(p), output=lambda _: None)
    assert result == 0
    assert calls[0][0] == "inputs"
    assert "--yes" not in calls[0]


def test_next_dry_run_is_read_only_and_shows_action(tmp_path, monkeypatch):
    _write_synthetic_inputs(tmp_path)
    (tmp_path / "KPOINTS").unlink()
    messages = []
    assert cli.main(["next", "--workdir", str(tmp_path), "--dry-run"], input_fn=lambda p: pytest.fail(p), output=messages.append) == 0
    assert any("inputs" in line for line in messages)
    assert not (tmp_path / "KPOINTS").exists()
    assert not (tmp_path / "vaspsolkit.state.json").exists()


def test_next_routes_through_menu_recommended_action(tmp_path, monkeypatch):
    from vaspsolkit import interactive_menu
    _write_synthetic_inputs(tmp_path)
    calls = []
    monkeypatch.setattr(interactive_menu, "synchronize_case", lambda *a, **kw: interactive_menu.SyncResult())
    monkeypatch.setattr(interactive_menu, "run_menu_action", lambda code, snapshot, **kw: calls.append((code, snapshot.case.workdir)) or 0)
    assert cli.main(["next", "--workdir", str(tmp_path)], input_fn=lambda _: "n", output=lambda _: None) == 0
    assert calls == [("02", tmp_path)]


def test_init_can_complete_inputs_from_template_and_explicit_mesh(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "user-config"))
    _write_synthetic_inputs(tmp_path)
    template = tmp_path / "INCAR.template"
    (tmp_path / "INCAR").rename(template)
    (tmp_path / "KPOINTS").unlink()
    assert cli.main([
        "init", "--workdir", str(tmp_path), "--yes", "--scheduler", "slurm", "--script", "vasp.slurm",
        "--she-reference", "4.7", "--incar-template", str(template), "--mesh", "2", "2", "1",
    ], input_fn=lambda p: pytest.fail(p), output=lambda _: None) == 0
    assert (tmp_path / "vaspsolkit.json").exists()
    assert "2 2 1" in (tmp_path / "KPOINTS").read_text()


def test_kspacing_only_cli_init_then_prepare(tmp_path):
    _write_synthetic_inputs(tmp_path)
    (tmp_path / "KPOINTS").unlink()
    with (tmp_path / "INCAR").open("a") as handle:
        handle.write("KSPACING=0.3\nKGAMMA=.TRUE.\n")
    assert cli.main(["init", "--workdir", str(tmp_path), "--yes", "--scheduler", "slurm", "--script", "vasp.slurm", "--she-reference", "4.7"], output=lambda _: None) == 0
    assert cli.main(["prepare-neutral", "--workdir", str(tmp_path), "--yes"], output=lambda _: None) == 0
    assert not (tmp_path / "KPOINTS").exists()
    assert "KSPACING" in (tmp_path / "INCAR").read_text()


def test_recommendation_progresses_from_collection_to_fit_to_results(tmp_path):
    import hashlib
    import json
    from vaspsolkit.config import KitConfig
    from vaspsolkit.guide_model import GuideSnapshot
    from vaspsolkit.tui_model import CaseSnapshot

    config = KitConfig()
    case = CaseSnapshot(tmp_path, tmp_path / "vaspsolkit.json", neutral_status="CONVERGED", charge_total=5,
                        charge_statuses=tuple((str(i), "CONVERGED") for i in range(5)), prepared_checked=True,
                        reference_confirmed=True, results_available=True, reference_results_status="current", config=config)
    snapshot = GuideSnapshot(case, (), True)
    results = tmp_path / config.workflow.results_root
    results.mkdir()
    summary = results / config.workflow.summary_file
    summary.write_bytes(b"synthetic summary for routing only\n")
    assert recommend_action(snapshot).cli_command == "postprocess"
    fit_dir = results / "eu"
    fit_dir.mkdir()
    (fit_dir / "analysis.json").write_text(json.dumps({"provenance": {"summary_sha256": hashlib.sha256(summary.read_bytes()).hexdigest()}}))
    assert recommend_action(snapshot).cli_command == "postprocess"
    for name in ("eu_curve.csv", "eu_curve.png", "report.md", "quality_report.csv", "points_to_rerun.csv"):
        (fit_dir / name).write_text("synthetic routing artifact\n")
    action = recommend_action(snapshot)
    assert action.name == "results-ready"
    assert action.effect == "read-only"
    assert run_menu_action("02", snapshot, input_fn=lambda p: pytest.fail(p), output=lambda _: None) == 0
    summary.write_bytes(b"changed summary\n")
    assert recommend_action(snapshot).cli_command == "postprocess"


def test_menu_postprocess_does_not_overwrite_detailed_audit(tmp_path):
    from vaspsolkit.config import KitConfig
    from vaspsolkit.guide_model import GuideSnapshot
    from vaspsolkit.tui_model import CaseSnapshot
    from vaspsolkit.interactive_menu import _fixed_cli_argv
    from vaspsolkit.menu_actions import action_by_code
    config = KitConfig()
    snapshot = GuideSnapshot(CaseSnapshot(tmp_path, tmp_path / "vaspsolkit.json", config=config), (), True)
    argv = _fixed_cli_argv(action_by_code("62"), snapshot)
    assert Path(argv[argv.index("--output") + 1]) == tmp_path / config.workflow.results_root / "eu"
