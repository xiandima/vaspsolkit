from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from vaspsolkit.slurm_profiles import (
    CaseResourceOverride,
    SlurmProfile,
    apply_slurm_override,
    import_slurm_profile,
    rewrite_slurm_resources,
    slurm_script_diff,
)


def _profile(**overrides) -> SlurmProfile:
    values = {
        "name": "cluster-a",
        "partition": "compute",
        "node_count": 1,
        "tasks": 96,
        "tasks_per_node": 96,
        "walltime": "72:00:00",
        "script": "vasp.slurm",
    }
    values.update(overrides)
    return SlurmProfile(**values)


def test_imports_generic_reference_and_forces_portable_executable() -> None:
    script = """#!/bin/bash
#SBATCH -J example
#SBATCH -N 1
#SBATCH --ntasks=96
#SBATCH --ntasks-per-node=96
#SBATCH -t 72:00:00
#SBATCH -p long
#SBATCH --nodelist=node01

cd $SLURM_SUBMIT_DIR
ulimit -s unlimited
source /etc/profile.d/modules.sh
module load compiler/2024 mpi/5
module load vasp/6.4

mpirun -np 96 vasp_legacy > vasp.log 2>&1
"""

    profile = import_slurm_profile(script, "generic")

    assert profile == SlurmProfile(
        name="generic",
        partition="long",
        node_count=1,
        tasks=96,
        tasks_per_node=96,
        walltime="72:00:00",
        script="vasp.slurm",
        nodes=("node01",),
        launcher="mpirun",
        executable="vasp_std",
        module_init="/etc/profile.d/modules.sh",
        modules=("compiler/2024", "mpi/5", "vasp/6.4"),
    )


def test_import_uses_conservative_defaults() -> None:
    profile = import_slurm_profile("#!/bin/bash\ntrue\n", "default")

    assert profile.partition == "compute"
    assert profile.node_count == 1
    assert profile.tasks == 96
    assert profile.tasks_per_node == 96
    assert profile.walltime == "72:00:00"


@pytest.mark.parametrize(
    "script",
    [
        "#SBATCH --job-name demo\n#SBATCH --partition compute\n"
        "#SBATCH --nodes 2\n#SBATCH --ntasks 128\n"
        "#SBATCH --ntasks-per-node 64\n#SBATCH --time 10:20:30\n"
        "#SBATCH --nodelist node01,node02\n#SBATCH --output result.log\n",
        "#SBATCH -J=demo\n#SBATCH -p=compute\n#SBATCH -N=2\n"
        "#SBATCH -n=128\n#SBATCH --ntasks-per-node=64\n"
        "#SBATCH -t=10:20:30\n#SBATCH -w=node01,node02\n"
        "#SBATCH -o=result.log\n",
    ],
)
def test_import_accepts_short_long_equals_and_space_directives(script: str) -> None:
    profile = import_slurm_profile(script, "forms", script_name="job.slurm")

    assert profile.partition == "compute"
    assert profile.node_count == 2
    assert profile.tasks == 128
    assert profile.tasks_per_node == 64
    assert profile.walltime == "10:20:30"
    assert profile.nodes == ("node01", "node02")
    assert profile.script == "job.slurm"


