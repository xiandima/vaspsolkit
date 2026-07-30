from __future__ import annotations


def test_current_resources_are_rendered_and_returned_unchanged() -> None:
    from vaspsolkit.config import KitConfig, SchedulerConfig
    from vaspsolkit.submission_resources import prompt_submission_resources

    config = KitConfig(
        scheduler=SchedulerConfig(
            kind="pbs",
            queue="normal",
            cores=48,
            walltime="48:00:00",
            script="vasp.pbs",
            nodes=["compute-a.example.org"],
        )
    )
    output = []
    selected = prompt_submission_resources(
        config,
        input_fn=lambda prompt: "1",
        output=output.append,
    )

    assert selected.allocation == "specified"
    assert selected.nodes == ("compute-a.example.org",)
    assert selected.cores == 48
    assert selected.persist is False
    text = "\n".join(output)
    assert "compute-a.example.org" in text
    assert "核心数：48" in text
    assert "队列：normal" in text
    assert "Walltime：48:00:00" in text


def test_auto_allocation_uses_new_cores_and_optional_persistence() -> None:
    from vaspsolkit.config import KitConfig
    from vaspsolkit.submission_resources import prompt_submission_resources

    answers = iter(["2", "32", "y"])
    selected = prompt_submission_resources(
        KitConfig(),
        input_fn=lambda prompt: next(answers),
        output=lambda value: None,
    )

    assert selected.allocation == "auto"
    assert selected.nodes == ()
    assert selected.cores == 32
    assert selected.persist is True


def test_specified_node_is_discovered_and_free_cores_are_validated() -> None:
    from vaspsolkit.config import KitConfig
    from vaspsolkit.scheduler import PBSNodeInfo
    from vaspsolkit.submission_resources import prompt_submission_resources

    class Scheduler:
        def inspect_nodes(self, min_node=0, ppn=48):
            return [PBSNodeInfo("compute-a.example.org", "free", 48, 0, 48)]

    answers = iter(["3", "compute-a.example.org", "40", "n"])
    selected = prompt_submission_resources(
        KitConfig(),
        input_fn=lambda prompt: next(answers),
        output=lambda value: None,
        scheduler_factory=lambda config: Scheduler(),
    )

    assert selected.nodes == ("compute-a.example.org",)
    assert selected.cores == 40
    assert selected.persist is False


def test_insufficient_free_cores_returns_to_resource_selection() -> None:
    from vaspsolkit.config import KitConfig
    from vaspsolkit.scheduler import PBSNodeInfo
    from vaspsolkit.submission_resources import prompt_submission_resources

    class Scheduler:
        def inspect_nodes(self, min_node=0, ppn=48):
            return [PBSNodeInfo("node24", "job-busy", 48, 32, 16)]

    answers = iter(["3", "node24", "32", "0"])
    output = []
    selected = prompt_submission_resources(
        KitConfig(),
        input_fn=lambda prompt: next(answers),
        output=output.append,
        scheduler_factory=lambda config: Scheduler(),
    )

    assert selected is None
    assert any("空闲核心数" in line for line in output)
    assert sum("提交资源配置" in line for line in output) == 2


def test_scheduler_failure_returns_to_resource_selection() -> None:
    from vaspsolkit.config import KitConfig
    from vaspsolkit.submission_resources import prompt_submission_resources

    class Scheduler:
        def inspect_nodes(self, min_node=0, ppn=48):
            raise RuntimeError("pbsnodes unavailable")

    answers = iter(["3", "0"])
    output = []
    selected = prompt_submission_resources(
        KitConfig(),
        input_fn=lambda prompt: next(answers),
        output=output.append,
        scheduler_factory=lambda config: Scheduler(),
    )

    assert selected is None
    assert any("pbsnodes unavailable" in line for line in output)


def test_resource_prompt_zero_cancels_without_scheduler_access() -> None:
    from vaspsolkit.config import KitConfig
    from vaspsolkit.submission_resources import prompt_submission_resources

    selected = prompt_submission_resources(
        KitConfig(),
        input_fn=lambda prompt: "0",
        output=lambda value: None,
        scheduler_factory=lambda config: (_ for _ in ()).throw(
            AssertionError("scheduler must not be created")
        ),
    )

    assert selected is None


def test_resource_request_serializes_to_explicit_cli_flags() -> None:
    from vaspsolkit.operations.actions import ResourceRequest
    from vaspsolkit.submission_resources import resource_cli_argv

    request = ResourceRequest.create(
        allocation="specified",
        nodes=("node24",),
        cores=40,
        queue="normal",
        walltime="48:00:00",
        script="vasp.pbs",
        persist=True,
    )

    assert resource_cli_argv(request) == [
        "--resource-allocation",
        "specified",
        "--resource-node",
        "node24",
        "--resource-cores",
        "40",
        "--save-resources",
    ]
