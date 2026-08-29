from vaspsolkit.config import KitConfig
from vaspsolkit.scheduler import SlurmNodeInfo
from vaspsolkit.submission_resources import prompt_submission_resources, resource_cli_argv, resources_from_config


class Scheduler:
    def inspect_partitions(self): return ["compute", "long"]
    def inspect_nodes(self, partition):
        name = "node11" if partition == "compute" else "zx01"
        return [SlurmNodeInfo(name, partition, "idle", 96, 0, 96, 0)]


def test_default_resources_use_compute_without_node():
    request = resources_from_config(KitConfig())
    assert (request.partition, request.allocation, request.nodes) == ("compute", "auto", ())
    assert request.tasks == request.tasks_per_node == 96


def test_partition_is_selected_before_explicit_node():
    answers = iter(["3", "compute", "node11", "96", "n"])
    request = prompt_submission_resources(KitConfig(), input_fn=lambda _: next(answers), output=lambda _: None, scheduler_factory=lambda _: Scheduler())
    assert request.partition == "compute" and request.nodes == ("node11",) and request.tasks == 96


def test_node_outside_partition_restarts_then_cancels():
    answers, output = iter(["3", "compute", "zx01", "0"]), []
    request = prompt_submission_resources(KitConfig(), input_fn=lambda _: next(answers), output=output.append, scheduler_factory=lambda _: Scheduler())
    assert request is None and any("不属于分区 compute" in line for line in output)


def test_insufficient_idle_cores_restarts():
    class Busy(Scheduler):
        def inspect_nodes(self, partition): return [SlurmNodeInfo("node11", partition, "mixed", 96, 80, 16, 0)]
    answers, output = iter(["3", "compute", "node11", "32", "0"]), []
    request = prompt_submission_resources(KitConfig(), input_fn=lambda _: next(answers), output=output.append, scheduler_factory=lambda _: Busy())
    assert request is None and any("空闲核心数 16" in line for line in output)


def test_resource_cli_argv_uses_slurm_names():
    answers = iter(["3", "compute", "node11", "48", "y"])
    request = prompt_submission_resources(KitConfig(), input_fn=lambda _: next(answers), output=lambda _: None, scheduler_factory=lambda _: Scheduler())
    assert resource_cli_argv(request) == ["--resource-allocation", "specified", "--resource-partition", "compute", "--resource-node", "node11", "--resource-node-count", "1", "--resource-tasks", "48", "--resource-tasks-per-node", "48", "--save-resources"]
