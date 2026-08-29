from __future__ import annotations

import copy
import json
from dataclasses import asdict, fields

import pytest

from vaspsolkit import cli as cli_module
from vaspsolkit import config as config_module
from vaspsolkit.cli import main
from vaspsolkit.config import KitConfig, SchedulerConfig, WorkflowConfig, write_kit_config


def test_kit_config_defaults_to_version_2() -> None:
    assert KitConfig().config_version == 2


def test_scheduler_config_has_slurm_local_server_defaults() -> None:
    assert asdict(SchedulerConfig()) == {
        "kind": "slurm",
        "partition": "compute",
        "nodes": [],
        "node_count": 1,
        "tasks": 96,
        "tasks_per_node": 96,
        "memory": "",
        "walltime": "72:00:00",
        "max_inflight": None,
        "script": "vasp.slurm",
        "launcher": "mpirun",
        "executable": "vasp_std",
        "module_init": "",
        "modules": [],
        "submit_command": [],
        "inspect_command": [],
        "status_command": [],
        "cancel_command": [],
        "job_id_pattern": r"(?P<job_id>\S+)",
    }


def test_pbs_scheduler_is_rejected() -> None:
    with pytest.raises(ValueError, match="slurm or custom"):
        SchedulerConfig(kind="pbs").validate()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"node_count": 0}, "node_count"),
        ({"tasks": 0}, "tasks"),
        ({"tasks_per_node": 0}, "tasks_per_node"),
        ({"node_count": 1, "tasks": 97, "tasks_per_node": 96}, "capacity"),
        ({"nodes": ["node01", "node02"], "node_count": 1}, "node_count"),
        ({"nodes": ["node01", "node01"], "node_count": 2}, "duplicates"),
        ({"nodes": ["node01", " node01 "], "node_count": 2}, "duplicates"),
        ({"nodes": ["node01", "  "], "node_count": 2}, "non-empty"),
        ({"kind": "custom"}, "submit_command"),
    ],
)
def test_scheduler_validation_rejects_invalid_resources(overrides, message) -> None:
    scheduler = SchedulerConfig(**overrides)
    with pytest.raises(ValueError, match=message):
        scheduler.validate()


def test_scheduler_validation_accepts_matching_explicit_nodes() -> None:
    SchedulerConfig(
        nodes=["node01", "node02"],
        node_count=2,
        tasks=192,
        tasks_per_node=96,
    ).validate()


def test_workflow_config_has_no_pbs_submission_fields() -> None:
    field_names = {item.name for item in fields(WorkflowConfig)}
    assert "pbs_file" not in field_names
    assert not {name for name in field_names if name.startswith("qsub_")}
    assert KitConfig().to_dict()["scheduler"]["script"] == "vasp.slurm"


def test_migrate_v2_returns_validated_normalized_copy_without_mutation() -> None:
    data = {
        "config_version": 2,
        "profile": "vaspsol-neutral-relax",
        "workflow": {"folders": ["1"], "nelect_offsets": [0]},
        "scheduler": {"tasks": 24, "tasks_per_node": 24},
    }
    original = copy.deepcopy(data)

    migrated = config_module.migrate_config_data(data)

    assert data == original
    assert migrated is not data
    assert migrated["workflow"]["folders"] == ["1"]
    assert migrated["scheduler"]["tasks"] == 24
    assert migrated["scheduler"]["partition"] == "compute"
    assert migrated["config_version"] == 2


