from __future__ import annotations

import csv
import json
from pathlib import Path


def _write_summary(path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=(
                "folder",
                "converged",
                "fit_included",
                "delta_electrons",
                "u_vs_she",
                "energy_at_potential",
                "she_reference_eV",
                "she_reference_source",
            ),
        )
        writer.writeheader()
        for index, potential in enumerate((-1.0, -0.5, 0.0, 0.5, 1.0), 1):
            writer.writerow(
                {
                    "folder": str(index),
                    "converged": 1,
                    "fit_included": 1,
                    "delta_electrons": potential,
                    "u_vs_she": potential,
                    "energy_at_potential": -((potential - 0.2) ** 2) - 10.0,
                    "she_reference_eV": 4.70,
                    "she_reference_source": "project convention",
                }
            )


def test_versioned_postprocess_never_overwrites_previous_analysis(tmp_path: Path) -> None:
    from vaspsolkit.postprocess import postprocess_versioned

    summary = tmp_path / "summary.csv"
    history = tmp_path / "results" / "history"
    _write_summary(summary)

    first = postprocess_versioned(summary, history, run_id="run-001")
    first_analysis = first.analysis_path.read_bytes()
    second = postprocess_versioned(summary, history, run_id="run-002")

    assert first.run_dir == history / "run-001"
    assert second.run_dir == history / "run-002"
    assert first.analysis_path.read_bytes() == first_analysis
    for result in (first, second):
        assert (result.run_dir / "summary.csv").read_bytes() == summary.read_bytes()
        assert (result.run_dir / "analysis-log.md").is_file()
        analysis = json.loads(result.analysis_path.read_text(encoding="utf-8"))
        assert analysis["provenance"]["run_id"] == result.run_dir.name
        assert analysis["provenance"]["summary_sha256"]
        assert analysis["provenance"]["she_reference_eV"] == 4.70
        assert analysis["provenance"]["she_reference_source"] == "project convention"


def test_versioned_postprocess_rejects_existing_or_unsafe_run_id(tmp_path: Path) -> None:
    from vaspsolkit.postprocess import postprocess_versioned

    summary = tmp_path / "summary.csv"
    history = tmp_path / "history"
    _write_summary(summary)
    postprocess_versioned(summary, history, run_id="run-001")

    for run_id in ("run-001", "../escape", "contains space"):
        try:
            postprocess_versioned(summary, history, run_id=run_id)
        except (FileExistsError, ValueError):
            pass
        else:
            raise AssertionError(f"unsafe or existing run id was accepted: {run_id}")


def test_workbench_postprocess_is_previewed_and_revalidates_summary(tmp_path: Path) -> None:
    from vaspsolkit.config import KitConfig, WorkflowConfig, write_kit_config
    from vaspsolkit.operations.controller import WorkbenchController

    case = tmp_path / "case"
    case.mkdir()
    write_kit_config(case / "vaspsolkit.json", KitConfig(workflow=WorkflowConfig(
        she_reference=4.70,
        she_reference_source="project convention",
        she_reference_confirmed=True,
    )))
    summary = case / "results" / "summary.csv"
    summary.parent.mkdir()
    _write_summary(summary)
    controller = WorkbenchController(case)

    plan = controller.plan("postprocess")
    assert plan.effect == "file-changing"
    assert plan.commands_summary == ("postprocess × 1",)
    assert plan.blocked_reason == ""
    assert "不会覆盖" in " ".join(plan.warnings)
    summary.write_text(summary.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    try:
        controller.execute(plan, confirmed=True)
    except RuntimeError as exc:
        assert "summary.csv 已变化" in str(exc)
    else:
        raise AssertionError("stale postprocess preview was executed")

    fresh = controller.plan("postprocess")
    result = controller.execute(fresh, confirmed=True)
    assert result.ok
    histories = tuple((case / "results" / "history").iterdir())
    assert len(histories) == 1
    assert (histories[0] / "analysis.json").is_file()
    assert result.snapshot is not None
    assert tuple(row.name for row in result.snapshot.analysis_runs) == (
        histories[0].name,
    )
    assert result.snapshot.analysis_runs[0].status == "COMPLETE"


def test_workbench_postprocess_blocks_stale_she_reference(tmp_path: Path) -> None:
    from vaspsolkit.config import KitConfig, WorkflowConfig, write_kit_config
    from vaspsolkit.operations.controller import WorkbenchController

    case = tmp_path / "case"
    case.mkdir()
    write_kit_config(case / "vaspsolkit.json", KitConfig(workflow=WorkflowConfig(she_reference=4.44, she_reference_confirmed=True)))
    summary = case / "results" / "summary.csv"
    summary.parent.mkdir()
    _write_summary(summary)
    plan = WorkbenchController(case).plan("postprocess")
    assert "SHE reference" in plan.blocked_reason

