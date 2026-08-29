from __future__ import annotations

import copy
import json
from dataclasses import asdict, fields

import pytest

from vaspsolkit import config as config_module
from vaspsolkit.cli import main
from vaspsolkit.config import KitConfig, SchedulerConfig, WorkflowConfig


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
        "workflow": {"folders": [1], "nelect_offsets": [0]},
        "scheduler": {"tasks": "24", "tasks_per_node": "24"},
    }
    original = copy.deepcopy(data)

    migrated = config_module.migrate_config_data(data)

    assert data == original
    assert migrated is not data
    assert migrated["workflow"]["folders"] == ["1"]
    assert migrated["scheduler"]["tasks"] == 24
    assert migrated["scheduler"]["partition"] == "compute"
    assert migrated["config_version"] == 2


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