@pytest.mark.parametrize(
    ("data", "error_path"),
    [
        ({"config_version": "2"}, "config_version"),
        ({"config_version": 2.0}, "config_version"),
        ({"config_version": 2.9}, "config_version"),
        ({"config_version": True}, "config_version"),
        ({"config_version": 3}, "config_version"),
        ({"config_version": 2, "workflow": []}, "workflow"),
        ({"config_version": 2, "scheduler": []}, "scheduler"),
        (
            {"config_version": 2, "scheduler": {"submit_command": "sbatch"}},
            "scheduler.submit_command",
        ),
        (
            {"config_version": 2, "scheduler": {"nodes": "node01"}},
            "scheduler.nodes",
        ),
        (
            {"config_version": 2, "scheduler": {"modules": "vasp/6"}},
            "scheduler.modules",
        ),
        (
            {"config_version": 2, "scheduler": {"tasks": "96"}},
            "scheduler.tasks",
        ),
        (
            {"config_version": 2, "scheduler": {"tasks": 96.0}},
            "scheduler.tasks",
        ),
        (
            {"config_version": 2, "scheduler": {"tasks": True}},
            "scheduler.tasks",
        ),
        (
            {"config_version": 2, "workflow": {"folders": "1"}},
            "workflow.folders",
        ),
        (
            {"config_version": 2, "workflow": {"nelect_offsets": "0"}},
            "workflow.nelect_offsets",
        ),
        (
            {
                "config_version": 2,
                "workflow": {"she_reference_confirmed": "false"},
            },
            "workflow.she_reference_confirmed",
        ),
        (
            {
                "config_version": 2,
                "workflow": {"charge_points_include_neutral": 1},
            },
            "workflow.charge_points_include_neutral",
        ),
        (
            {"config_version": 2, "workflow": {"interface_count": 1.0}},
            "workflow.interface_count",
        ),
        (
            {"config_version": 2, "workflow": {"she_reference": "4.70"}},
            "workflow.she_reference",
        ),
        (
            {"config_version": 2, "workflow": {"target_potentials": [True]}},
            "workflow.target_potentials[0]",
        ),
        (
            {
                "config_version": 1,
                "workflow": {},
                "scheduler": {"kind": "slurm", "cores": "96"},
            },
            "scheduler.cores",
        ),
    ],
)
def test_migrate_rejects_coercive_or_malformed_schema_values(data, error_path) -> None:
    with pytest.raises(ValueError) as error:
        config_module.migrate_config_data(data)

    assert error_path in str(error.value)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="positive-infinity"),
        pytest.param(float("-inf"), id="negative-infinity"),
    ],
)
def test_migrate_rejects_non_finite_scalar_with_path(value) -> None:
    with pytest.raises(ValueError) as error:
        config_module.migrate_config_data(
            {"config_version": 2, "workflow": {"nelect_ref": value}}
        )

    assert "workflow.nelect_ref" in str(error.value)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="positive-infinity"),
        pytest.param(float("-inf"), id="negative-infinity"),
    ],
)
def test_migrate_rejects_non_finite_list_item_with_path(value) -> None:
    with pytest.raises(ValueError) as error:
        config_module.migrate_config_data(
            {"config_version": 2, "workflow": {"target_potentials": [value]}}
        )

    assert "workflow.target_potentials[0]" in str(error.value)


@pytest.mark.parametrize(
    ("workflow", "error_path"),
    [
        ({"nelect_ref": 10**10000}, "workflow.nelect_ref"),
        ({"target_potentials": [10**10000]}, "workflow.target_potentials[0]"),
    ],
)
def test_migrate_wraps_oversized_numeric_conversion_as_value_error(
    workflow, error_path
) -> None:
    with pytest.raises(ValueError) as error:
        config_module.migrate_config_data(
            {"config_version": 2, "workflow": workflow}
        )

    assert error_path in str(error.value)


def test_write_kit_config_rejects_non_finite_serialization(tmp_path) -> None:
    target = tmp_path / "config.json"
    config = KitConfig(
        profile="vaspsol-neutral-relax",
        workflow=WorkflowConfig(target_potentials=[float("nan")]),
    )

    with pytest.raises(ValueError):
        write_kit_config(target, config)

    assert not target.exists()


