#!/usr/bin/env python3
"""Build reproducible VASPsolKit wheel and sdist artifacts from git HEAD."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Iterable


def normalize_sdist(path: Path, source_date_epoch: int) -> None:
    """Rewrite one gzip tarball with stable ordering and portable metadata."""
    path = Path(path)
    normalized = io.BytesIO()
    with tarfile.open(path, "r:gz") as source, tarfile.open(
        fileobj=normalized, mode="w", format=tarfile.PAX_FORMAT
    ) as target:
        for member in sorted(source.getmembers(), key=lambda item: item.name):
            info = tarfile.TarInfo(member.name)
            info.type = member.type
            info.linkname = member.linkname
            info.uid = 0
            info.gid = 0
            info.uname = "root"
            info.gname = "root"
            info.mtime = int(source_date_epoch)
            info.pax_headers = {}
            if member.isdir():
                info.mode = 0o755
                info.size = 0
                target.addfile(info)
            elif member.isfile():
                info.mode = 0o755 if member.mode & 0o111 else 0o644
                extracted = source.extractfile(member)
                if extracted is None:
                    raise RuntimeError(f"cannot read sdist member: {member.name}")
                content = extracted.read()
                info.size = len(content)
                target.addfile(info, io.BytesIO(content))
            elif member.issym() or member.islnk():
                info.mode = 0o777
                info.size = 0
                target.addfile(info)
            else:
                raise RuntimeError(
                    f"unsupported sdist member type for {member.name}: {member.type!r}"
                )
    with path.open("wb") as destination, gzip.GzipFile(
        filename="", mode="wb", compresslevel=9, fileobj=destination, mtime=0
    ) as compressed:
        compressed.write(normalized.getvalue())


def _safe_extract_git_archive(archive_bytes: bytes, destination: Path) -> None:
    destination = destination.resolve()
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:") as archive:
        for member in archive.getmembers():
            target = (destination / member.name).resolve()
            try:
                target.relative_to(destination)
            except ValueError as exc:
                raise RuntimeError(f"git archive member escapes root: {member.name}") from exc
        archive.extractall(destination)


def _run(command: Iterable[str], cwd: Path, env: dict[str, str]) -> None:
    subprocess.run(tuple(command), cwd=cwd, env=env, check=True)


def _has_pypa_build() -> bool:
    try:
        return importlib.util.find_spec("build.__main__") is not None
    except (ImportError, ModuleNotFoundError):
        return False


def _build_raw(source: Path, output: Path, env: dict[str, str]) -> None:
    if _has_pypa_build():
        _run(
            (
                sys.executable,
                "-m",
                "build",
                "--no-isolation",
                "--wheel",
                "--sdist",
                "--outdir",
                str(output),
            ),
            source,
            env,
        )
        return
    _run(
        (
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(output),
            ".",
        ),
        source,
        env,
    )
    code = (
        "import setuptools.build_meta as backend; "
        f"backend.build_sdist({str(output)!r})"
    )
    _run((sys.executable, "-c", code), source, env)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_release(repo: Path, output: Path, source_date_epoch: int) -> tuple[Path, ...]:
    """Build from a clean git archive and return copied artifact paths."""
    repo = Path(repo).resolve()
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(
        ("git", "archive", "--format=tar", "HEAD"),
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
    ).stdout
    env = os.environ.copy()
    env.update(
        {
            "SOURCE_DATE_EPOCH": str(int(source_date_epoch)),
            "PYTHONHASHSEED": "0",
            "TZ": "UTC",
            "LC_ALL": "C.UTF-8",
        }
    )
    with tempfile.TemporaryDirectory(prefix="vaspsolkit-release-") as temporary:
        source = Path(temporary) / "source"
        raw = Path(temporary) / "raw"
        source.mkdir()
        raw.mkdir()
        _safe_extract_git_archive(archive, source)
        _build_raw(source, raw, env)
        sdists = tuple(raw.glob("*.tar.gz"))
        wheels = tuple(raw.glob("*.whl"))
        if len(sdists) != 1 or len(wheels) != 1:
            raise RuntimeError("release build must produce exactly one wheel and one sdist")
        normalize_sdist(sdists[0], source_date_epoch)
        copied = []
        for artifact in sorted((*wheels, *sdists), key=lambda item: item.name):
            destination = output / artifact.name
            shutil.copyfile(artifact, destination)
            copied.append(destination)
    return tuple(copied)


def _git_epoch(repo: Path) -> int:
    result = subprocess.run(
        ("git", "show", "-s", "--format=%ct", "HEAD"),
        cwd=repo,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    return int(result.stdout.strip())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build reproducible wheel and sdist artifacts from git HEAD"
    )
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--outdir", type=Path, required=True)
    parser.add_argument("--source-date-epoch", type=int, default=None)
    args = parser.parse_args(argv)
    epoch = args.source_date_epoch
    if epoch is None:
        epoch = _git_epoch(args.repo)
    for artifact in build_release(args.repo, args.outdir, epoch):
        print(f"{_sha256(artifact)}  {artifact}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
