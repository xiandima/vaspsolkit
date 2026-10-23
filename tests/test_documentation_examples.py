from pathlib import Path
import re
import shlex

import pytest

from vaspsolkit.cli import _build_parser


ROOT = Path(__file__).resolve().parents[1]
GUIDES = (
    ROOT / "README.md",
    ROOT / "docs/USER_GUIDE.md",
    ROOT / "docs/INSTALLATION.md",
    ROOT / "docs/CLUSTER_CONFIGURATION.md",
    ROOT / "docs/index.md",
    ROOT / "docs/quickstart.md",
    ROOT / "docs/commands.md",
    ROOT / "docs/results.md",
)


def _cli_examples():
    for path in GUIDES:
        for block in re.findall(r"```bash\n(.*?)```", path.read_text(encoding="utf-8"), re.DOTALL):
            for line in block.replace("\\\n", " ").splitlines():
                args = shlex.split(line, comments=True)
                if args and args[0] == "vaspsolkit":
                    yield args[1:]


def test_documented_cli_examples_use_real_parser_options():
    examples = list(_cli_examples())
    assert len(examples) >= 20
    for args in examples:
        if "--help" in args:
            with pytest.raises(SystemExit) as result:
                _build_parser().parse_args(args)
            assert result.value.code == 0
            continue
        _build_parser().parse_args(args)


def test_postprocess_example_preserves_detailed_audit_directory():
    for args in _cli_examples():
        if args and args[0] == "postprocess" and "--help" not in args:
            parsed = _build_parser().parse_args(args)
            assert Path(parsed.output) != Path(parsed.summary).parent


def test_user_documentation_local_links_exist():
    paths = [ROOT / "README.md", ROOT / "CONTRIBUTING.md", *ROOT.joinpath("docs").rglob("*.md")]
    for path in paths:
        for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", path.read_text(encoding="utf-8")):
            if "://" in target or target.startswith("#"):
                continue
            destination = target.split("#", 1)[0]
            assert (path.parent / destination).is_file(), (path.relative_to(ROOT), target)


def test_readme_menu_shortcuts_match_the_live_catalogue():
    from vaspsolkit.menu_actions import action_by_code

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    rows = re.findall(r"^\| `(\d{2})` \| ([^|]+) \|", readme, re.MULTILINE)
    assert {code for code, _ in rows} >= {"02", "03", "14", "61", "62", "00"}
    for code, title in rows:
        assert action_by_code(code).title_zh == title.strip()


def test_readme_links_to_advanced_guides_without_requiring_developer_setup():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for guide in ("docs/USER_GUIDE.md", "docs/INSTALLATION.md", "docs/CLUSTER_CONFIGURATION.md"):
        assert f"]({guide})" in readme
    assert "[dev]" not in readme
    assert "pytest" not in readme
    assert "tools/build_release.py" not in readme