def test_migrate_v1_slurm_maps_resources_and_removes_pbs_workflow_keys() -> None:
    data = {
        "config_version": 1,
        "profile": "vaspsol-neutral-relax",
        "workflow": {
            "folders": ["neutral"],
            "nelect_offsets": [0],
            "pbs_file": "obsolete.pbs",
            "qsub_queue": "obsolete",
            "qsub_ppn": 32,
            "qsub_min_node": 7,
            "qsub_walltime": "12:00:00",
        },
        "scheduler": {
            "kind": "slurm",
            "queue": "gpu",
            "cores": 64,
            "nodes": ["node01", "node02"],
            "memory": "256G",
            "max_inflight": 3,
            "script": "submit.slurm",
            "submit_command": ["sbatch", "{script}"],
            "inspect_command": ["scontrol", "show", "job", "{job_id}"],
            "status_command": ["squeue", "-j", "{job_id}"],
            "cancel_command": ["scancel", "{job_id}"],
        },
    }
    original = copy.deepcopy(data)

    migrated = config_module.migrate_config_data(data)

    assert data == original
    assert migrated["config_version"] == 2
    assert migrated["scheduler"]["partition"] == "gpu"
    assert migrated["scheduler"]["tasks"] == 64
    assert migrated["scheduler"]["tasks_per_node"] == 64
    assert migrated["scheduler"]["node_count"] == 2
    assert migrated["scheduler"]["walltime"] == "12:00:00"
    assert migrated["scheduler"]["nodes"] == ["node01", "node02"]
    assert migrated["scheduler"]["memory"] == "256G"
    assert migrated["scheduler"]["max_inflight"] == 3
    assert migrated["scheduler"]["script"] == "submit.slurm"
    assert migrated["scheduler"]["submit_command"] == ["sbatch", "{script}"]
    assert migrated["scheduler"]["inspect_command"] == [
        "scontrol",
        "show",
        "job",
        "{job_id}",
    ]
    assert migrated["scheduler"]["status_command"] == ["squeue", "-j", "{job_id}"]
    assert migrated["scheduler"]["cancel_command"] == ["scancel", "{job_id}"]
    assert not {key for key in migrated["workflow"] if key.startswith("qsub_")}
    assert "pbs_file" not in migrated["workflow"]


def test_migrate_v1_slurm_uses_workflow_ppn_when_cores_is_absent() -> None:
    migrated = config_module.migrate_config_data(
        {
            "config_version": 1,
            "profile": "vaspsol-neutral-relax",
            "workflow": {
                "folders": ["neutral"],
                "nelect_offsets": [0],
                "qsub_ppn": 40,
                "qsub_walltime": "18:00:00",
            },
            "scheduler": {"kind": "slurm", "walltime": "20:00:00"},
        }
    )

    assert migrated["scheduler"]["tasks"] == 40
    assert migrated["scheduler"]["tasks_per_node"] == 40
    assert migrated["scheduler"]["walltime"] == "20:00:00"


def test_migrate_v1_pbs_profile_is_rejected() -> None:
    with pytest.raises(ValueError, match="select a SLURM profile"):
        config_module.migrate_config_data(
            {
                "config_version": 1,
                "workflow": {},
                "scheduler": {"kind": "pbs"},
            }
        )


def test_migrate_flat_pbs_shaped_config_is_rejected() -> None:
    with pytest.raises(ValueError, match="select a SLURM profile"):
        config_module.migrate_config_data(
            {"folders": ["1"], "nelect_offsets": [0], "qsub_ppn": 48}
        )


def test_migrate_cli_previews_and_requires_confirmation(tmp_path) -> None:
    source = tmp_path / "v1.json"
    target = tmp_path / "v2.json"
    source.write_text(
        json.dumps(
            {
                "config_version": 1,
                "workflow": {},
                "scheduler": {"kind": "slurm"},
            }
        ),
        encoding="utf-8",
    )
    output = []

    result = main(
        ["migrate", "--input", str(source), "--output", str(target)],
        input_fn=lambda prompt: "n",
        output=output.append,
    )

    assert result == 1
    assert not target.exists()
    preview = "\n".join(output)
    assert f"--- {source}" in preview
    assert f"+++ {target}" in preview
    assert '"config_version": 2' in preview


def test_migrate_cli_writes_with_yes(tmp_path) -> None:
    source = tmp_path / "v1.json"
    target = tmp_path / "v2.json"
    source.write_text(
        json.dumps(
            {
                "config_version": 1,
                "workflow": {},
                "scheduler": {"kind": "slurm"},
            }
        ),
        encoding="utf-8",
    )

    result = main(
        ["migrate", "--input", str(source), "--output", str(target), "--yes"],
        input_fn=lambda prompt: pytest.fail(f"unexpected prompt: {prompt}"),
    )

    assert result == 0
    assert json.loads(target.read_text(encoding="utf-8"))["config_version"] == 2


