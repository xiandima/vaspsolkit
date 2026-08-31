from pathlib import Path
import subprocess


def test_submit_passes_slurm_resources_and_normalizes_job_id() -> None:
    from vaspsolkit.scheduler import SlurmScheduler

    calls = []
    def runner(args, cwd=None):
        calls.append((list(args), cwd))
        return subprocess.CompletedProcess(args, 0, "4321;cluster\n", "")

    job_id = SlurmScheduler(runner).submit(
        Path("/tmp/case"), "vasp.slurm", partition="compute",
        nodes=("node11",), node_count=1, tasks=96,
        tasks_per_node=96, walltime="72:00:00",
    )
    assert job_id == "4321"
    assert calls[0][0] == [
        "sbatch", "--parsable", "--partition", "compute", "--nodes", "1",
        "--ntasks", "96", "--ntasks-per-node", "96", "--time", "72:00:00",
        "--nodelist", "node11", "vasp.slurm",
    ]


def test_status_uses_sacct_after_job_leaves_squeue() -> None:
    from vaspsolkit.scheduler import SlurmScheduler

    replies = iter([(0, "", ""), (0, "4321|COMPLETED|0:0\n4321.batch|COMPLETED|0:0\n", "")])
    def runner(args, cwd=None):
        code, out, err = next(replies)
        return subprocess.CompletedProcess(args, code, out, err)

    state = SlurmScheduler(runner).status("4321")
    assert state.exists and state.state == "COMPLETED" and state.exit_code == "0:0"


def test_sacct_failure_is_unknown_not_missing() -> None:
    from vaspsolkit.scheduler import SlurmScheduler

    replies = iter([(0, "", ""), (1, "", "controller unavailable")])
    def runner(args, cwd=None):
        code, out, err = next(replies)
        return subprocess.CompletedProcess(args, code, out, err)
    state = SlurmScheduler(runner).status("4321")
    assert state.exists and state.state == "UNKNOWN"


def test_inspect_partitions_and_nodes() -> None:
    from vaspsolkit.scheduler import SlurmScheduler

    outputs = iter([
        "compute*\nlong\ncompute*\n",
        "node08|compute*|mixed|96|40/56/0/96\nnode11|compute*|idle|96|0/96/0/96\n",
    ])
    def runner(args, cwd=None):
        return subprocess.CompletedProcess(args, 0, next(outputs), "")
    scheduler = SlurmScheduler(runner)
    assert scheduler.inspect_partitions() == ["compute", "long"]
    nodes = scheduler.inspect_nodes("compute")
    assert [(n.name, n.allocated_cores, n.idle_cores) for n in nodes] == [
        ("node08", 40, 56), ("node11", 0, 96)
    ]
