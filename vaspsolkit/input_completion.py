"""Confirmed, missing-file-only input generation for CLI and menu callers."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .config import EXPECT_ABSENT, write_config_bytes
from .incar import parse_incar, replace_or_append
from .inputs import poscar_elements, suggest_encut, validate_potcar_order
from .state import WorkflowState, workflow_state_lock


INPUT_NAMES = ("POSCAR", "INCAR", "POTCAR", "KPOINTS")
FAMILIES = {"PBE": {"PAW_PBE"}, "LDA": {"PAW"}, "PW91": {"PAW_GGA"}}


def add_input_options(parser):
    parser.add_argument("--incar-template", help="copy this template only when INCAR is missing")
    parser.add_argument("--backend", choices=("native", "vaspkit"), default=None)
    parser.add_argument("--mesh", type=int, nargs=3, metavar=("NX", "NY", "NZ"))
    parser.add_argument("--centering", choices=("gamma", "monkhorst"), default=None)
    parser.add_argument("--kpr", type=float, help="VASPKIT density in 2*pi/Angstrom, NOT VASP KSPACING")
    parser.add_argument("--potcar-root", help="licensed functional-specific library root containing element/POTCAR")
    parser.add_argument("--functional", choices=tuple(FAMILIES))
    parser.add_argument("--potential", action="append", default=[], metavar="ELEMENT=VARIANT")
    parser.add_argument("--settings", help="reusable input settings JSON; defaults to XDG_CONFIG_HOME/vaspsolkit/inputs.json")
    parser.add_argument("--save-settings", action="store_true", help="persist library/mapping settings after confirmation; never saves an explicit mesh")


def _snapshot(root):
    result = {}
    for name in INPUT_NAMES:
        path = root / name
        if path.exists() or path.is_symlink():
            if not path.is_file() or not path.stat().st_size:
                raise ValueError(f"{name} must be a non-empty file: {path}")
            result[name] = path.read_bytes()
        else:
            result[name] = None
    return result


def _unchanged(root, before):
    if _snapshot(root) != before:
        raise RuntimeError("inputs changed during completion; discard this stale preview and retry")


def _check_idle(root):
    from .operations.activity import read_submission_receipt

    try:
        receipt = read_submission_receipt(root)
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("unreadable submission receipt; reconcile it before input completion") from exc
    if receipt is not None:
        raise ValueError(f"unresolved submission receipt ({receipt.status}); reconcile it before input completion")
    path = root / "vaspsolkit.state.json"
    if not path.exists():
        return
    state = WorkflowState.load(path)
    records = list(state.jobs.values()) + ([state.neutral] if state.neutral else [])
    active = [r.status for r in records if r.status in {"SUBMITTED", "QUEUED", "RUNNING", "UNKNOWN"}]
    if active:
        raise ValueError("input completion is blocked while jobs are active or UNKNOWN; check recorded jobs first")


def _settings_path(args):
    if args.settings:
        return Path(args.settings).expanduser().resolve()
    base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return base / "vaspsolkit" / "inputs.json"


def _load_settings(path):
    before = path.read_bytes() if path.exists() else None
    try:
        data = json.loads(before) if before is not None else {}
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"invalid input settings JSON: {path.resolve()}: {exc}; "
            "correct this file or use --settings PATH to select another settings file"
        ) from exc
    if not isinstance(data, dict) or data.get("schema_version", 1) != 1:
        raise ValueError(f"unsupported input settings: {path}")
    mapping = data.get("potentials", {})
    if not isinstance(mapping, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in mapping.items()):
        raise ValueError("input settings potentials must map element names to variant names")
    for key in ("backend", "centering", "potcar_root", "functional"):
        if key in data and not isinstance(data[key], str):
            raise ValueError(f"input settings {key} must be a string")
    if "kpr" in data and data["kpr"] is not None and type(data["kpr"]) not in (int, float):
        raise ValueError("input settings kpr must be a finite positive number")
    return data, before


def _options(args, saved):
    result = dict(saved)
    result["potentials"] = dict(saved.get("potentials", {}))
    for key in ("backend", "centering", "kpr", "potcar_root", "functional"):
        value = getattr(args, key, None)
        if value is not None:
            result[key] = value
    result.setdefault("backend", "native")
    result.setdefault("centering", "gamma")
    if result.get("potcar_root"):
        result["potcar_root"] = str(Path(result["potcar_root"]).expanduser().resolve())
    result["mesh"] = args.mesh
    if result["backend"] not in ("native", "vaspkit"):
        raise ValueError("input backend must be native or vaspkit")
    if result["centering"] not in ("gamma", "monkhorst"):
        raise ValueError("centering must be gamma or monkhorst")
    for item in args.potential:
        if "=" not in item:
            raise ValueError("--potential requires ELEMENT=VARIANT")
        element, variant = item.split("=", 1)
        result["potentials"][element.strip()] = variant.strip()
    return result


def _potential_parts(elements, options):
    root = Path(options["potcar_root"]).expanduser().resolve()
    functional = options["functional"]
    if functional not in FAMILIES:
        raise ValueError("functional must be PBE, LDA or PW91")
    parts = {}
    for element in elements:
        variant = options["potentials"][element]
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*", variant):
            raise ValueError(f"invalid potential variant for {element}: {variant}")
        path = (root / variant / "POTCAR").resolve()
        if root not in path.parents:
            raise ValueError(f"potential path escapes the selected library: {element}")
        data = path.read_bytes()
        _check_potential(data, functional, [variant])
        suggest_encut(path)
        parts[element] = data
    options["potcar_root"] = str(root)
    return parts


def _check_potential(data, functional, variants):
    text = data.decode("utf-8", errors="replace")
    records = re.findall(r"^\s*TITEL\s*=\s*(\S+)\s+(\S+)", text, re.M | re.I)
    if [item[1] for item in records] != variants:
        raise ValueError("POTCAR variant/order does not match the confirmed potential mapping")
    if any(item[0] not in FAMILIES[functional] for item in records):
        raise ValueError("POTCAR functional family does not match --functional")
    enmax = re.findall(r"\bENMAX\s*=\s*([^;\s]+)", text, re.I)
    if len(enmax) != len(variants):
        raise ValueError("each selected potential must contain one ENMAX record")
    for value in enmax:
        if not math.isfinite(float(value)) or float(value) <= 0:
            raise ValueError("potential ENMAX must be finite and positive")


def _resolve_missing(options, missing, elements, interactive, input_fn):
    absent = []
    if "POTCAR" in missing:
        for key, prompt in (("potcar_root", "授权赝势库目录（包含 C/POTCAR 等）"), ("functional", "赝势泛函 PBE/LDA/PW91")):
            if not options.get(key) and interactive:
                options[key] = input_fn(f"{prompt} >> ").strip()
            if not options.get(key):
                absent.append("--" + key.replace("_", "-"))
        for element in elements:
            if not options["potentials"].get(element) and interactive:
                options["potentials"][element] = input_fn(f"{element} 对应赝势名称 [{element}] >> ").strip() or element
            if not options["potentials"].get(element):
                absent.append(f"--potential {element}=VARIANT")
    if "KPOINTS" in missing:
        if options["mesh"] is None and options["backend"] == "native":
            if interactive:
                raw = input_fn("KPOINTS 网格 NX NY NZ（例如 3 3 1，须按体系确认） >> ")
                try:
                    options["mesh"] = [int(value) for value in raw.split()]
                except ValueError as exc:
                    raise ValueError("--mesh requires three positive integers") from exc
            else:
                absent.append("--mesh NX NY NZ (or --backend vaspkit --kpr VALUE)")
        if options["mesh"] is None and options["backend"] == "vaspkit" and options.get("kpr") is None:
            if interactive:
                options["kpr"] = float(input_fn("VASPKIT kpr（单位 2*pi/Angstrom） >> "))
            else:
                absent.append("--kpr VALUE")
    if absent:
        raise ValueError("missing " + ", ".join(missing) + ": input completion requires explicit " + ", ".join(absent))
    mesh = options["mesh"]
    if mesh is not None and (len(mesh) != 3 or any(type(x) is not int or x <= 0 for x in mesh)):
        raise ValueError("--mesh requires three positive integers")
    kpr = options.get("kpr")
    if kpr is not None and (isinstance(kpr, bool) or not math.isfinite(float(kpr)) or float(kpr) <= 0):
        raise ValueError("--kpr must be finite and positive (units 2*pi/Angstrom)")


def _vaspkit(stage, options, elements, parts, missing):
    executable = shutil.which("vaspkit")
    if executable is None:
        raise FileNotFoundError("vaspkit is unavailable; install it or select --backend native --mesh NX NY NZ")
    executable = os.path.abspath(executable)
    home = stage / "helper-home"
    home.mkdir()
    potroot = home / "potentials"
    for element, data in parts.items():
        target = potroot / element / "POTCAR"
        target.parent.mkdir(parents=True)
        target.write_bytes(data)
    config = [
        f"{name}_PATH = {potroot}" for name in ("PBE", "LDA", "GGA")
    ] + [
        f"POTCAR_TYPE = {options.get('functional', 'PBE')}",
        "RECOMMENDED_POTCAR = .FALSE.", "GW_POTCAR = .FALSE.",
        "AUTO_SUBMIT_JOB = .FALSE.", "AUTO_PLOT = .FALSE.",
    ]
    (home / ".vaspkit").write_text("\n".join(config) + "\n", encoding="utf-8")
    env = {**os.environ, "HOME": str(home)}

    def run(arguments):
        try:
            result = subprocess.run(
                [executable, *arguments], cwd=stage, env=env, input="",
                text=True, capture_output=True, timeout=120, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("VASPKIT timed out after 120s; no generated inputs imported") from exc
        if result.returncode:
            raise RuntimeError(f"VASPKIT exited with code {result.returncode}; no generated inputs imported")
        return result

    help_result = run(["-help"])
    match = re.search(r"VASPKIT.*?(\d+\.\d+\.\d+)", help_result.stdout + help_result.stderr, re.S | re.I)
    if match is None or not match.group(1).startswith("1.5."):
        raise ValueError("automatic VASPKIT tasks currently support 1.5.x; use native inputs for other versions")
    calls = []
    if "POTCAR" in missing:
        calls.append(["-task", "103"])
    if "KPOINTS" in missing and options["mesh"] is None:
        calls.append(["-task", "102", "-kpr", str(options["kpr"]), "-kps", "G" if options["centering"] == "gamma" else "M"])
    for arguments in calls:
        run(arguments)
    return {"version": match.group(1), "executable": executable, "arguments": calls}


def complete_inputs(args, input_fn=input, output=print, *, require_incar=True, incar_overrides=None):
    from .sampling import describe_sampling, kpoints_required
    from .operations.snapshot import _validate_kpoints
    from .cli import _publish_missing_inputs

    root = Path(args.workdir).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Case directory is missing: {root}")
    before = _snapshot(root)
    if before["POSCAR"] is None:
        raise FileNotFoundError("POSCAR is required; input completion cannot infer a structure from INCAR")
    elements = poscar_elements(root / "POSCAR")
    dry_run = getattr(args, "dry_run", False)
    interactive = not args.yes and not dry_run and (input_fn is not input or sys.stdin.isatty())
    template = args.incar_template
    if before["INCAR"] is None and require_incar and not template:
        if interactive:
            template = input_fn("INCAR 模板路径 >> ").strip()
        if not template:
            raise ValueError("missing INCAR: provide --incar-template PATH")
    template_data = None
    if template:
        template_data = Path(template).expanduser().read_bytes()
        if not parse_incar(template_data.decode("utf-8")):
            raise ValueError("INCAR template contains no readable tags")
        if before["INCAR"] is not None and template_data != before["INCAR"]:
            raise ValueError("INCAR already exists and differs from the template; existing files are never replaced")

    with tempfile.TemporaryDirectory(prefix="vaspsolkit-complete-") as temporary:
        stage = Path(temporary)
        for name, data in before.items():
            if data is not None:
                (stage / name).write_bytes(data)
        if template_data is not None and before["INCAR"] is None:
            (stage / "INCAR").write_bytes(template_data)
        if incar_overrides:
            text = (stage / "INCAR").read_text(encoding="utf-8") if (stage / "INCAR").exists() else ""
            for key, value in incar_overrides.items():
                text = replace_or_append(text, key, value)
            (stage / "INCAR").write_text(text, encoding="utf-8")
        effective_incar = (stage / "INCAR").read_bytes() if (stage / "INCAR").exists() else None
        need_kpoints = kpoints_required(stage)
        missing = [name for name in ("POTCAR", "KPOINTS") if before[name] is None and (name != "KPOINTS" or need_kpoints)]
        if template_data is not None and before["INCAR"] is None:
            missing.insert(0, "INCAR")
        if before["POTCAR"] is not None:
            validate_potcar_order(stage / "POSCAR", stage / "POTCAR")
        if before["KPOINTS"] is not None:
            _validate_kpoints(before["KPOINTS"].decode("utf-8"))
        if "KPOINTS" not in missing:
            output(describe_sampling(stage))
        if not missing and not args.save_settings:
            output("输入已齐全；未修改文件。")
            return 0
        if missing:
            _check_idle(root)
        settings_path = _settings_path(args)
        saved, settings_before = _load_settings(settings_path)
        options = _options(args, saved)
        _resolve_missing(options, missing, elements, interactive, input_fn)
        parts = _potential_parts(elements, options) if "POTCAR" in missing else {}
        output("输入补全预览")
        output("  仅生成：" + (", ".join(missing) or "无"))
        output(f"  后端：{options['backend']}")
        if "KPOINTS" in missing:
            output(f"  采样：mesh={options['mesh']} kpr={options.get('kpr')} (2*pi/Angstrom), centering={options['centering']}")
        if parts:
            output(f"  赝势库：{options['potcar_root']}；泛函：{options['functional']}")
            output("  元素映射：" + ", ".join(f"{e}={options['potentials'][e]}" for e in elements))
        save_settings = args.save_settings
        if parts and interactive and not save_settings:
            save_settings = input_fn("保存赝势库、元素映射等设置供后续 Case 复用（不保存显式网格）？[y/N] >> ").strip().lower() in {"y", "yes"}
        if save_settings:
            output(f"  保存可复用配置：{settings_path}（不保存显式网格）")
        if dry_run:
            output("预览完成；未运行外部工具、写文件或提交任务。")
            return 0
        if not args.yes:
            if not interactive:
                raise ValueError("input completion needs confirmation; use --dry-run or explicit parameters with --yes")
            if input_fn("确认补全以上输入？[y/N] >> ").strip().lower() not in {"y", "yes"}:
                output("已取消；未修改输入。")
                return 1
        _unchanged(root, before)
        helper = None
        if options["backend"] == "vaspkit" and (parts or ("KPOINTS" in missing and options["mesh"] is None)):
            helper = _vaspkit(stage, options, elements, parts, missing)
        elif parts:
            (stage / "POTCAR").write_bytes(b"".join(parts[element] for element in elements))
        staged_poscar = stage / "POSCAR"
        if staged_poscar.is_symlink() or not staged_poscar.is_file() or staged_poscar.read_bytes() != before["POSCAR"]:
            raise ValueError("helper changed staged POSCAR geometry; no generated inputs imported")
        if "KPOINTS" in missing and options["mesh"] is not None:
            mode = "Gamma" if options["centering"] == "gamma" else "Monkhorst-Pack"
            (stage / "KPOINTS").write_text("VASPsolKit explicit mesh\n0\n" + mode + "\n" + " ".join(map(str, options["mesh"])) + "\n0 0 0\n", encoding="utf-8")
        generated = {}
        for name in missing:
            candidate = stage / name
            if candidate.is_symlink() or not candidate.is_file() or not candidate.stat().st_size:
                raise ValueError(f"helper did not create a non-empty regular {name}; no inputs imported")
            generated[name] = candidate.read_bytes()
        if "INCAR" in generated:
            if generated["INCAR"] != effective_incar:
                raise ValueError("helper changed the INCAR template; no inputs imported")
            generated["INCAR"] = template_data
        if "POTCAR" in generated:
            _check_potential(generated["POTCAR"], options["functional"], [options["potentials"][e] for e in elements])
            validate_potcar_order(stage / "POSCAR", stage / "POTCAR")
            if generated["POTCAR"] != b"".join(parts[element] for element in elements):
                raise ValueError("generated POTCAR content differs from the confirmed library datasets; no inputs imported")
        if "KPOINTS" in generated:
            _validate_kpoints(generated["KPOINTS"].decode("utf-8"))
        audit = {
            "schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
            "generated": list(generated), "backend": options["backend"], "helper": helper,
            "sampling": {key: options.get(key) for key in ("mesh", "kpr", "centering")},
            "functional": options.get("functional"),
            "potentials": {e: options["potentials"].get(e) for e in elements} if parts else {},
            "sources_sha256": {name: hashlib.sha256(data).hexdigest() for name, data in before.items() if data is not None},
            "generated_sha256": {name: hashlib.sha256(data).hexdigest() for name, data in generated.items()},
            "potential_sources_sha256": {e: hashlib.sha256(data).hexdigest() for e, data in parts.items()},
        }
        if generated:
            audit_name = f"vaspsolkit.inputs-{uuid.uuid4().hex}.json"
            generated[audit_name] = (json.dumps(audit, indent=2, sort_keys=True) + "\n").encode("utf-8")
        with workflow_state_lock(root / "vaspsolkit.state.json"):
            _unchanged(root, before)
            if generated:
                _check_idle(root)
            _publish_missing_inputs(root, generated)
        for name in generated:
            output(f"wrote {root / name}")
        if save_settings:
            reusable = {key: options[key] for key in ("backend", "centering", "kpr", "potcar_root", "functional", "potentials") if key in options}
            reusable["schema_version"] = 1
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                write_config_bytes(settings_path, (json.dumps(reusable, indent=2, sort_keys=True) + "\n").encode(), expected_current=settings_before if settings_before is not None else EXPECT_ABSENT)
            except (OSError, RuntimeError) as exc:
                raise RuntimeError(f"inputs were completed but reusable settings were not saved: {settings_path}; {exc}") from exc
        output("输入补全完成；未提交任务。")
        return 0
