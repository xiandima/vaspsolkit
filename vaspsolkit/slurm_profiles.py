"""Parse, validate, and synchronize portable SLURM submission profiles."""
from __future__ import annotations

from dataclasses import dataclass, replace
import difflib
from pathlib import PurePath
import re
import shlex
from typing import Dict, Optional, Tuple


_NODE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_PARTITION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SCRIPT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_WALLTIME = re.compile(r"^\d+:[0-5]\d:[0-5]\d$")
_MODULE_PATH = re.compile(r"^/?[A-Za-z0-9._+/-]+$")
_MODULE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+/@%-]*$")
_SHELL_METACHARACTERS = re.compile(r"[\n\r;&|<>`$(){}\[\]*?!]")

_DIRECTIVES = {
    "-J": "job-name",
    "--job-name": "job-name",
    "-p": "partition",
    "--partition": "partition",
    "-N": "nodes",
    "--nodes": "nodes",
    "-n": "ntasks",
    "--ntasks": "ntasks",
    "--ntasks-per-node": "ntasks-per-node",
    "-t": "time",
    "--time": "time",
    "-w": "nodelist",
    "--nodelist": "nodelist",
    "-o": "output",
    "--output": "output",
}
_SHORT_DIRECTIVES = ("-J", "-p", "-N", "-n", "-t", "-w", "-o")
_RESOURCE_OPTIONS = {
    "partition": ("-p", "--partition"),
    "nodes": ("-N", "--nodes"),
    "ntasks": ("-n", "--ntasks"),
    "ntasks-per-node": ("--ntasks-per-node",),
    "time": ("-t", "--time"),
    "nodelist": ("-w", "--nodelist"),
}
_VASP_EXECUTABLES = {"vasp", "vasp_std", "vasp_gam", "vasp_ncl"}
_LAUNCH_OPTIONS_WITHOUT_VALUES = {
    "--allow-run-as-root",
    "--display-map",
    "--exclusive",
    "--oversubscribe",
    "--report-bindings",
    "--tag-output",
    "--timestamp-output",
    "--verbose",
    "-v",
}
_LAUNCH_OPTIONS_WITH_TWO_VALUES = {"--mca", "-mca"}


def _is_positive_int(value: object) -> bool:
    return type(value) is int and value > 0


def _validate_safe_text(value: str, label: str, *, allow_empty: bool = False) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    if not value and not allow_empty:
        raise ValueError(f"{label} must be nonempty")
    if _SHELL_METACHARACTERS.search(value):
        raise ValueError(f"{label} contains unsafe shell metacharacters")


def _validate_nodes(nodes: Tuple[str, ...], node_count: int, label: str) -> None:
    if not isinstance(nodes, tuple):
        raise TypeError(f"{label} must be a tuple")
    if any(not isinstance(node, str) or _NODE_NAME.fullmatch(node) is None for node in nodes):
        raise ValueError(f"invalid SLURM {label.rstrip('s')} name")
    if len(set(nodes)) != len(nodes):
        raise ValueError(f"SLURM {label} must not contain duplicates")
    if nodes and len(nodes) != node_count:
        raise ValueError(f"SLURM {label} must match node_count")


@dataclass(frozen=True)
class SlurmProfile:
    name: str
    partition: str
    node_count: int
    tasks: int
    tasks_per_node: int
    walltime: str
    script: str
    nodes: tuple[str, ...] = ()
    launcher: str = "mpirun"
    executable: str = "vasp_std"
    module_init: str = ""
    modules: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_safe_text(self.name, "name")
        if _PARTITION.fullmatch(self.partition) is None:
            raise ValueError("partition must be a nonempty safe SLURM partition")
        if (
            _SCRIPT_NAME.fullmatch(self.script) is None
            or PurePath(self.script).name != self.script
            or self.script in {".", ".."}
        ):
            raise ValueError("script must be a path-safe filename")
        for label, value in (
            ("node_count", self.node_count),
            ("tasks", self.tasks),
            ("tasks_per_node", self.tasks_per_node),
        ):
            if not _is_positive_int(value):
                raise ValueError(f"{label} must be positive")
        if self.tasks > self.node_count * self.tasks_per_node:
            raise ValueError("tasks exceed SLURM node capacity")
        _validate_nodes(self.nodes, self.node_count, "nodes")
        if not isinstance(self.walltime, str) or _WALLTIME.fullmatch(self.walltime) is None:
            raise ValueError("walltime must use H+:MM:SS")
        if self.launcher != "mpirun":
            raise ValueError("launcher must be mpirun")
        if self.executable != "vasp_std":
            raise ValueError("executable must be vasp_std")
        if self.module_init:
            if _MODULE_PATH.fullmatch(self.module_init) is None or ".." in PurePath(
                self.module_init
            ).parts:
                raise ValueError("module_init must be a safe source path")
        if not isinstance(self.modules, tuple):
            raise TypeError("modules must be a tuple")
        if any(
            not isinstance(module, str) or _MODULE_NAME.fullmatch(module) is None
            for module in self.modules
        ):
            raise ValueError("module names must be safe and nonempty")


