# VASPsolKit

VASPsolKit 是面向 VASP/VASPsol 恒电势带电扫描的状态驱动终端工作流。它把中性结构优化、
带电点准备、SLURM 提交、收敛检查、结果收集和五点 E-U 拟合组织为可恢复的 Case。

当前 `0.3.0` 版本内置支持 **SLURM**，并保留 custom scheduler 接口。内置 PBS 支持已经
移除。任务提交后程序立即返回，不会在前台等待 VASP 计算结束。

> **Alpha 软件。** 本项目不提供 VASP、VASPsol、POTCAR 或机构专属模块配置。首次使用
> 请先用小体系验证 INCAR、赝势、VASPsol 版本和 SLURM 资源参数。

## 安装

需要 Python 3.9 或更高版本：

```bash
python -m pip install .

# 开发环境
python -m pip install -e ".[dev]"
```

无外网集群可以构建 wheel 后离线安装：

```bash
python tools/build_release.py --outdir /tmp/vaspsolkit-release
python -m pip install --no-index --no-deps /tmp/vaspsolkit-release/vaspsolkit-*.whl
```

## Case 输入

每个体系使用独立目录：

```text
my-case/
├── POSCAR
├── INCAR
├── KPOINTS
├── POTCAR
└── vasp.slurm
```

- `POSCAR` 是中性优化的初始结构。
- `POTCAR` 顺序必须与 `POSCAR` 一致；不要将 POTCAR 提交到公开仓库。
- `INCAR` 以用户输入为准。程序展示并确认关键参数，不会用固定模板覆盖整个文件。
- 中性点默认 `ISTART=0, ICHARG=2`。
- 带电点读取中性 `CHGCAR`，默认 `ISTART=0, ICHARG=1`。
- 默认不复制、不读取 `WAVECAR`；只有用户明确启用时才允许使用。

## SLURM 默认值

```text
partition       compute
nodes           1
tasks           96
tasks-per-node  96
walltime        72:00:00
script          vasp.slurm
launcher        mpirun
executable      vasp_std
```

默认运行命令等价于 `mpirun -np 96 vasp_std`。便携模板位于
[`templates/slurm.vasp.sh`](templates/slurm.vasp.sh)，不包含本机模块路径、固定节点或
私有队列，请按服务器环境补充模块加载命令。

## 快速开始

```bash
cd /path/to/my-case
vaspsolkit

# 或显式指定目录
vaspsolkit menu --workdir /path/to/my-case
```

新手通常只需反复选择 `02` 执行推荐下一步。程序只查询当前 Case 状态文件中记录的 Job
ID，不会扫描或认领用户全局队列，也不会在启动时自动提交或取消任务。

## 资源选择

提交前有两种分配模式：

1. **自动分配节点**：默认使用 `compute` 分区，由 SLURM 选择节点。
2. **指定节点**：先选择分区，再通过 `sinfo` 列出该分区内节点，最后选择具体节点。

指定节点必须属于已选分区。提交通过 `sbatch` 显式传递 `--partition`、`--nodes`、
`--ntasks`、`--ntasks-per-node` 和 `--time`；指定节点时再传递 `--nodelist`。

资源修改默认只影响本次提交，确认保存后才写入 `vaspsolkit.json`。提交必须精确输入
`SUBMIT`，取消必须精确输入 `CANCEL`。

```bash
vaspsolkit configure-scheduler --workdir /path/to/my-case
```

该命令遵循“分区 -> 分区内节点 -> tasks -> walltime”的顺序。若登录节点无法连接 SLURM
控制器，命令会失败并保留原配置，不会猜测节点状态。

## 标准流程

### 1. 初始化

```bash
vaspsolkit init --workdir /path/to/my-case
vaspsolkit configure-reference --workdir /path/to/my-case
```

初始化检查 VASP 输入、提交脚本、POTCAR 元素顺序和关键 INCAR 参数，并生成
`vaspsolkit.json`。SHE reference 必须显式确认并记录来源。

### 2. 中性计算

