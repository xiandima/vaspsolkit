import os
import re
import tarfile
import zipfile
from pathlib import Path


def _has_forbidden_release_content(content: bytes) -> bool:
    forbidden = (b"/" + b"home/", b"/opt/" + b"modules", b".hpc" + b".local")
    return any(value.lower() in content.lower() for value in forbidden)


def test_default_slurm_template_is_portable() -> None:
    text = Path("templates/slurm.vasp.sh").read_text(encoding="utf-8")
    assert "#SBATCH --partition=compute" in text
    assert "#SBATCH --ntasks=96" in text
    assert "--nodelist" not in text
    assert "/opt/" + "modules" not in text
    assert "vasp_std" in text and "vasp_gam" not in text


def test_default_configuration_uses_slurm_local_server_profile() -> None:
    from vaspsolkit.config import KitConfig

    config = KitConfig()

    assert config.scheduler.kind == "slurm"
    assert config.scheduler.partition == "compute"
    assert config.scheduler.nodes == []
    assert config.scheduler.module_init == ""
    assert config.scheduler.modules == []


def test_readme_documents_slurm_workflow_and_confirmation_contract() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")

    assert "vaspsolkit menu" in readme
    assert "02" in readme and "推荐下一步" in readme
    assert "SUBMIT" in readme
    assert "CANCEL" in readme
    assert "只查询当前 Case" in readme
    assert "自动分配节点" in readme
    assert "指定节点" in readme
    assert "分区 -> 分区内节点" in readme
    assert "compute" in readme and "96" in readme and "72:00:00" in readme
    assert "mpirun -np 96 vasp_std" in readme
    assert "squeue" in readme and "sacct" in readme
    assert "configure-reference" in readme
    assert "默认不复制、不读取 `WAVECAR`" in readme


def test_runtime_has_no_builtin_pbs_implementation() -> None:
    allowed = {Path("vaspsolkit/config.py")}
    forbidden = re.compile(r"PBSScheduler|PBSNodeInfo|qsub|qstat|qdel|pbsnodes|#PBS|vasp\.pbs")
    violations = []
    for path in Path("vaspsolkit").rglob("*.py"):
        if path in allowed:
            continue
        if forbidden.search(path.read_text(encoding="utf-8")):
            violations.append(str(path))
    assert violations == []
    assert not Path("vaspsolkit/pbs.py").exists()
    assert not Path("templates/pbs.vasp.pbs").exists()


def test_public_tree_has_no_private_paths_or_site_hostnames() -> None:
    forbidden_literals = (
        "/" + "home/",
        "/opt/" + "modules",
    )
    site_node = re.compile(r"node\d+\.hpc\.local", re.IGNORECASE)
    text_suffixes = {
        ".cff", ".css", ".json", ".md", ".py", ".sh", ".svg", ".toml", ".txt", ".yml", ".yaml"
    }
    violations = []
    for path in Path(".").rglob("*"):
        if not path.is_file() or path.suffix.lower() not in text_suffixes:
            continue
        if any(part in {".git", ".pytest_cache", "__pycache__"} for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if any(value in text for value in forbidden_literals) or site_node.search(text):
            violations.append(str(path))
    assert violations == []


def test_ci_runs_release_build_and_public_leak_gate() -> None:
    workflow = Path(".github/workflows/tests.yml").read_text(encoding="utf-8")

    assert "tools/build_release.py" in workflow
    assert "test_public_release.py" in workflow
    assert "PUBLIC_RELEASE_ARTIFACTS" in workflow


def test_built_release_artifacts_have_no_private_paths() -> None:
    artifact_root = os.environ.get("PUBLIC_RELEASE_ARTIFACTS", "").strip()
    if not artifact_root:
        return

    artifacts = tuple(Path(artifact_root).glob("vaspsolkit-*"))
    assert artifacts
    for artifact in artifacts:
        if artifact.suffix == ".whl":
            with zipfile.ZipFile(artifact) as archive:
                payloads = (archive.read(name) for name in archive.namelist())
                assert not any(_has_forbidden_release_content(payload) for payload in payloads)
        elif artifact.name.endswith(".tar.gz"):
            with tarfile.open(artifact, "r:gz") as archive:
                payloads = (
                    extracted.read()
                    for member in archive.getmembers()
                    if member.isfile()
                    for extracted in [archive.extractfile(member)]
                    if extracted is not None
                )
                assert not any(_has_forbidden_release_content(payload) for payload in payloads)


def test_release_version_is_consistent_across_public_metadata() -> None:
    project = Path("pyproject.toml").read_text(encoding="utf-8")
    package = Path("vaspsolkit/__init__.py").read_text(encoding="utf-8")
    citation = Path("CITATION.cff").read_text(encoding="utf-8")

    project_version = re.search(r'^version = "([^"]+)"$', project, re.MULTILINE)
    package_version = re.search(r'^__version__ = "([^"]+)"$', package, re.MULTILINE)
    citation_version = re.search(r'^version: "([^"]+)"$', citation, re.MULTILINE)

    assert project_version is not None
    assert package_version is not None
    assert citation_version is not None
    assert project_version.group(1) == package_version.group(1) == citation_version.group(1)