@dataclass(frozen=True)
class CaseResourceOverride:
    partition: Optional[str] = None
    nodes: Optional[tuple[str, ...]] = None
    node_count: Optional[int] = None
    tasks: Optional[int] = None
    tasks_per_node: Optional[int] = None
    walltime: Optional[str] = None

    def __post_init__(self) -> None:
        if self.partition is not None and _PARTITION.fullmatch(self.partition) is None:
            raise ValueError("override partition must be a nonempty safe SLURM partition")
        for label, value in (
            ("node_count", self.node_count),
            ("tasks", self.tasks),
            ("tasks_per_node", self.tasks_per_node),
        ):
            if value is not None and not _is_positive_int(value):
                raise ValueError(f"override {label} must be positive")
        if self.nodes is not None:
            count = self.node_count if self.node_count is not None else len(self.nodes)
            _validate_nodes(self.nodes, count, "nodes")
        if self.walltime is not None and _WALLTIME.fullmatch(self.walltime) is None:
            raise ValueError("override walltime must use H+:MM:SS")


def import_slurm_profile(
    script_text: str,
    name: str,
    script_name: str = "vasp.slurm",
) -> SlurmProfile:
    """Parse supported SBATCH and environment lines without evaluating the script."""
    if not isinstance(script_text, str):
        raise TypeError("script_text must be a string")
    directives: Dict[str, object] = {}
    module_init = ""
    modules = []

    for raw_line in script_text.splitlines():
        directive_match = re.match(r"^\s*#SBATCH(?:\s+)(.*)$", raw_line)
        if directive_match is not None:
            for key, value in _parse_directive_tokens(directive_match.group(1)):
                parsed = _parse_directive_value(key, value)
                previous = directives.get(key)
                if previous is not None and previous != parsed:
                    raise ValueError(f"conflicting SLURM {key} directive")
                directives[key] = parsed
            continue

        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            tokens = shlex.split(stripped, comments=True, posix=True)
        except ValueError as exc:
            raise ValueError("invalid shell command syntax") from exc
        if not tokens:
            continue
        if tokens[0] in {"source", "."}:
            if len(tokens) != 2 or _MODULE_PATH.fullmatch(tokens[1]) is None:
                raise ValueError("source command must contain one safe path")
            if module_init and module_init != tokens[1]:
                raise ValueError("conflicting source commands")
            module_init = tokens[1]
        elif len(tokens) >= 2 and tokens[:2] == ["module", "load"]:
            if len(tokens) == 2 or any(
                _MODULE_NAME.fullmatch(module) is None for module in tokens[2:]
            ):
                raise ValueError("module load contains an unsafe module name")
            modules.extend(tokens[2:])

    return SlurmProfile(
        name=name,
        partition=str(directives.get("partition", "compute")),
        node_count=int(directives.get("nodes", 1)),
        tasks=int(directives.get("ntasks", 96)),
        tasks_per_node=int(directives.get("ntasks-per-node", 96)),
        walltime=str(directives.get("time", "72:00:00")),
        script=script_name,
        nodes=directives.get("nodelist", ()),  # type: ignore[arg-type]
        launcher="mpirun",
        executable="vasp_std",
        module_init=module_init,
        modules=tuple(modules),
    )


