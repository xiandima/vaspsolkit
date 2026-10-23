import copy

import pytest

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


def test_auto_allocation_selects_local_partition_without_inspecting_nodes():
    class OtherCluster(Scheduler):
        def inspect_partitions(self):
            return ["cpu", "long"]

        def inspect_nodes(self, partition):
            pytest.fail("automatic allocation should let SLURM select nodes")

    config = KitConfig()
    config.scheduler.nodes = ["previous-node"]
    before = copy.deepcopy(config)
    answers = iter(["2", "cpu", "48", "y"])
    request = prompt_submission_resources(
        config, input_fn=lambda _: next(answers), output=lambda _: None,
        scheduler_factory=lambda _: OtherCluster(),
    )
    assert request.partition == "cpu" and request.nodes == ()
    assert request.tasks == request.tasks_per_node == 48
    assert request.allocation == "auto" and request.persist
    assert config == before
    argv = resource_cli_argv(request)
    assert argv[argv.index("--resource-partition") + 1] == "cpu"
    assert "--resource-node" not in argv


def test_auto_allocation_accepts_existing_partition_as_default():
    answers = iter(["2", "", "96", "n"])
    request = prompt_submission_resources(
        KitConfig(), input_fn=lambda _: next(answers), output=lambda _: None,
        scheduler_factory=lambda _: Scheduler(),
    )
    assert request.partition == "compute" and not request.persist


@pytest.mark.parametrize("failure", ["unavailable", "invalid", "empty"])
def test_auto_partition_failure_can_cancel_without_changing_configuration(failure):
    class Unavailable(Scheduler):
        def inspect_partitions(self):
            if failure == "unavailable":
                raise RuntimeError("sinfo unavailable")
            return [] if failure == "empty" else ["cpu"]

    config = KitConfig()
    before = copy.deepcopy(config)
    answers = iter(["2", "compute", "0"] if failure == "invalid" else ["2", "0"])
    output = []
    request = prompt_submission_resources(
        config, input_fn=lambda _: next(answers), output=output.append,
        scheduler_factory=lambda _: Unavailable(),
    )
    assert request is None
    assert config == before
    expected = {"unavailable": "sinfo unavailable", "invalid": "未知 SLURM 分区", "empty": "未查询到 SLURM 分区"}[failure]
    assert any("资源配置失败" in line and expected in line for line in output)


def test_auto_allocation_keeps_configured_multinode_layout():
    config = KitConfig()
    config.scheduler.node_count = 2
    config.scheduler.tasks_per_node = 48
    answers = iter(["2", "long", "96", "n"])
    request = prompt_submission_resources(
        config, input_fn=lambda _: next(answers), output=lambda _: None,
        scheduler_factory=lambda _: Scheduler(),
    )
    assert request.partition == "long" and request.nodes == ()
    assert (request.node_count, request.tasks, request.tasks_per_node) == (2, 96, 48)


@pytest.mark.parametrize("choice", ["0", "1"])
def test_cancel_or_keep_current_resources_does_not_query_partitions(choice):
    def no_scheduler(_):
        pytest.fail("keeping or cancelling a preview must not query the scheduler")

    request = prompt_submission_resources(
        KitConfig(), input_fn=lambda _: choice, output=lambda _: None,
        scheduler_factory=no_scheduler,
    )
    if choice == "0":
        assert request is None
    else:
        assert request == resources_from_config(KitConfig())
