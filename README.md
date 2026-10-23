# VASPsolKit

[在线用户手册](https://xiandima.github.io/vaspsolkit/) · [第一次计算](https://xiandima.github.io/vaspsolkit/quickstart.html) · [E-U 结果示例](https://xiandima.github.io/vaspsolkit/results.html)

> 在线手册对应 `0.4.0` 开发快照，部分功能尚未合入本分支；安装前请核对[手册版本说明](https://xiandima.github.io/vaspsolkit/versions.html)。

VASPsolKit 是面向 VASP/VASPsol 固定 NELECT 扫描与电势后处理的状态驱动终端工作流。它把中性结构优化、
带电点准备、SLURM 提交、收敛检查、结果收集和五点 E-U 拟合组织为可恢复的 Case。

当前工作树版本号为 `0.4.0`，未发布变更见 [CHANGELOG](CHANGELOG.md)，发布状态以 tag/release 为准。
内置支持 **SLURM**，并保留 custom scheduler 接口。内置 PBS 支持已经
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

离线安装需要同时准备依赖。在与目标集群兼容的 Python/操作系统环境中，先安装开发依赖，再构建当前工作树并下载依赖：

```bash
python -m pip wheel --no-deps --no-build-isolation --wheel-dir /tmp/vaspsolkit-wheelhouse .
python -m pip download --dest /tmp/vaspsolkit-wheelhouse /tmp/vaspsolkit-wheelhouse/vaspsolkit-*.whl
```

传输整个 wheelhouse 到集群后安装：

```bash
python -m pip install --no-index --find-links /path/to/wheelhouse vaspsolkit
```

`--no-deps` 安装只适用于依赖已齐全的环境。维护者脚本 `tools/build_release.py` **只打包
已提交的 HEAD**，不包含当前未提交修订，不能用它验证脏工作树的修复内容。

## Case 输入

不需要提前算好中性参考。每个体系使用独立目录，菜单入口需要以下输入：

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
- 已有 `INCAR` 且 CLI 不指定 `--profile` 时保留用户设置；必要 VASPsol 标签冲突会阻止初始化。
- 显式指定 profile 会覆盖其中的标签，再应用 `--set TAG=VALUE`，须逐项检查差异。
- 中性点默认 `ISTART=0, ICHARG=2`。
- 带电点读取中性 `CHGCAR`，默认 `ISTART=0, ICHARG=1`。
- 默认不复制、不读取 `WAVECAR`；只有用户明确启用时才允许使用。

CLI `init` 可以按 profile 生成缺失的 INCAR；交互模式下缺失 POTCAR/KPOINTS 可调用本机
VASPKIT 辅助生成，仍须配置自己的赝势库。`--yes` 不会自动解决这些缺失输入。
POTCAR 校验要求 POSCAR 包含 VASP5 元素行。缺失 ENCUT 的建议来自 POTCAR ENMAX；
保留已有 INCAR 的路径不会自动填入该建议。VASPKIT 不是内置 E-U 收集器的功函数后端。

程序自带的 INCAR profile 会补充或更新一组计算参数，其中包括 VASPsol 模型参数。使用前
必须阅读[计算约定](docs/SCIENTIFIC_CONVENTIONS.md)，并在预览中确认每一项差异；这些默认值
不是适用于所有溶剂、温度、离子强度或泛函的通用物理常数。

## SLURM 起始配置

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

这些值未经修改时会实际用于提交，并非只显示在预览中；它们不是跨服务器推荐资源。首次使用必须根据本机
`sinfo`、队列政策和 VASP 编译方式重新确认。默认运行命令等价于
`mpirun -np 96 vasp_std`；也可以保留 `compute` 自动分配或先选分区、再指定该分区中的
节点。

便携模板位于 [`templates/slurm.vasp.sh`](templates/slurm.vasp.sh)。模板本身不再硬编码
分区、节点、核数和 walltime，这些参数由 VASPsolKit 在 `sbatch` 命令中显式传入。
受管理的 SLURM profile 目前只支持 `mpirun` + `vasp_std`，不能直接改成 `srun` 或 GPU
VASP。服务器模块、账户和接口边界见[集群配置](docs/CLUSTER_CONFIGURATION.md)。

## 快速开始

```bash
cd /path/to/my-case
vaspsolkit

# 或显式指定目录
vaspsolkit menu --workdir /path/to/my-case
```

新手通常只需反复选择 `02` 执行推荐下一步。状态跟踪只查询当前 Case 状态文件中记录的 Job
ID；plan/资源查看可读取队列快照，但不会因此认领其他任务，也不会在启动时自动提交或取消任务。

## 资源选择

提交前有两种分配模式：

1. **自动分配节点**：默认使用 `compute` 分区，由 SLURM 选择节点。
2. **指定节点**：先选择分区，再通过 `sinfo` 列出该分区内节点，最后选择具体节点。

指定节点必须属于已选分区。提交通过 `sbatch` 显式传递 `--partition`、`--nodes`、
`--ntasks`、`--ntasks-per-node` 和 `--time`；指定节点时再传递 `--nodelist`。

菜单中资源修改默认只影响本次提交，另行确认保存后才写入配置。菜单提交要求精确输入
`SUBMIT`，取消要求 `CANCEL`；普通 CLI 确认一般为 `y/yes`。支持 `--yes` 的命令会跳过
确认，不能当成无害预览开关。

```bash
vaspsolkit configure-scheduler --workdir /path/to/my-case
```

该命令是指定节点配置入口，遵循“分区 -> 分区内节点 -> tasks -> walltime”的顺序，校验后
立即保存，不是菜单的自动分配选择。若登录节点无法连接 SLURM
控制器，命令会失败并保留原配置，不会猜测节点状态。

## 标准流程

以下命令分阶段执行，不是一段可连续提交到底的脚本。任务提交后返回，须等待结束并
检查通过再进入下一阶段；自定义结果目录或扫描目录名后也须调整示例路径。

### 1. 初始化

```bash
vaspsolkit init --workdir /path/to/my-case
vaspsolkit configure-reference --workdir /path/to/my-case
vaspsolkit plan --workdir /path/to/my-case
```

初始化执行文件级预检，包括必要输入、提交脚本、POTCAR 元素顺序和关键 INCAR 参数，
并生成 `vaspsolkit.json`。这不等同于技术收敛或科学有效性检查。SHE 数值要确认；来源
虽然可以在界面留空，科研使用仍应填写。`workflow.method_fingerprint` 目前须手动填写，
是经人工核对的方法记录标识，不是自动从实际输入生成的文件指纹。示例将它留空，不能
用占位字符串冒充核验结果，具体记录项目见[计算约定](docs/SCIENTIFIC_CONVENTIONS.md)。

### 2. 中性计算

```bash
vaspsolkit prepare-neutral --workdir /path/to/my-case
vaspsolkit submit-neutral --workdir /path/to/my-case --dry-run
vaspsolkit submit-neutral --workdir /path/to/my-case
vaspsolkit monitor --workdir /path/to/my-case
vaspsolkit check-neutral --workdir /path/to/my-case
```

`monitor` 默认只刷新一次，加 `--watch` 可持续查看。提交后须等待结束，再执行
`check-neutral`。中性任务需要可用的 `OUTCAR/CONTCAR/CHGCAR/LOCPOT`；CHGCAR 作为
扫描点默认初猜。文件存在、正常结束、技术收敛和科学可比性是不同层次。

### 3. 五点带电扫描

```bash
vaspsolkit prepare-charge --workdir /path/to/my-case
vaspsolkit check-prepared --workdir /path/to/my-case
vaspsolkit submit-selected --workdir /path/to/my-case 1 2 3 4 5 --dry-run
vaspsolkit submit-selected --workdir /path/to/my-case 1 2 3 4 5
```

默认偏移为 `[-1.0, -0.5, 0.0, 0.5, 1.0]`，必须恰好包含一个中性点。拟合不会静默
删除中性点，也不会自动退化为四点拟合。

默认扫描目录 `3` 是中性点，根目录中性准备任务不替代它，通常共需 **1+5 个任务**。
五点均默认从中性 CHGCAR 启动。默认 charge-relax 会优化各点结构，拟合前要人工核对
构型和磁态连续性。设置 `max_inflight` 时一次仅提交可容纳的批次，后续需再次操作。

### 4. 检查与修复

```bash
vaspsolkit status --workdir /path/to/my-case
vaspsolkit check --workdir /path/to/my-case
vaspsolkit repair --workdir /path/to/my-case --job 3 --no-submit
```

状态查询先使用 `squeue`，已离开队列的任务再使用 `sacct` 查询历史状态和 ExitCode。
SLURM 查询失败时状态为 `UNKNOWN`，程序不会因此重复提交。`repair` 展示差异并询问，
拒绝才是仅预览。上例确认后会归档旧输出、修改输入，但不提交；省略 `--no-submit` 会
尝试提交可就绪的批次，可能不止修复的一个点，须查看任务列表。默认先尝试中性 CHGCAR，
再尝试全新电荷密度，不自动启用 WAVECAR。

### 5. 收集与后处理

```bash
vaspsolkit collect --workdir /path/to/my-case
vaspsolkit audit --workdir /path/to/my-case
vaspsolkit postprocess \
  --summary /path/to/my-case/results/summary.csv \
  --output /path/to/my-case/results/eu
```

`collect` 生成 `results/summary.csv`，`audit` 重新收集并生成详细的
`quality_report.csv/points_to_rerun.csv`。先审查它们，再拟合。

`postprocess` 在 `results/eu/` 生成 `analysis.json`、`eu_curve.csv`、`eu_curve.png`、
`report.md` 和仅按 CSV 纳入/收敛标志生成的简要质量报告；它不复制或生成 summary.csv，
也不重新检查 OUTCAR。**不要与 audit 使用同一输出目录，否则详细审计会被同名简表覆盖。**
重复使用同一输出目录也会覆盖旧图；菜单版本化后处理则保存到 `results/history/<run-id>/`，
附带 summary 副本、SHA256 和分析记录。

报告包含二次顶点 `U_vertex`、另一个 `q(U)=0` 线性拟合估计、系数、R2、RMSE 和最大残差。
二次顶点不无条件等于 PZC。默认五点门槛要求唯一中性点且全部纳入；显式
`--allow-excluded` 仅用于诊断，图例和报告注明数量与排除目录，不满足五点验收。
低层 `analyze` 没有同样的五点检查，不能替代此步骤。缺失质量标志的手工 CSV 可能按
默认值纳入，也不能因此当成已验证数据。

当前内置收集器采用：

```text
V_ref    = max(planar_average(neutral_LOCPOT))
U_vs_SHE = V_ref - E_Fermi - E_abs(SHE)
E(U)     = TOTEN - delta_electrons * E_Fermi
```

它记录 `fermi_shift_handling=not-applied`，不自动实现原始 VASPsol 输出中的
`FERMI_SHIFT` 对齐。若所用 VASPsol 版本要求该修正，不得把当前结果直接解释为已完成修正；
应使用独立、可追溯的处理分支。当前通用 CSV 后处理会固定写入默认约定标签，并非任意
外部校正数据的安全导入器。平面均值最大值也不等于已经识别真空平台；这些限制、
TOTEN 与自由能的区别见[计算约定](docs/SCIENTIFIC_CONVENTIONS.md)。

## Reaction Spec

反应层独立于通用 VASP I/O。受限表达式解析器可以组合多个体系的 `analysis.json`，不会
使用 `eval`，也不会在核心代码中硬编码特定反应：

```bash
vaspsolkit reaction \
  --spec /path/to/reaction.json \
  --output /path/to/reaction-results
```

例如可表达 `E("*N2O2", U) - E("*NO", U) - G_NO`。

先修改[示例](examples/reaction-spec.example.json)的源文件路径、常数和网格；路径相对于 spec
文件，示例不附真实结果。输出 `reaction_curves.csv/png` 和 `reaction_report.md`。

reaction 默认要求所有 `analysis.json` 具有一致的 SHE reference、potential convention、
energy convention、FERMI_SHIFT handling 和 `method_fingerprint`，并要求绘图区间位于各体系
共同拟合范围内。旧数据只能通过 `compatibility.allow_unknown_provenance=true` 显式放行；
外推也必须通过 `compatibility.allow_extrapolation=true` 显式确认。默认纵轴称为 electronic
energy expression；只有明确加入 ZPE、熵及其他自由能修正后，才应改称 Gibbs free energy。
相同字符串不证明实际输入一致。启用豁免时报告明确记录开关、输入、常数和公式，不再
标记为严格检查通过。化学计量、单位和标准态仍需人工验证，见[反应边界](docs/SCIENTIFIC_CONVENTIONS.md#反应层边界)。

## 配置迁移

当前格式为 `config_version: 2`：

```bash
vaspsolkit migrate --input /path/to/old-slurm.json --output /path/to/new-vaspsolkit.json
```

- 旧 SLURM v1 配置可以迁移到 v2，写入前显示差异并采用原子替换。
- 覆盖另一个已存在的目标须加 `--force`；原地迁移仍需确认。
- 旧 PBS 配置不会自动翻译，因为资源和节点语义无法可靠一一对应。
- 遇到 PBS 配置时，请导入或填写当前服务器的 SLURM profile 后重新初始化调度部分。

## Custom scheduler

非 SLURM 系统可使用 `kind: custom`，配置提交、查询、取消命令模板和 Job ID 正则。custom
模式不具备 SLURM 分区与节点检查能力，也不承诺整套 SLURM 菜单可直接复用；它是低层
命令适配接口，提交前仍需显式确认，详见[集群配置](docs/CLUSTER_CONFIGURATION.md)。

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
通过 `VASPSOLKIT_STATE_ROOT=/path/to/state` 指定隔离目录。测试自行覆盖部分默认路径，
运行整套测试时不宜全局强设该变量。

贡献前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md) 和
[公开发布范围](docs/maintainer/PUBLIC_RELEASE_SCOPE.md)。本轮问题与修正对应见[文档审查记录](docs/DOCUMENTATION_AUDIT.md)。

## License

[MIT](LICENSE)

Repository: [github.com/xiandima/vaspsolkit](https://github.com/xiandima/vaspsolkit)