def _parse_directive_tokens(text: str) -> list[tuple[str, str]]:
    try:
        tokens = shlex.split(text, comments=True, posix=True)
    except ValueError as exc:
        raise ValueError("invalid SBATCH directive syntax") from exc
    parsed = []
    index = 0
    while index < len(tokens):
        directive = _recognized_directive_at(tokens, index)
        if directive is None:
            index += 1
            continue
        key, value, index = directive
        parsed.append((key, value))
    return parsed


def _parse_directive_value(key: str, value: str) -> object:
    if _SHELL_METACHARACTERS.search(value):
        raise ValueError(f"SLURM {key} directive contains shell metacharacters")
    if key == "partition":
        if _PARTITION.fullmatch(value) is None:
            raise ValueError("invalid SLURM partition directive")
        return value
    if key in {"nodes", "ntasks", "ntasks-per-node"}:
        if not value.isdecimal() or int(value) <= 0:
            raise ValueError(f"invalid SLURM {key} directive")
        return int(value)
    if key == "time":
        if _WALLTIME.fullmatch(value) is None:
            raise ValueError("invalid SLURM time directive; expected H+:MM:SS")
        return value
    if key == "nodelist":
        nodes = tuple(value.split(","))
        if any(_NODE_NAME.fullmatch(node) is None for node in nodes):
            raise ValueError("invalid SLURM nodelist directive")
        if len(set(nodes)) != len(nodes):
            raise ValueError("SLURM nodelist directive contains duplicates")
        return nodes
    _validate_safe_text(value, f"SLURM {key} directive")
    return value


def apply_slurm_override(
    profile: SlurmProfile,
    override: CaseResourceOverride,
) -> SlurmProfile:
    """Return a resolved profile without mutating either frozen input."""
    if not isinstance(profile, SlurmProfile):
        raise TypeError("profile must be SlurmProfile")
    if not isinstance(override, CaseResourceOverride):
        raise TypeError("override must be CaseResourceOverride")
    values = {
        field: getattr(override, field)
        for field in (
            "partition",
            "nodes",
            "node_count",
            "tasks",
            "tasks_per_node",
            "walltime",
        )
        if getattr(override, field) is not None
    }
    return replace(profile, **values)


def rewrite_slurm_resources(script_text: str, profile: SlurmProfile) -> str:
    """Synchronize portable resources and the VASP launch, preserving other text."""
    if not isinstance(script_text, str):
        raise TypeError("script_text must be a string")
    if not isinstance(profile, SlurmProfile):
        raise TypeError("profile must be SlurmProfile")

    newline = "\r\n" if "\r\n" in script_text else "\n"
    normalized = script_text.replace("\r\n", "\n")
    had_final_newline = normalized.endswith("\n")
    lines = normalized.split("\n")
    if had_final_newline:
        lines.pop()

    replacements = {
        "partition": f"#SBATCH --partition={profile.partition}",
        "nodes": f"#SBATCH --nodes={profile.node_count}",
        "ntasks": f"#SBATCH --ntasks={profile.tasks}",
        "ntasks-per-node": f"#SBATCH --ntasks-per-node={profile.tasks_per_node}",
        "time": f"#SBATCH --time={profile.walltime}",
    }
    seen = set()
    rewritten = []
    for line in lines:
        rewritten_line = _rewrite_sbatch_line(line, replacements, seen)
        if rewritten_line is not None:
            rewritten.append(rewritten_line)

    missing = [
        replacements[key]
        for key in ("partition", "nodes", "ntasks", "ntasks-per-node", "time")
        if key not in seen
    ]
    if profile.nodes:
        missing.append(f"#SBATCH --nodelist={','.join(profile.nodes)}")
    insertion = _directive_insertion_index(rewritten)
    rewritten[insertion:insertion] = missing

    launch = f"mpirun -np ${{SLURM_NTASKS:-{profile.tasks}}} vasp_std > vasp.log 2>&1"
    for index, line in enumerate(rewritten):
        if _is_vasp_launch(line):
            rewritten[index] = launch
            break
    else:
        rewritten.append(launch)

    result = "\n".join(rewritten)
    if had_final_newline:
        result += "\n"
    return result.replace("\n", newline)