def test_migrate_cli_refuses_unrelated_existing_target_without_force(tmp_path) -> None:
    source = tmp_path / "v1.json"
    target = tmp_path / "unrelated.json"
    source.write_text(
        json.dumps(
            {
                "config_version": 1,
                "workflow": {},
                "scheduler": {"kind": "slurm"},
            }
        ),
        encoding="utf-8",
    )
    original_target = b'{"keep": "exactly these bytes"}\n'
    target.write_bytes(original_target)

    with pytest.raises(FileExistsError, match="--force"):
        main(
            ["migrate", "--input", str(source), "--output", str(target), "--yes"],
            input_fn=lambda prompt: pytest.fail(f"unexpected prompt: {prompt}"),
        )

    assert target.read_bytes() == original_target


def test_migrate_cli_force_previews_existing_target_and_replaces_it(tmp_path) -> None:
    source = tmp_path / "v1.json"
    target = tmp_path / "existing.json"
    source.write_text(
        json.dumps(
            {
                "config_version": 1,
                "workflow": {},
                "scheduler": {"kind": "slurm"},
            }
        ),
        encoding="utf-8",
    )
    target.write_text('{"keep": "old destination"}\n', encoding="utf-8")
    output = []

    result = main(
        [
            "migrate",
            "--input",
            str(source),
            "--output",
            str(target),
            "--yes",
            "--force",
        ],
        output=output.append,
    )

    assert result == 0
    assert '"config_version": 2' in target.read_text(encoding="utf-8")
    preview = "\n".join(output)
    assert '-{"keep": "old destination"}' in preview
    assert f"--- {target}" in preview


def test_migrate_cli_supports_in_place_migration(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "config_version": 1,
                "workflow": {},
                "scheduler": {"kind": "slurm"},
            }
        ),
        encoding="utf-8",
    )

    result = main(
        ["migrate", "--input", str(path), "--output", str(path), "--yes"],
    )

    assert result == 0
    assert json.loads(path.read_text(encoding="utf-8"))["config_version"] == 2


def test_migrate_cli_rejects_concurrent_destination_change(tmp_path, monkeypatch) -> None:
    source = tmp_path / "v1.json"
    target = tmp_path / "existing.json"
    source.write_text(
        json.dumps(
            {
                "config_version": 1,
                "workflow": {},
                "scheduler": {"kind": "slurm"},
            }
        ),
        encoding="utf-8",
    )
    target.write_bytes(b"original destination\n")
    concurrent_bytes = b"concurrent edit\n"
    real_write = cli_module.write_kit_config

    def concurrent_write(path, config, **kwargs):
        path.write_bytes(concurrent_bytes)
        return real_write(path, config, **kwargs)

    monkeypatch.setattr(cli_module, "write_kit_config", concurrent_write)

    with pytest.raises(RuntimeError, match="changed"):
        main(
            [
                "migrate",
                "--input",
                str(source),
                "--output",
                str(target),
                "--yes",
                "--force",
            ]
        )

    assert target.read_bytes() == concurrent_bytes


def test_migrate_cli_rejects_concurrently_created_new_destination(
    tmp_path, monkeypatch
) -> None:
    source = tmp_path / "v1.json"
    target = tmp_path / "new.json"
    source.write_text(
        json.dumps(
            {
                "config_version": 1,
                "workflow": {},
                "scheduler": {"kind": "slurm"},
            }
        ),
        encoding="utf-8",
    )
    concurrent_bytes = b"created after preview\n"
    real_write = cli_module.write_kit_config

    def concurrent_create(path, config, **kwargs):
        path.write_bytes(concurrent_bytes)
        return real_write(path, config, **kwargs)

    monkeypatch.setattr(cli_module, "write_kit_config", concurrent_create)

    with pytest.raises(FileExistsError):
        main(
            ["migrate", "--input", str(source), "--output", str(target), "--yes"]
        )

    assert target.read_bytes() == concurrent_bytes
