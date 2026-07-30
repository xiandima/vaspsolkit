from __future__ import annotations

import io
import tarfile
from pathlib import Path


def _write_tar(path: Path, entries: list[tuple[str, bytes, int]], mtime: int) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name, content, mode in entries:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = mode
            info.uid = 123
            info.gid = 456
            info.uname = "builder"
            info.gname = "builders"
            info.mtime = mtime
            archive.addfile(info, io.BytesIO(content))


def test_normalized_sdist_is_reproducible_and_scrubs_host_metadata(tmp_path: Path) -> None:
    from tools.build_release import normalize_sdist

    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"
    entries = [
        ("pkg-1.0/docs/UI_GUIDE_zh.md", b"guide\n", 0o600),
        ("pkg-1.0/vaspsolkit/workbench/styles/workbench.tcss", b"css\n", 0o664),
    ]
    _write_tar(first, entries, 10)
    _write_tar(second, list(reversed(entries)), 999)

    normalize_sdist(first, source_date_epoch=123456789)
    normalize_sdist(second, source_date_epoch=123456789)

    assert first.read_bytes() == second.read_bytes()
    with tarfile.open(first, "r:gz") as archive:
        members = archive.getmembers()
    assert [member.name for member in members] == sorted(member.name for member in members)
    assert all(member.uid == member.gid == 0 for member in members)
    assert all(member.uname == member.gname == "root" for member in members)
    assert all(member.mtime == 123456789 for member in members)
    assert all(member.mode == 0o644 for member in members)


def test_release_manifest_excludes_archived_ui() -> None:
    manifest = Path("MANIFEST.in").read_text(encoding="utf-8")

    assert "prune archive" in manifest
    assert "docs/UI_GUIDE_zh.md" not in manifest
    assert "vaspsolkit/workbench/styles" not in manifest
    assert "tools/build_release.py" in manifest


def test_formal_sources_include_menu_operations_and_exclude_ui_runtime() -> None:
    root = Path(".")
    project = (root / "pyproject.toml").read_text(encoding="utf-8")

    assert (root / "vaspsolkit" / "interactive_menu.py").is_file()
    assert (root / "vaspsolkit" / "menu_actions.py").is_file()
    assert (root / "vaspsolkit" / "operations" / "controller.py").is_file()
    assert not (root / "vaspsolkit" / "workbench").exists()
    assert not (root / "vaspsolkit" / "textual_ui.py").exists()
    assert not (root / "vaspsolkit" / "tui.py").exists()
    assert '"textual' not in project
