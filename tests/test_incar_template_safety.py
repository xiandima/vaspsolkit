from pathlib import Path

import pytest

from vaspsolkit.incar import replace_or_append
from vaspsolkit.inputs import apply_incar_profile, plan_neutral_vaspsol_update


def test_edit_preserves_other_assignments_and_comment_on_same_line():
    text = "ICHARG = 2; ALGO = Normal; ISPIN = 2 # user settings\n"
    assert replace_or_append(text, "ICHARG", "1") == text.replace("ICHARG = 2", "ICHARG = 1")


def test_edit_targets_active_assignment_not_commented_example():
    text = "# ICHARG = 2; ALGO = Fast\nicharg = 2 ! active\n"
    assert replace_or_append(text, "ICHARG", "1") == (
        "# ICHARG = 2; ALGO = Fast\nICHARG = 1 ! active\n"
    )


def test_edit_updates_all_active_duplicates_without_touching_other_tags():
    text = "ICHARG=2; ALGO=Normal\nicharg=0; ISPIN=2\n"
    result = replace_or_append(text, "ICHARG", "1")
    assert result == "ICHARG = 1; ALGO=Normal\nICHARG = 1; ISPIN=2\n"


def test_edit_keeps_commented_template_and_appends_active_setting():
    assert replace_or_append("# ICHARG = 2\n", "ICHARG", "1") == (
        "# ICHARG = 2\nICHARG = 1\n"
    )


def test_edit_replaces_entire_continued_value_only():
    text = "MAGMOM = 2*1.0 " + "\\" + "\n 2*0.0; ISPIN=2\n"
    assert replace_or_append(text, "MAGMOM", "4*1.0") == "MAGMOM = 4*1.0; ISPIN=2\n"


def test_quoted_block_is_not_treated_as_top_level_assignments():
    text = 'WANNIER90_WIN = "\nICHARG = 99; # literal\n"\nICHARG=2\n'
    assert replace_or_append(text, "ICHARG", "1") == text.replace("ICHARG=2", "ICHARG = 1")


def test_profile_keeps_semicolon_separated_scientific_settings():
    result = apply_incar_profile(
        "ENCUT=450; ALGO=Normal; ISPIN=2; MAGMOM=2*1.0\n", "vaspsol-neutral-relax"
    )
    assert "ALGO=Normal" in result
    assert "ALGO = Fast" not in result
    assert "ISPIN=2; MAGMOM=2*1.0" in result


@pytest.mark.parametrize("value", ["80", "80.0", "8.0E1", "8.0D1"])
def test_neutral_plan_accepts_equivalent_numeric_settings_and_semicolons(value):
    text = f"IBRION=2; NSW=200\nEB_K={value}\n"
    plan = plan_neutral_vaspsol_update(text)
    assert not plan.conflicts
    assert f"EB_K={value}" in plan.candidate


def test_neutral_plan_detects_duplicate_tags_on_same_line():
    plan = plan_neutral_vaspsol_update("IBRION=2; NSW=200\nENCUT=400; encut=500\n")
    assert plan.duplicates == ("ENCUT",)


def test_neutral_readback_and_charge_patch_share_assignment_rules(tmp_path: Path):
    from vaspsolkit.orchestrator import _is_geometry_optimization, _read_incar_tags
    from vaspsolkit.workflow import _active_incar_nelect, _write_charge_incar

    path = tmp_path / "INCAR"
    path.write_text("IBRION=2; NSW=200\nISPIN=2; nelect=10\n# NELECT=99\n")
    assert _is_geometry_optimization(_read_incar_tags(path))
    assert _active_incar_nelect(path) == 10
    _write_charge_incar(path, 10.5)
    assert _active_incar_nelect(path) == 10.5
    assert "ISPIN=2" in path.read_text()


def test_nelect_check_ignores_commented_example_and_reads_lowercase(tmp_path: Path):
    from vaspsolkit.convergence import _nelect_diagnostics

    path = tmp_path / "INCAR"
    path.write_text("ISPIN=2; nelect=10\n# NELECT=99\n")
    assert _nelect_diagnostics(path, "NELECT = 10") == []
    assert _nelect_diagnostics(path, "NELECT = 11") == ["nelect_mismatch"]


def test_critical_parameter_preview_shows_all_assignments():
    from vaspsolkit.cli import _print_critical_incar

    output = []
    _print_critical_incar("ENCUT=450; ISPIN=2; ALGO=Normal\n", output.append)
    assert "  ISPIN = 2" in output
    assert "  ALGO = Normal" in output


def test_nested_blocks_are_rejected_instead_of_patching_wrong_scope():
    with pytest.raises(ValueError, match="nested"):
        replace_or_append("IMAGES {\nICHARG=2\n}\n", "ICHARG", "1")
