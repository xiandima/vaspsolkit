"""First-run menu journeys with synthetic inputs and no external executables."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_cli_user_experience import (
    _contents,
    _write_synthetic_inputs,
    isolated_case,
)
from tests.test_input_completion import library
from tests.test_vaspsolkit_interactive_menu import RecordingScheduler
from vaspsolkit.guide_model import build_snapshot, recommend_action
from vaspsolkit.interactive_menu import run_menu, run_menu_action, synchronize_case


@pytest.fixture
def new_user(isolated_case, monkeypatch):
    root, env = isolated_case
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    settings_root = Path(env["HOME"]) / ".config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(settings_root))
    monkeypatch.chdir(root)
    _write_synthetic_inputs(root)
    potentials = library(root)
    template = root / "INCAR.template"
    (root / "INCAR").rename(template)
    (root / "POTCAR").unlink()
    (root / "KPOINTS").unlink()

    def no_external_command(*args, **kwargs):
        pytest.fail(f"unexpected external command: {args}")

    monkeypatch.setattr("subprocess.run", no_external_command)
    scheduler = RecordingScheduler()
    return SimpleNamespace(
        root=root,
        template=template,
        potentials=potentials,
        settings=settings_root / "vaspsolkit" / "inputs.json",
        scheduler=scheduler,
    )


def _completion_answer(user, prompts, *, save="n", confirm="y", mesh="2 2 1"):
    answers = {
        "INCAR": str(user.template),
        "授权": str(user.potentials),
        "赝势泛函": "PBE",
        "C 对应": "",
        "KPOINTS": mesh,
        "保存": save,
        "确认补全": confirm,
    }

    def answer(prompt):
        prompts.append(prompt)
        for prefix, value in answers.items():
            if prompt.startswith(prefix):
                return value
        pytest.fail(f"unexpected prompt: {prompt}")

    return answer


@pytest.mark.parametrize("menu_code", ["14", "02"])
def test_first_completion_offers_settings_and_reuses_them(new_user, menu_code):
    user = new_user
    prompts, messages = [], []
    source = _contents(user.root)
    assert recommend_action(build_snapshot(user.root)).cli_command == "inputs"

    assert run_menu_action(
        menu_code,
        build_snapshot(user.root),
        input_fn=_completion_answer(user, prompts, save="y"),
        output=messages.append,
    ) == 0

    assert any(prompt.startswith("保存") for prompt in prompts), prompts
    assert user.settings.is_file(), "menu offered no way to persist the chosen library"
    saved = json.loads(user.settings.read_text())
    assert saved["potcar_root"] == str(user.potentials.resolve())
    assert saved["functional"] == "PBE"
    assert saved["potentials"] == {"C": "C"}
    assert "mesh" not in saved
    assert "incar_template" not in saved
    assert (user.root / "INCAR").read_bytes() == source["INCAR.template"]
    assert all((user.root / name).read_bytes() == data for name, data in source.items())
    assert recommend_action(build_snapshot(user.root)).cli_command == "init"

    second = user.root.parent / "second case"
    second.mkdir()
    (second / "POSCAR").write_bytes(source["POSCAR"])
    prompts.clear()
    assert run_menu_action(
        "02",
        build_snapshot(second),
        input_fn=_completion_answer(user, prompts, mesh="4 4 1"),
        output=messages.append,
    ) == 0
    assert not any(prompt.startswith(("授权", "赝势泛函", "C 对应")) for prompt in prompts)
    assert any(prompt.startswith("KPOINTS") for prompt in prompts)
    assert "4 4 1" in (second / "KPOINTS").read_text()
    assert (second / "POTCAR").read_bytes() == (user.potentials / "C" / "POTCAR").read_bytes()
    assert json.loads(user.settings.read_text()) == saved
    assert user.scheduler.queried == []


@pytest.mark.parametrize("save, confirm", [("y", "n"), ("n", "y"), ("", "y")])
def test_declining_completion_or_settings_does_not_save(new_user, save, confirm):
    user = new_user
    before = _contents(user.root)
    prompts, messages = [], []
    result = run_menu_action(
        "14",
        build_snapshot(user.root),
        input_fn=_completion_answer(user, prompts, save=save, confirm=confirm),
        output=messages.append,
    )

    assert result == (1 if confirm == "n" else 0)
    assert not user.settings.exists()
    if confirm == "n":
        assert _contents(user.root) == before
        assert any("取消" in message for message in messages)
    else:
        assert (user.root / "INCAR").is_file()
        assert (user.root / "POTCAR").is_file()
        assert (user.root / "KPOINTS").is_file()


@pytest.mark.parametrize("menu_code", ["14", "02"])
def test_bad_mesh_explains_retry_then_reaches_initialization(new_user, menu_code):
    user = new_user
    before = _contents(user.root)
    commands = iter([menu_code, menu_code, "02", "00"])
    prompts, messages = [], []
    attempt = 0
    initialization_answers = {
        "该操作将修改": "y",
        "SHE reference": "4.7",
        "参考来源": "synthetic menu test",
        "Select:": "1",
        "Submission script": "vasp.slurm",
        "Additional TAG=VALUE": "",
        "Write INCAR": "y",
    }

    def answer(prompt):
        nonlocal attempt
        if prompt.startswith("输入任务编号"):
            attempt += 1
            if attempt == 2:
                assert _contents(user.root) == before
            if attempt == 3:
                assert recommend_action(build_snapshot(user.root)).cli_command == "init"
            return next(commands)
        if attempt <= 2:
            return _completion_answer(
                user, prompts, mesh="2 nope 1" if attempt == 1 else "2 2 1"
            )(prompt)
        prompts.append(prompt)
        for prefix, value in initialization_answers.items():
            if prompt.startswith(prefix):
                return value
        pytest.fail(f"unexpected initialization prompt: {prompt}")

    assert run_menu(
        user.root,
        input_fn=answer,
        output=messages.append,
        synchronize_fn=lambda root, **kwargs: synchronize_case(
            root, scheduler_factory=lambda config: user.scheduler, **kwargs
        ),
    ) == 0

    assert any("--mesh requires three positive integers" in message for message in messages)
    recovery = [message for message in messages if message.startswith("恢复建议")]
    assert any("14" in message and "02" in message and "重新" in message for message in recovery), recovery
    assert (user.root / "vaspsolkit.json").is_file()
    config = json.loads((user.root / "vaspsolkit.json").read_text())
    assert config["scheduler"]["script"] == "vasp.slurm"
    assert config["workflow"]["she_reference"] == 4.7
    assert (user.root / "vasp.slurm").read_bytes() == before["vasp.slurm"]
    assert (user.root / "INCAR.template").read_bytes() == before["INCAR.template"]
    assert not user.settings.exists()
    assert user.scheduler.queried == []