@pytest.mark.parametrize(
    ("directive", "first", "second"),
    [
        ("partition", "#SBATCH -p compute", "#SBATCH --partition=long"),
        ("nodes", "#SBATCH -N 1", "#SBATCH --nodes=2"),
        ("ntasks", "#SBATCH -n 96", "#SBATCH --ntasks=48"),
        ("job-name", "#SBATCH -J one", "#SBATCH --job-name=two"),
        ("output", "#SBATCH -o one.log", "#SBATCH --output=two.log"),
    ],
)
def test_import_rejects_conflicting_duplicate_directives(
    directive: str, first: str, second: str
) -> None:
    with pytest.raises(ValueError, match=directive):
        import_slurm_profile(f"{first}\n{second}\n", "conflict")


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"script": "../vasp.slurm"}, "script"),
        ({"script": "/tmp/vasp.slurm"}, "script"),
        ({"partition": ""}, "partition"),
        ({"partition": "compute;touch"}, "partition"),
        ({"node_count": 0}, "node_count"),
        ({"tasks": 0}, "tasks"),
        ({"tasks_per_node": 0}, "tasks_per_node"),
        ({"tasks": 97}, "capacity"),
        ({"nodes": ("node01", "node02")}, "node_count"),
        ({"nodes": ("node01;down",)}, "node"),
        ({"walltime": "2:3:04"}, "walltime"),
        ({"launcher": "srun"}, "launcher"),
        ({"executable": "vasp_legacy"}, "executable"),
        ({"module_init": "/etc/profile;touch"}, "module_init"),
        ({"modules": ("vasp/6\nmalicious",)}, "module"),
    ],
)
def test_profile_rejects_unsafe_or_invalid_values(kwargs, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _profile(**kwargs)


@pytest.mark.parametrize(
    ("line", "directive"),
    [
        ("#SBATCH --partition=compute;touch", "partition"),
        ("#SBATCH --nodelist=node01,$(hostname)", "nodelist"),
        ("#SBATCH --time=72:00:00|sh", "time"),
        ("#SBATCH --job-name=demo&&id", "job-name"),
        ("#SBATCH --output=log;id", "output"),
        ("source /etc/profile;id", "source"),
        ("module load vasp/6;id", "module"),
    ],
)
def test_import_rejects_shell_metacharacters(line: str, directive: str) -> None:
    with pytest.raises(ValueError, match=directive):
        import_slurm_profile(line + "\n", "unsafe")


def test_profile_and_override_are_frozen() -> None:
    profile = _profile()
    override = CaseResourceOverride(tasks=48)

    with pytest.raises(FrozenInstanceError):
        profile.tasks = 48  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        override.tasks = 24  # type: ignore[misc]


def test_apply_override_is_non_mutating_and_covers_every_resource() -> None:
    profile = _profile()
    override = CaseResourceOverride(
        partition="long",
        nodes=("node01", "node02"),
        node_count=2,
        tasks=128,
        tasks_per_node=64,
        walltime="10:00:00",
    )

    resolved = apply_slurm_override(profile, override)

    assert resolved == _profile(
        partition="long",
        nodes=("node01", "node02"),
        node_count=2,
        tasks=128,
        tasks_per_node=64,
        walltime="10:00:00",
    )
    assert profile == _profile()


def test_rewrite_updates_resources_removes_auto_nodes_and_preserves_content() -> None:
    before = """#!/bin/bash
#SBATCH -J retained-name
#SBATCH -p old # partition comment
#SBATCH -N 2
#SBATCH -n 64
#SBATCH --ntasks-per-node 32
#SBATCH -t 12:00:00
#SBATCH -w node01,node02
#SBATCH --nodelist=node01,node02
#SBATCH -o retained-%j.log
# retained comment

cd "$SLURM_SUBMIT_DIR"
ulimit -s unlimited
source /etc/profile.d/modules.sh
module load vasp/6.4
export OMP_NUM_THREADS=1
echo /data/vasp_std/reference
# mpirun -np 64 vasp_legacy > ignored.log
mpirun -np 64 vasp_legacy > old.log 2>&1
echo done
"""

    after = rewrite_slurm_resources(before, _profile(tasks=48, tasks_per_node=48))

    assert "#SBATCH --partition=compute # partition comment" in after
    assert "#SBATCH --nodes=1" in after
    assert "#SBATCH --ntasks=48" in after
    assert "#SBATCH --ntasks-per-node=48" in after
    assert "#SBATCH --time=72:00:00" in after
    assert "--nodelist" not in after
    assert "#SBATCH -w " not in after
    assert "#SBATCH -J retained-name" in after
    assert "#SBATCH -o retained-%j.log" in after
    assert "# retained comment" in after
    assert 'cd "$SLURM_SUBMIT_DIR"' in after
    assert "ulimit -s unlimited" in after
    assert "source /etc/profile.d/modules.sh" in after
    assert "module load vasp/6.4" in after
    assert "export OMP_NUM_THREADS=1" in after
    assert "echo /data/vasp_std/reference" in after
    assert "# mpirun -np 64 vasp_legacy > ignored.log" in after
    assert after.count(
        "mpirun -np ${SLURM_NTASKS:-48} vasp_std > vasp.log 2>&1"
    ) == 1
    assert "echo done" in after


def test_rewrite_explicit_nodes_writes_exactly_one_nodelist() -> None:
    before = "#SBATCH --nodelist=old01\n#SBATCH -w old01\ntrue\n"
    profile = _profile(
        nodes=("node01", "node02"),
        node_count=2,
        tasks=192,
        tasks_per_node=96,
    )

    after = rewrite_slurm_resources(before, profile)

    assert after.count("#SBATCH --nodelist=node01,node02") == 1
    assert after.count("nodelist") == 1
    assert "-w old01" not in after


@pytest.mark.parametrize(
    ("before", "line_ending", "has_final_newline"),
    [
        ("#!/bin/bash\r\n#SBATCH -p old\r\ntrue\r\n", "\r\n", True),
        ("#!/bin/bash\r\n#SBATCH -p old\r\ntrue", "\r\n", False),
        ("#!/bin/bash\n#SBATCH -p old\ntrue", "\n", False),
    ],
)
def test_rewrite_preserves_newline_style_and_final_newline(
    before: str, line_ending: str, has_final_newline: bool
) -> None:
    after = rewrite_slurm_resources(before, _profile())

    assert after.endswith(line_ending) is has_final_newline
    if line_ending == "\r\n":
        assert "\n" not in after.replace("\r\n", "")
    assert "mpirun -np ${SLURM_NTASKS:-96} vasp_std > vasp.log 2>&1" in after


def test_rewrite_inserts_missing_resources_after_shebang() -> None:
    after = rewrite_slurm_resources("#!/bin/bash\ntrue\n", _profile())
    lines = after.splitlines()

    assert lines[:6] == [
        "#!/bin/bash",
        "#SBATCH --partition=compute",
        "#SBATCH --nodes=1",
        "#SBATCH --ntasks=96",
        "#SBATCH --ntasks-per-node=96",
        "#SBATCH --time=72:00:00",
    ]


def test_slurm_script_diff_is_stable_and_empty_for_identical_text() -> None:
    before = "#!/bin/bash\ntrue\n"
    after = "#!/bin/bash\nfalse\n"

    assert slurm_script_diff(before, before) == ""
    assert slurm_script_diff(before, after) == (
        "--- a/vasp.slurm\n"
        "+++ b/vasp.slurm\n"
        "@@ -1,2 +1,2 @@\n"
        " #!/bin/bash\n"
        "-true\n"
        "+false\n"
    )


def test_portable_slurm_template_has_no_site_specific_values() -> None:
    template = (
        Path(__file__).parents[1] / "templates" / "slurm.vasp.sh"
    ).read_text(encoding="utf-8")

    assert template.startswith("#!/bin/bash\n")
    assert "#SBATCH --partition=compute" in template
    assert "#SBATCH --nodes=1" in template
    assert "#SBATCH --ntasks=96" in template
    assert "#SBATCH --ntasks-per-node=96" in template
    assert "#SBATCH --time=72:00:00" in template
    assert "nodelist" not in template.lower()
    assert "module load" not in template
    assert "source /" not in template
    assert 'cd "${SLURM_SUBMIT_DIR}"' in template
    assert "ulimit -s unlimited" in template
    assert (
        'mpirun -np "${SLURM_NTASKS}" vasp_std > vasp.log 2>&1' in template
    )
