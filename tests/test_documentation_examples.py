from pathlib import Path
import re
import shlex

from vaspsolkit.cli import _build_parser


ROOT = Path(__file__).resolve().parents[1]
GUIDES = (ROOT / "README.md", ROOT / "docs/CLUSTER_CONFIGURATION.md")


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
        if args == ["--help"]:
            continue
        _build_parser().parse_args(args)


def test_postprocess_example_preserves_detailed_audit_directory():
    for args in _cli_examples():
        if args and args[0] == "postprocess":
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