def _rewrite_sbatch_line(
    line: str,
    replacements: Dict[str, str],
    seen: set[str],
) -> Optional[str]:
    match = re.match(r"^(?P<indent>\s*)#SBATCH(?:\s+)(?P<body>.*)$", line)
    if match is None:
        return line
    body, comment = _split_shell_comment(match.group("body"))
    try:
        tokens = shlex.split(body, comments=False, posix=True)
    except ValueError as exc:
        raise ValueError("invalid SBATCH directive syntax") from exc

    rewritten = []
    changed = False
    index = 0
    while index < len(tokens):
        parsed = _recognized_directive_at(tokens, index)
        if parsed is None:
            rewritten.append(tokens[index])
            index += 1
            continue
        key, _value, next_index = parsed
        if key not in _RESOURCE_OPTIONS:
            rewritten.extend(tokens[index:next_index])
        elif key != "nodelist" and key not in seen:
            rewritten.append(replacements[key].removeprefix("#SBATCH "))
            seen.add(key)
            changed = True
        else:
            changed = True
        index = next_index

    if not changed:
        return line
    if not rewritten:
        return comment or None
    directive = f"{match.group('indent')}#SBATCH {shlex.join(rewritten)}"
    return f"{directive} {comment}" if comment else directive


def _split_shell_comment(text: str) -> tuple[str, str]:
    quote = ""
    escaped = False
    for index, character in enumerate(text):
        if escaped:
            escaped = False
            continue
        if character == "\\" and quote != "'":
            escaped = True
            continue
        if quote:
            if character == quote:
                quote = ""
            continue
        if character in {"'", '"'}:
            quote = character
        elif character == "#":
            return text[:index].rstrip(), text[index:].strip()
    return text.rstrip(), ""


def _recognized_directive_at(
    tokens: list[str], index: int
) -> Optional[tuple[str, str, int]]:
    token = tokens[index]
    option = token
    value: Optional[str] = None
    if "=" in token:
        option, value = token.split("=", 1)
    elif token not in _DIRECTIVES:
        for short in _SHORT_DIRECTIVES:
            if token.startswith(short) and len(token) > len(short):
                option, value = short, token[len(short) :]
                break
    key = _DIRECTIVES.get(option)
    if key is None:
        return None
    next_index = index + 1
    if value is None:
        if next_index >= len(tokens):
            raise ValueError(f"SLURM {key} directive requires a value")
        value = tokens[next_index]
        next_index += 1
    if not value:
        raise ValueError(f"SLURM {key} directive requires a value")
    return key, value, next_index


def _directive_insertion_index(lines: list[str]) -> int:
    sbatch_indexes = [
        index for index, line in enumerate(lines) if re.match(r"^\s*#SBATCH\b", line)
    ]
    if sbatch_indexes:
        return sbatch_indexes[-1] + 1
    if lines and lines[0].startswith("#!"):
        return 1
    return 0


def _is_vasp_launch(line: str) -> bool:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return False
    try:
        lexer = shlex.shlex(stripped, posix=True, punctuation_chars="|&;<>")
        lexer.whitespace_split = True
        lexer.commenters = "#"
        tokens = list(lexer)
    except ValueError:
        return False
    if not tokens or PurePath(tokens[0]).name not in {"mpirun", "mpiexec", "srun"}:
        return False
    executable = _launcher_executable(tokens)
    return executable is not None and PurePath(executable).name in _VASP_EXECUTABLES


def _launcher_executable(tokens: list[str]) -> Optional[str]:
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token and token[0] in "|&;<>":
            return None
        if token == "--":
            index += 1
            return tokens[index] if index < len(tokens) else None
        if not token.startswith("-"):
            return token
        if "=" in token or token in _LAUNCH_OPTIONS_WITHOUT_VALUES:
            index += 1
            continue
        value_count = 2 if token in _LAUNCH_OPTIONS_WITH_TWO_VALUES else 1
        index += value_count + 1
    return None


def slurm_script_diff(
    before: str,
    after: str,
    script_name: str = "vasp.slurm",
) -> str:
    """Return a deterministic unified diff suitable for a resource preview."""
    if before == after:
        return ""
    lines = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{script_name}",
        tofile=f"b/{script_name}",
        lineterm="\n",
    )
    rendered = []
    for line in lines:
        if line.endswith(("\n", "\r")):
            rendered.append(line)
            continue
        rendered.append(line + "\n")
        if line.startswith((" ", "+", "-")):
            rendered.append("\\ No newline at end of file\n")
    return "".join(rendered)
