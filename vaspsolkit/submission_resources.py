"""Interactive, side-effect-free resource review for one submission."""
from __future__ import annotations

from typing import Callable, List, Optional

from .config import KitConfig
from .operations.actions import ResourceRequest
from .scheduler import scheduler_from_config


InputFn = Callable[[str], str]
OutputFn = Callable[[str], object]


def resources_from_config(
    config: KitConfig, *, persist: bool = False
) -> ResourceRequest:
    return ResourceRequest.create(
        allocation="specified" if config.scheduler.nodes else "auto",
        nodes=tuple(config.scheduler.nodes),
        cores=config.scheduler.cores,
        queue=config.scheduler.queue,
        walltime=config.scheduler.walltime,
        script=config.scheduler.script,
        persist=persist,
    )


def _positive_int(prompt: str, input_fn: InputFn) -> int:
    raw = input_fn(prompt).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("核心数必须是正整数") from exc
    if value <= 0:
        raise ValueError("核心数必须是正整数")
    return value


def _render_current(config: KitConfig, output: OutputFn) -> None:
    scheduler = config.scheduler
    output("提交资源配置")
    output("")
    output("当前配置")
    output(f"  调度器：{scheduler.kind.upper()}")
    output(f"  队列：{scheduler.queue or '集群默认'}")
    output(f"  节点策略：{'指定节点' if scheduler.nodes else '自动分配'}")
    output(f"  节点：{','.join(scheduler.nodes) if scheduler.nodes else '自动'}")
    output(f"  核心数：{scheduler.cores}")
    output(f"  Walltime：{scheduler.walltime}")
    output(f"  提交脚本：{scheduler.script}")


def _specified_resources(
    config: KitConfig,
    *,
    input_fn: InputFn,
    output: OutputFn,
    scheduler_factory,
) -> ResourceRequest:
    if config.scheduler.kind != "pbs":
        raise ValueError("指定节点当前只支持 PBS")
    scheduler = scheduler_factory(config.scheduler)
    nodes = scheduler.inspect_nodes(
        min_node=config.workflow.qsub_min_node,
        ppn=config.scheduler.cores,
    )
    output("PBS 节点：")
    for item in nodes:
        output(
            f"  {item.name}: state={item.state} total={item.total_cores} "
            f"used={item.used_cores} free={item.free_cores}"
        )
    selected_name = input_fn("节点名 >> ").strip()
    selected = next((item for item in nodes if item.name == selected_name), None)
    if selected is None:
        raise ValueError(f"节点不在本次 PBS 查询结果中：{selected_name}")
    state = selected.state.lower()
    if "down" in state or "offline" in state or "unknown" in state:
        raise ValueError(f"节点当前不可用：{selected_name}")
    cores = _positive_int("核心数 >> ", input_fn)
    if selected.free_cores < cores:
        raise ValueError(
            f"节点 {selected_name} 空闲核心数 {selected.free_cores} 小于请求值 {cores}"
        )
    persist = (
        input_fn("是否保存为当前 Case 默认配置？[y/N] ").strip().lower()
        == "y"
    )
    return ResourceRequest.create(
        allocation="specified",
        nodes=(selected_name,),
        cores=cores,
        queue=config.scheduler.queue,
        walltime=config.scheduler.walltime,
        script=config.scheduler.script,
        persist=persist,
    )


def prompt_submission_resources(
    config: KitConfig,
    *,
    input_fn: InputFn = input,
    output: OutputFn = print,
    scheduler_factory=scheduler_from_config,
) -> Optional[ResourceRequest]:
    while True:
        _render_current(config, output)
        output("")
        output("1. 使用以上配置")
        output("2. 自动分配节点，重新设置核心数")
        if config.scheduler.kind == "pbs":
            output("3. 指定节点，重新设置核心数")
        output("0. 取消提交")
        choice = input_fn("选择资源配置 >> ").strip()
        if choice == "0":
            return None
        if choice == "1":
            return resources_from_config(config)
        try:
            if choice == "2":
                cores = _positive_int("核心数 >> ", input_fn)
                persist = (
                    input_fn("是否保存为当前 Case 默认配置？[y/N] ")
                    .strip()
                    .lower()
                    == "y"
                )
                return ResourceRequest.create(
                    allocation="auto",
                    nodes=(),
                    cores=cores,
                    queue=config.scheduler.queue,
                    walltime=config.scheduler.walltime,
                    script=config.scheduler.script,
                    persist=persist,
                )
            if choice == "3":
                return _specified_resources(
                    config,
                    input_fn=input_fn,
                    output=output,
                    scheduler_factory=scheduler_factory,
                )
            raise ValueError("未知或当前不可用的资源选项")
        except (OSError, RuntimeError, ValueError) as exc:
            output(f"资源配置失败：{exc}")
            output("请重新选择资源配置。")


def resource_cli_argv(resources: ResourceRequest) -> List[str]:
    argv = [
        "--resource-allocation",
        resources.allocation,
    ]
    for node in resources.nodes:
        argv.extend(["--resource-node", node])
    argv.extend(["--resource-cores", str(resources.cores)])
    if resources.persist:
        argv.append("--save-resources")
    return argv
