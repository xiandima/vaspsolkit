from __future__ import annotations

import json
import csv
from pathlib import Path

import pytest


def _analysis(
    path: Path,
    *,
    c: float,
    she_reference: float = 4.70,
    method_fingerprint: str = "sha256:method-a",
    u_min: float = -1.0,
    u_max: float = 1.0,
) -> None:
    path.write_text(
        json.dumps(
            {
                "energy_fit": {"a": -1.0, "b": 0.0, "c": c},
                "fit_domain": {"u_min": u_min, "u_max": u_max, "point_count": 5},
                "provenance": {
                    "she_reference_eV": she_reference,
                    "she_reference_source": "project convention",
                    "potential_convention": "vacuum-reference-vs-she",
                    "energy_convention": "toten-minus-delta-electrons-times-efermi",
                    "fermi_shift_handling": "not-applied",
                    "method_fingerprint": method_fingerprint,
                },
            }
        ),
        encoding="utf-8",
    )


def _reaction_spec(root: Path, *, u_min: float = -1.0, u_max: float = 1.0) -> Path:
    spec = root / "reaction.json"
    spec.write_text(
        json.dumps(
            {
                "name": "example",
                "systems": {"slab": "slab.json", "star_A": "star_A.json"},
                "constants": {"G_A": -0.5},
                "curves": [
                    {
                        "name": "A adsorption",
                        "formula": 'E("star_A", U) - E("slab", U) - G_A',
                    }
                ],
                "grid": {"u_min": u_min, "u_max": u_max, "points": 5},
            }
        ),
        encoding="utf-8",
    )
    return spec


def test_energy_vertex_is_not_unconditionally_reported_as_pzc() -> None:
    from vaspsolkit.analysis import fit_energy_vs_potential

    rows = [
        {"u_vs_she": u, "energy_at_potential": -((u - 0.2) ** 2) - 10.0}
        for u in (-1.0, -0.5, 0.0, 0.5, 1.0)
    ]
    fit = fit_energy_vs_potential(rows)

    assert fit["u_vertex"] == pytest.approx(0.2)
    assert fit["u0"] == pytest.approx(0.2)
    assert fit["pzc"] is None
    assert fit["pzc_basis"] == "unassigned"


def test_charge_zero_potential_is_reported_separately() -> None:
    from vaspsolkit.analysis import analyze_rows

    rows = [
        {
            "u_vs_she": u,
            "energy_at_potential": -((u - 0.2) ** 2) - 10.0,
            "electrode_charge_e": 2.0 * u - 0.4,
        }
        for u in (-1.0, -0.5, 0.0, 0.5, 1.0)
    ]
    result = analyze_rows(rows)

    assert result["pzc_estimate"]["value"] == pytest.approx(0.2)
    assert result["pzc_estimate"]["basis"] == "linear-charge-zero"
    assert result["pzc_estimate"]["within_fit_domain"] is True


def test_postprocess_always_writes_scientific_provenance(tmp_path: Path) -> None:
    from vaspsolkit.postprocess import postprocess_summary

    summary = tmp_path / "summary.csv"
    with summary.open("w", newline="", encoding="utf-8") as handle:
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
                "method_fingerprint",
            ),
        )
        writer.writeheader()
        for index, u in enumerate((-1.0, -0.5, 0.0, 0.5, 1.0), 1):
            writer.writerow(
                {
                    "folder": index,
                    "converged": 1,
                    "fit_included": 1,
                    "delta_electrons": u,
                    "u_vs_she": u,
                    "energy_at_potential": -((u - 0.2) ** 2) - 10.0,
                    "she_reference_eV": 4.70,
                    "she_reference_source": "project convention",
                    "method_fingerprint": "sha256:method-a",
                }
            )

    result = postprocess_summary(summary, tmp_path / "out")
    analysis = json.loads(result.analysis_path.read_text(encoding="utf-8"))

    assert analysis["provenance"]["she_reference_eV"] == pytest.approx(4.70)
    assert analysis["provenance"]["method_fingerprint"] == "sha256:method-a"
    assert analysis["provenance"]["fermi_shift_handling"] == "not-applied"


def test_workflow_config_accepts_method_fingerprint() -> None:
    from vaspsolkit.config import WorkflowConfig

    config = WorkflowConfig.from_dict({"method_fingerprint": "sha256:method-a"})

    assert config.method_fingerprint == "sha256:method-a"


def test_reaction_rejects_unknown_provenance_by_default(tmp_path: Path) -> None:
    from vaspsolkit.reaction import run_reaction_spec

    for name in ("slab", "star_A"):
        (tmp_path / f"{name}.json").write_text(
            json.dumps({"energy_fit": {"a": -1.0, "b": 0.0, "c": -10.0}}),
            encoding="utf-8",
        )

    with pytest.raises(ValueError, match="provenance"):
        run_reaction_spec(_reaction_spec(tmp_path), tmp_path / "out")


def test_reaction_rejects_mixed_method_fingerprints(tmp_path: Path) -> None:
    from vaspsolkit.reaction import run_reaction_spec

    _analysis(tmp_path / "slab.json", c=-10.0, method_fingerprint="sha256:a")
    _analysis(tmp_path / "star_A.json", c=-12.0, method_fingerprint="sha256:b")

    with pytest.raises(ValueError, match="method_fingerprint"):
        run_reaction_spec(_reaction_spec(tmp_path), tmp_path / "out")


def test_reaction_rejects_grid_outside_common_fit_domain(tmp_path: Path) -> None:
    from vaspsolkit.reaction import run_reaction_spec

    _analysis(tmp_path / "slab.json", c=-10.0)
    _analysis(tmp_path / "star_A.json", c=-12.0)

    with pytest.raises(ValueError, match="outside the common fitted potential domain"):
        run_reaction_spec(
            _reaction_spec(tmp_path, u_min=-1.2, u_max=1.0),
            tmp_path / "out",
        )