```bash
vaspsolkit prepare-neutral --workdir /path/to/my-case
vaspsolkit submit-neutral --workdir /path/to/my-case
vaspsolkit monitor --workdir /path/to/my-case
vaspsolkit check-neutral --workdir /path/to/my-case
```

提交成功后保存 SLURM Job ID 并立即返回。中性任务需要得到可用的 `CONTCAR` 和
`CHGCAR`，后者作为带电点默认初猜。

### 3. 五点带电扫描

```bash
vaspsolkit prepare-charge --workdir /path/to/my-case
vaspsolkit check-prepared --workdir /path/to/my-case
vaspsolkit submit-selected --workdir /path/to/my-case 1 2 3 4 5
```

默认偏移为 `[-1.0, -0.5, 0.0, 0.5, 1.0]`，必须恰好包含一个中性点。拟合不会静默
删除中性点，也不会自动退化为四点拟合。

### 4. 检查与修复

```bash
vaspsolkit status --workdir /path/to/my-case
vaspsolkit check --workdir /path/to/my-case
vaspsolkit repair --workdir /path/to/my-case
```

状态查询先使用 `squeue`，已离开队列的任务再使用 `sacct` 查询历史状态和 ExitCode。
SLURM 查询失败时状态为 `UNKNOWN`，程序不会因此重复提交。`repair` 只生成差异预览，
确认后才修改输入；默认恢复顺序是中性 `CHGCAR`，然后全新电荷密度，不会自动启用
`WAVECAR`。

### 5. 收集与后处理

```bash
vaspsolkit collect --workdir /path/to/my-case
vaspsolkit audit --workdir /path/to/my-case
vaspsolkit postprocess \
  --summary /path/to/my-case/results/summary.csv \
  --output /path/to/my-case/results
```

后处理生成 `summary.csv`、`analysis.json`、质量报告、需重算点清单、E-U 数据、二次拟合图
和 Markdown 报告，并给出 `U0/PZC`、二次系数、R2、RMSE 和最大残差。

## Reaction Spec

反应层独立于通用 VASP I/O。受限表达式解析器可以组合多个体系的 `analysis.json`，不会
使用 `eval`，也不会在核心代码中硬编码特定反应：

```bash
vaspsolkit reaction --spec examples/reaction-spec.example.json
```

例如可表达 `E("*N2O2", U) - E("*NO", U) - G_NO`。

## 配置迁移

当前格式为 `config_version: 2`：

```bash
vaspsolkit migrate --config /path/to/vaspsolkit.json
```

- 旧 SLURM v1 配置可以迁移到 v2，写入前显示差异并采用原子替换。
- 旧 PBS 配置不会自动翻译，因为资源和节点语义无法可靠一一对应。
- 遇到 PBS 配置时，请导入或填写当前服务器的 SLURM profile 后重新初始化调度部分。

## Custom scheduler

非 SLURM 系统可使用 `kind: custom`，配置提交、查询、取消命令模板和 Job ID 正则。custom
模式不具备 SLURM 分区与节点检查能力，提交前仍需显式确认。

## Durable submission 屏障

程序在调用 `sbatch` 前保存提交意图，收到合法数字 Job ID 后记录接受状态，再原子更新
Case。若出现 `SUBMITTING`、`ACCEPTED`、`SUBMIT_UNKNOWN` 或回执损坏：

1. 不要再次提交；
2. 使用 `vaspsolkit menu` 查看恢复屏障；
3. 人工通过 `squeue`/`sacct` 核对真实 Job ID；
4. 使用恢复操作录入 Job ID，或确认未创建任务。

## 开发验证

```bash
python -m pytest -q
python -m compileall -q vaspsolkit tests
python -m build --wheel --no-isolation
```

活动记录默认位于 `${XDG_STATE_HOME:-~/.local/state}/vaspsolkit/cases`。CI 或只读 HOME 可
通过 `VASPSOLKIT_STATE_ROOT=/path/to/state` 指定隔离目录。

贡献前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md) 和
[公开发布范围](docs/maintainer/PUBLIC_RELEASE_SCOPE.md)。

## License

[MIT](LICENSE)
