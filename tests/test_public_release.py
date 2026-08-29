import os
import re
import tarfile
import zipfile
from pathlib import Path


def _has_forbidden_release_content(content: bytes) -> bool:
    forbidden = (b"/" + b"home/", b"/opt/" + b"modules", b".hpc" + b".local")
    return any(value.lower() in content.lower() for value in forbidden)


def test_default_pbs_template_has_no_site_specific_node_or_module() -> None:
    from vaspsolkit.pbs import PbsSpec, render_pbs_script

    text = render_pbs_script(PbsSpec(job_name="example", workdir=Path("/tmp/example")))

    assert "#PBS -l nodes=1:ppn=48" in text
    assert "#PBS -q" not in text
    assert "node17.example.invalid" not in text
    assert "/opt/" + "modules" not in text
    assert "module load" not in text


def test_configured_pbs_module_has_no_site_specific_init_script() -> None:
    from vaspsolkit.pbs import PbsSpec, render_pbs_script

    text = render_pbs_script(
        PbsSpec(job_name="example", workdir=Path("/tmp/example"), module="vasp/6")
    )

    assert "module load vasp/6" in text
    assert "/opt/" + "modules" not in text


def test_default_configuration_uses_slurm_local_server_profile() -> None:
    from vaspsolkit.config import KitConfig

    config = KitConfig()

    assert config.scheduler.kind == "slurm"
    assert config.scheduler.partition == "compute"
    assert config.scheduler.nodes == []
    assert config.scheduler.module_init == ""
    assert config.scheduler.modules == []


def test_readme_documents_numbered_menu_and_confirmation_contract() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")

    assert "vaspsolkit menu" in readme
    assert "02" in readme and "推荐下一步" in readme
    assert "SUBMIT" in readme
    assert "CANCEL" in readme
    assert "vaspsolkit ui" in readme and "已归档" in readme
    assert "只查询当前 Case" in readme
    assert "提交资源配置" in readme
    assert "使用以上配置" in readme
    assert "自动分配节点" in readme
    assert "指定节点" in readme
    assert "是否保存为当前 Case 默认配置" in readme
    assert "未输入 `SUBMIT`" in readme
    assert "NO_COLOR=1 vaspsolkit" in readme
    assert "Sarasa Mono SC" in readme
    assert "80 列" in readme
    assert "不会自动清屏" in readme
    assert "非交互" in readme and "ANSI" in readme
    assert "SHE reference [4.70 eV]" in readme
    assert "13)" in readme
    assert "configure-reference" in readme
    assert "she_reference_source" in readme
    assert "60 → 61 → 62" in readme


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