def test_reaction_accepts_matching_provenance_within_domain(tmp_path: Path) -> None:
    from vaspsolkit.reaction import run_reaction_spec

    _analysis(tmp_path / "slab.json", c=-10.0)
    _analysis(tmp_path / "star_A.json", c=-12.0)

    outputs = run_reaction_spec(_reaction_spec(tmp_path), tmp_path / "out")

    assert outputs.csv_path.is_file()
    report = outputs.report_path.read_text(encoding="utf-8")
    assert "Compatibility checks: passed" in report
    assert "Electronic energy expression" in report


@pytest.mark.parametrize("option", ["allow_unknown_provenance", "allow_mixed_provenance", "allow_extrapolation"])
def test_reaction_report_discloses_enabled_overrides(tmp_path: Path, option: str) -> None:
    from vaspsolkit.reaction import run_reaction_spec

    _analysis(tmp_path / "slab.json", c=-10.0)
    _analysis(tmp_path / "star_A.json", c=-12.0)
    spec_path = _reaction_spec(tmp_path)
    spec = json.loads(spec_path.read_text())
    spec["compatibility"] = {option: True}
    if option == "allow_unknown_provenance":
        analysis_path = tmp_path / "star_A.json"
        analysis = json.loads(analysis_path.read_text())
        del analysis["provenance"]
        analysis_path.write_text(json.dumps(analysis))
    elif option == "allow_mixed_provenance":
        _analysis(tmp_path / "star_A.json", c=-12.0, method_fingerprint="different-method")
    else:
        spec["grid"]["u_min"] = -1.2
    spec_path.write_text(json.dumps(spec))

    outputs = run_reaction_spec(spec_path, tmp_path / "out")
    report = outputs.report_path.read_text()
    assert "Compatibility checks: passed" not in report
    assert option in report
    assert "G_A" in report and "-0.5" in report
    assert "slab.json" in report and "star_A.json" in report
    assert "Warning:" in report


def test_legacy_provenance_does_not_hide_known_fit_domain(tmp_path: Path) -> None:
    from vaspsolkit.reaction import run_reaction_spec

    for name in ("slab", "star_A"):
        (tmp_path / f"{name}.json").write_text(json.dumps({
            "energy_fit": {"a": -1, "b": 0, "c": -10},
            "fit_domain": {"u_min": -0.5, "u_max": 0.5},
        }))
    spec_path = _reaction_spec(tmp_path)
    spec = json.loads(spec_path.read_text())
    spec["compatibility"] = {"allow_unknown_provenance": True}
    spec_path.write_text(json.dumps(spec))

    with pytest.raises(ValueError, match="outside the common fitted potential domain"):
        run_reaction_spec(spec_path, tmp_path / "out")


def test_legacy_report_does_not_invent_fitted_domain(tmp_path: Path) -> None:
    from vaspsolkit.reaction import run_reaction_spec

    for name in ("slab", "star_A"):
        (tmp_path / f"{name}.json").write_text(json.dumps({"a": -1, "b": 0, "c": -10}))
    spec_path = _reaction_spec(tmp_path)
    spec = json.loads(spec_path.read_text())
    spec["compatibility"] = {"allow_unknown_provenance": True}
    spec_path.write_text(json.dumps(spec))

    report = run_reaction_spec(spec_path, tmp_path / "out").report_path.read_text()
    assert "Common fitted domain: unknown" in report
    assert "Requested grid: -1.000000 to 1.000000" in report


@pytest.mark.parametrize("bounds", [(float("nan"), 1), (-1, float("inf")), (1, -1)])
def test_reaction_rejects_invalid_domain_even_with_overrides(tmp_path: Path, bounds) -> None:
    from vaspsolkit.reaction import run_reaction_spec

    _analysis(tmp_path / "slab.json", c=-10.0, u_min=bounds[0], u_max=bounds[1])
    _analysis(tmp_path / "star_A.json", c=-12.0)
    spec_path = _reaction_spec(tmp_path)
    spec = json.loads(spec_path.read_text())
    spec["compatibility"] = {"allow_unknown_provenance": True, "allow_extrapolation": True}
    spec_path.write_text(json.dumps(spec))
    with pytest.raises(ValueError, match="invalid fitted potential domain"):
        run_reaction_spec(spec_path, tmp_path / "out")


def test_excluded_neutral_is_named_in_plot_and_report(tmp_path: Path, monkeypatch) -> None:
    from matplotlib.axes import Axes
    from vaspsolkit.postprocess import postprocess_summary

    summary = tmp_path / "summary.csv"
    with summary.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["folder", "delta_electrons", "u_vs_she", "energy_at_potential", "fit_included"])
        writer.writeheader()
        for index, u in enumerate((-1, -0.5, 0, 0.5, 1), 1):
            writer.writerow({"folder": index, "delta_electrons": u, "u_vs_she": u, "energy_at_potential": -u*u - 10, "fit_included": int(u != 0)})
    labels = []
    scatter = Axes.scatter

    def record_label(self, *args, **kwargs):
        labels.append(kwargs.get("label"))
        return scatter(self, *args, **kwargs)

    monkeypatch.setattr(Axes, "scatter", record_label)
    with pytest.raises(ValueError, match="explicitly allow exclusion"):
        postprocess_summary(summary, tmp_path / "out")
    result = postprocess_summary(summary, tmp_path / "out", allow_excluded=True)
    assert "included (4/5)" in labels
    assert "excluded: 3" in labels
    assert "Excluded folders: 3" in result.report_path.read_text()
