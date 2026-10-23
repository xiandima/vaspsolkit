"""Input-boundary regressions using metadata fixtures and mocked subprocesses."""

import argparse
import subprocess
from pathlib import Path

import pytest

from vaspsolkit import input_completion
from vaspsolkit.inputs import poscar_elements, potcar_elements, validate_potcar_order


def _write_case(root, labels="H"):
    root.mkdir()
    counts = " ".join("1" for _ in labels.split())
    coordinates = "0 0 0\n" * len(labels.split())
    (root / "POSCAR").write_text(
        f"synthetic\n1\n3 0 0\n0 3 0\n0 0 12\n{labels}\n{counts}\nDirect\n{coordinates}"
    )
    (root / "INCAR").write_text("ENCUT=450\nKSPACING=0.25\n")


def _args(root, **overrides):
    values = dict(
        workdir=str(root), yes=True, dry_run=False, incar_template=None,
        backend="native", mesh=None, centering="gamma", kpr=None,
        potcar_root=None, functional="PBE", potential=[],
        settings=str(root.parent / "settings.json"), save_settings=False,
    )
    values.update(overrides)
    return argparse.Namespace(**values)


@pytest.mark.parametrize("backend", ["native", "vaspkit"])
@pytest.mark.parametrize("label", [
    "absolute", "../outside", "../../outside", "C/../../../outside",
    r"C\..\outside", "C;command", ".", "..", "C\x00",
])
def test_unsafe_poscar_labels_fail_before_staging(tmp_path, monkeypatch, backend, label):
    if label == "absolute":
        label = str(tmp_path / "outside")
    root = tmp_path / "case"
    _write_case(root, label)
    before = {path.name: path.read_bytes() for path in root.iterdir()}

    def forbidden(*args, **kwargs):
        pytest.fail("unsafe POSCAR reached staging or subprocess execution")

    monkeypatch.setattr(input_completion.tempfile, "TemporaryDirectory", forbidden)
    monkeypatch.setattr(input_completion.subprocess, "run", forbidden)
    args = _args(root, backend=backend, potential=[f"{label}=C"])
    with pytest.raises(ValueError, match="POSCAR.*element.*label"):
        input_completion.complete_inputs(args, output=lambda _: None)

    assert {path.name: path.read_bytes() for path in root.iterdir()} == before
    assert set(tmp_path.iterdir()) == {root}


@pytest.mark.parametrize("labels", ["H C Ni", "Cl Cs Og", "H.5 H1.25 Fe_pv"])
def test_poscar_parser_preserves_safe_chemical_labels(tmp_path, labels):
    root = tmp_path / "case"
    _write_case(root, labels)
    assert poscar_elements(root / "POSCAR") == labels.split()


@pytest.mark.parametrize("variant, element", [
    ("H.25", "H"), ("H.5", "H"), ("H.75", "H"),
    ("H1.25", "H"), ("H1.5", "H"), ("H1.75", "H"),
    ("Ni_pv", "Ni"), ("Li_sv_GW", "Li"), ("C_GW_new", "C"),
    ("Gd_3", "Gd"), ("O_s", "O"), ("Cl", "Cl"),
])
@pytest.mark.parametrize("family", ["PAW_PBE", "PAW", "PAW_GGA", "US"])
def test_titel_parser_recognizes_real_variant_suffixes(tmp_path, family, variant, element):
    potcar = tmp_path / "POTCAR"
    potcar.write_text(f"TITEL = {family} {variant} TEST\nENMAX = 400\n")
    assert potcar_elements(potcar) == [element]


def test_fractional_variants_still_enforce_element_order(tmp_path):
    root = tmp_path / "case"
    _write_case(root, "C H Ni")
    potcar = root / "POTCAR"
    potcar.write_text(
        "TITEL = PAW_PBE C_h TEST\n"
        "TITEL = PAW_PBE H.5 TEST\n"
        "TITEL = PAW_PBE Ni_pv TEST\n"
    )
    assert validate_potcar_order(root / "POSCAR", potcar) == ["C", "H", "Ni"]
    potcar.write_text(potcar.read_text().replace("C_h", "Si"))
    with pytest.raises(ValueError, match="element order"):
        validate_potcar_order(root / "POSCAR", potcar)


def test_variant_labels_in_poscar_compare_by_element(tmp_path):
    root = tmp_path / "case"
    _write_case(root, "H.5 Fe_pv")
    potcar = root / "POTCAR"
    potcar.write_text("TITEL = PAW_PBE H.5 TEST\nTITEL = PAW_PBE Fe_pv TEST\n")
    assert validate_potcar_order(root / "POSCAR", potcar) == ["H.5", "Fe_pv"]


@pytest.mark.parametrize("variant", ["H.5", "H1.25"])
def test_native_completion_copies_fractional_hydrogen_with_native_sampling(tmp_path, monkeypatch, variant):
    root = tmp_path / "case"
    _write_case(root)
    library = tmp_path / "library"
    source = library / variant / "POTCAR"
    source.parent.mkdir(parents=True)
    data = f"TITEL = PAW_PBE {variant} TEST\nENMAX = 400\n".encode()
    source.write_bytes(data)
    original_incar = (root / "INCAR").read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail("native completion must not execute an external tool")

    monkeypatch.setattr(input_completion.subprocess, "run", forbidden)
    args = _args(root, potcar_root=str(library), potential=[f"H={variant}"])
    assert input_completion.complete_inputs(args, output=lambda _: None) == 0
    assert (root / "POTCAR").read_bytes() == source.read_bytes() == data
    assert (root / "INCAR").read_bytes() == original_incar
    assert not (root / "KPOINTS").exists()


@pytest.mark.parametrize("entry", ["bin", "bin with spaces", ".", ""])
@pytest.mark.parametrize("symlink", [False, True])
def test_helper_resolves_relative_path_before_changing_cwd(tmp_path, monkeypatch, entry, symlink):
    bindir = tmp_path / entry
    bindir.mkdir(exist_ok=True)
    executable = bindir / "vaspkit"
    target = tmp_path / "helper-target" if symlink else executable
    target.write_text("# synthetic executable fixture; never executed\n")
    target.chmod(0o700)
    if symlink:
        executable.symlink_to(target)
    stage = tmp_path / "stage"
    stage.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", entry or ":missing-bin")
    calls = []

    def helper(argv, **kwargs):
        calls.append(argv)
        assert Path(argv[0]).is_absolute()
        assert Path(argv[0]) == executable
        assert Path(argv[0]).is_file()
        assert kwargs["cwd"] == stage
        if "-help" not in argv:
            (stage / "KPOINTS").write_text("synthetic\n0\nGamma\n2 2 1\n")
        return subprocess.CompletedProcess(argv, 0, "VASPKIT 1.5.0", "")

    monkeypatch.setattr(input_completion.subprocess, "run", helper)
    report = input_completion._vaspkit(
        stage, {"mesh": None, "kpr": 0.04, "centering": "gamma"}, ["H"], {}, ["KPOINTS"]
    )
    assert report["executable"] == str(executable)
    assert len(calls) == 2
