# SLURM 集群配置

本页描述实际实现，不把可存储的配置字段等同于已支持的提交能力。首次使用应提供本机
已验证的 SLURM 脚本，并先核对队列政策、MPI/VASP 编译环境与许可证；公共仓库不保存
机构专属模块路径、账户或计算数据。

## 支持范围

| 项目 | 当前行为 |
| --- | --- |
| 分区、节点数、总 tasks、每节点 tasks、walltime | 受管理提交显式传给 sbatch |
| 自动分配 | 默认 compute 分区，不绑定节点；分区名可修改 |
| 指定节点 | 先分区，再选择该分区内节点，传入 --nodelist |
| MPI/VASP | 受管理 profile 仅支持 mpirun + vasp_std |
| account、qos、GPU 等 | 没有通用的对应向导；在本机脚本中明确配置并独立验证 |
| memory 字段 | JSON 可存储，但当前 SlurmScheduler.submit 不将它转成 --mem；实际内存请求须写入脚本 |
| module_init/modules | 不能替代脚本里的环境初始化，须核对脚本正文 |
| PBS | 内置实现已移除，不自动转换旧 PBS 资源 |

起始值 compute、1 节点、96 tasks、96 tasks/node、72:00:00 **未经修改就会用于提交**。
它们不是跨集群推荐资源。tasks 指 MPI ranks，不等于 GPU 数或 OpenMP 线程数；请按本机
节点容量及 VASP 并行约定设置。不要只把 launcher 字段换成 srun 后就认为已适配。

## 本机脚本

[公共模板](../templates/slurm.vasp.sh)是起点，不含资源头或模块加载，需要在服务器上补齐。
受管理提交会通过 sbatch 显式传资源，脚本资源同步也可能写入对应 `#SBATCH` 行。
原有 account/qos、环境加载及未管理的头部选项应保留，但仍须阅读差异，不保证任意复杂
脚本都可自动理解。脚本采用唯一、明确的启动行：

```bash
mpirun -np "${SLURM_NTASKS}" vasp_std > vasp.log 2>&1
```

不要把该行改成动态可执行文件变量或多重启动分支后继续依赖资源自动改写；不被识别的
启动行可能保留并另添启动命令。确认预览中只有一次 VASP 启动。已修正公共模板，使其
资源同步后保持唯一启动行。替代可执行文件或复杂 launcher 需要单独适配，不是内置功能。

`#SBATCH --output/--error` 收集 SLURM 作业 shell 的输出；上述重定向将 VASP stdout/stderr
合并写入 vasp.log，并非独立的 VASP stderr 文件。环境模块须与 mpirun 和 vasp_std 匹配。
直接 sbatch 模板属于绕过工作流的提交，不能获得 Case 状态与确认屏障，不作为快速入门。

## 选择资源

菜单提交时可选自动或指定。指定策略先选分区，再查看分区内节点；资源预检会参考当前
空闲核心，因此可能拒绝暂时没有足够空闲核心的选择。快照不是节点预留，也不保证立即运行。

```bash
vaspsolkit configure-scheduler --workdir /path/to/my-case
vaspsolkit plan --workdir /path/to/my-case
```

CLI `configure-scheduler` 是指定节点配置入口，验证成功后立即写入 JSON；没有留空节点
即可切换自动模式的承诺。自动分配可在菜单选择，也可使用提交资源参数，例如先预览：

```bash
vaspsolkit submit-neutral --workdir /path/to/my-case \
  --resource-allocation auto --resource-partition compute \
  --resource-node-count 1 --resource-tasks 96 --resource-tasks-per-node 96 --dry-run
```

上述数字仅演示参数；先核对本机需求再取消 dry-run。时限仍取已保存配置，不会因修改
tasks 而自动匹配本机政策。`--save-resources` 可将本次资源选择保存，须先审核。
`submit-selected` 提供同组资源参数；不要把它们套在不支持这些参数的低层 `submit` 命令上。

## 排队与状态

建议显式设置有限的 `scheduler.max_inflight`。默认 null 不做该层并发限制；一次操作
可能提交全部已选且就绪的任务。设置上限后需按当前 Case 活跃任务分批操作，它不是全局
用户配额管理器，也不是后台自动补交服务。

菜单状态跟踪只跟随当前 Case 已记录的 Job ID；plan/资源查看可读取队列快照，但不会
因此认领其他任务。squeue 离队后用 sacct 确认终态与 ExitCode，再独立检查 VASP 输出。
UNKNOWN、回执不确定、查询失败均不能作为重复提交的依据。资源 dry-run 不启动 VASP；
本地 fixture 测试通过也不代表真实集群已经验收。

## Custom 适配边界

`kind: custom` 是低层命令适配器，不是已经打通的另一套交互资源菜单。命令以参数数组
配置，不隐式经 shell 展开；唯一内置占位符为 `{script}`、`{job_id}`。例如 scheduler 部分：

```json
{
  "kind": "custom",
  "script": "submit.sh",
  "submit_command": ["site-submit", "{script}"],
  "status_command": ["site-status", "{job_id}"],
  "cancel_command": ["site-cancel", "{job_id}"],
  "inspect_command": ["site-queue"],
  "job_id_pattern": "JobID=(?P<job_id>[0-9]+)"
}
```

这些 site-* 是须自行实现并验证的包装程序，不是随包提供的命令。status 的第一行须返回
工作流可识别的单一状态（如 RUNNING、COMPLETED），不能直接返回任意队列表；空输出或
失败按未知处理。inspect 只保存原始文本，不具备结构化分区/节点容量检查。
需要测试提交回执、状态映射、取消和异常恢复后再使用；不能只改 kind 就宣称全流程适配。
