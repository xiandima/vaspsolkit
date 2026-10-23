# VASPsolKit

在 SSH 终端中，用编号菜单完成 **输入准备 → 中性计算 → 五点电荷扫描 → 收敛检查 → E-U 图与报告**。
不需要提前算好中性参考，也不需要记住整套命令；每个体系用一个独立计算目录，程序提示下一步。

适合使用 VASP/VASPsol、在 Linux 集群上计算的用户。内置支持 **SLURM**，VASPKIT 可选。
这里的工作方式是固定 `NELECT` 扫描后建立能量与电势的关系，不是运行中自动调电子数的恒电势求解器。

> 当前为 `0.4.0` 开发版，尚未发布对应正式版本。首次换服务器或计算方法，请先用小体系验证。
> VASP/VASPsol、合法赝势和服务器运行环境由你提供。

[在线用户手册](https://xiandima.github.io/vaspsolkit/) · [第一次计算](https://xiandima.github.io/vaspsolkit/quickstart.html) · [E-U 结果示例](https://xiandima.github.io/vaspsolkit/results.html)

在线手册从通过测试的 GitHub `main` 自动构建，与同次提交的程序同步；
对应提交可在[版本说明](https://xiandima.github.io/vaspsolkit/versions.html)核对。
文档源码入口见[用户手册首页](docs/index.md)。

## 1. 安装一次

需要 Python 3.9 或更新版本。服务器可联网时，在终端运行：

```bash
git clone https://github.com/xiandima/vaspsolkit.git
cd vaspsolkit
python -m venv "$HOME/.venvs/vaspsolkit"
source "$HOME/.venvs/vaspsolkit/bin/activate"
python -m pip install .
vaspsolkit --help
```

已有源码可跳过前两行，在源码目录安装。以后重新登录，只需激活同一个环境：

```bash
source "$HOME/.venvs/vaspsolkit/bin/activate"
```

服务器不能联网、不能使用 venv、需要升级或出现旧版菜单？见[安装与升级](docs/INSTALLATION.md)。
计算请在自己的计算目录运行，不要放在源码目录里。

## 2. 准备一个体系

新建一个目录，例如 `my-case`。本文把这样的独立计算目录称为 **Case**。

| 你准备什么 | 用途 |
| --- | --- |
| `POSCAR` | 初始结构，包含元素名称行；不要求已经收敛 |
| `INCAR` 或已有 INCAR 模板 | 你的计算参数；初始化时会显示关键参数并允许修改 |
| `vasp.slurm` | 当前服务器上可用的提交脚本，放在 Case 内 |
| `POTCAR` 或合法赝势库的位置 | 已有 POTCAR 直接使用；缺失时按你确认的元素变体生成 |
| `KPOINTS` 或 INCAR 中的 `KSPACING` | 二选一；都没有时由补全步骤询问采样方式 |

已有四个 VASP 输入文件的用户，目录如下，可直接进入下一步：

```text
my-case/
├── POSCAR
├── INCAR
├── POTCAR
├── KPOINTS          # 已明确设置有效 KSPACING 时可省略
└── vasp.slurm
```

只有结构、模板和脚本也能开始，不用先手算中性点：

```bash
cd "/path/to/my-case"
vaspsolkit inputs --incar-template "/path/to/INCAR.template" --save-settings
```

将示例路径换成实际路径。程序会询问缺失输入需要的信息：赝势库目录、泛函、元素变体和采样，
显示将生成的文件后再请你确认。已有输入不会被补全操作覆盖。
`--save-settings` 保存确认过的赝势等设置，下一体系可复用；结构和显式网格不会跨体系照搬。
通过菜单补全时也会询问是否保存，默认不保存。

**没有 VASPKIT 也能使用。** 默认由程序拼接确认的赝势并生成显式网格；已安装 VASPKIT 1.5.x
时可选其自动补全。具体选择和示例见[输入准备](docs/USER_GUIDE.md#输入准备)。
如果 KPOINTS 和 KSPACING 同时存在，以 KPOINTS 为准。

## 3. 打开菜单，开始计算

在 Case 目录执行：

```bash
vaspsolkit
```

第一次使用按下面的顺序操作。不必记住全部编号，通常选择 **`02` 执行推荐下一步**即可；
拟合前请另外选择 `61` 查看详细审计。

| 阶段 | 你需要做什么 | 完成后 |
| --- | --- | --- |
| 补齐输入 | 缺输入时选 `14`，按提示填写模板、赝势和采样 | 输入齐全，可以初始化 |
| 初始化 | 选 `11` 或 `02`；选调度器、确认脚本、SHE 参考值与来源、INCAR 参数 | 保存 Case 配置，不提交计算 |
| 准备中性计算 | 选 `20` 或 `02`，核对准备后的 INCAR | 生成中性任务记录 |
| 提交中性任务 | 选 `21` 或 `02`，核对资源并输入 `SUBMIT` | 显示 Job ID，返回菜单 |
| 等待与检查 | 选 `40` 看状态，结束后选 `22` 检查收敛 | 通过后允许准备五点 |
| 准备与提交五点 | 依次选 `30`、`31`、`32`，或按 `02` 的推荐操作 | 提交选定的扫描点 |
| 收集与审查 | 各点完成后选 `50` 检查，再选 `60` 收集、`61` 审计 | 查看异常点与重算建议 |
| 生成曲线 | 审查通过后选 `62` | 得到五点 E-U 图、数据和报告 |

初始化时保留已有 INCAR 的用户设置，并检查必要 VASPsol 标签；冲突会提示你处理。
后续准备步骤仍会应用对应阶段设置，请核对生成的 INCAR，尤其是自旋、优化与溶剂参数。
SHE 参考值应与你的方法一致，界面起始值不是通用常数，见[计算约定](docs/SCIENTIFIC_CONVENTIONS.md)。

### 提交前，确认自己的服务器资源

提交界面提供自动分配或指定节点：

- **自动分配**：选择分区，由 SLURM 分配节点。
- **指定节点**：先选分区，再从该分区的节点列表中选择节点。
- 核对 MPI tasks、walltime 和脚本；需要长期复用时，再确认保存为本 Case 的默认值。

初始配置是 `compute / 1 节点 / 96 tasks / 72:00:00`，**不是通用推荐值**。提交前按本机修改，
这些资源设置可能覆盖脚本中的同名 `#SBATCH` 选项。内置受管理启动方式是 `mpirun + vasp_std`；
模块、账户、内存、QOS 等放在你自己的脚本中。换服务器、使用其他启动方式或非 SLURM 系统，
先看[服务器适配](docs/CLUSTER_CONFIGURATION.md)。

### 任务很长，可以退出吗？

可以。提交后程序不等待 VASP 算完；选 `00` 退出不会取消服务器任务。下次进入**同一目录**
重新运行 `vaspsolkit`，会读取记录并继续推荐下一步。已有 Case 不要重新初始化或删除状态文件。
任务跟踪只查询当前 Case 已记录的 Job ID，不会认领你在其他目录提交的作业。

`02` 每次只执行一个动作，**不会在你退出后自动提交下一阶段**。限制了同时在途任务数
`max_inflight` 时，剩余点也需回来再次提交。状态为 `UNKNOWN` 时先核对队列，不要直接重提。

## 4. 到哪里看结果

默认目录如下；如果改过结果路径，以自己的配置为准：

```text
my-case/results/
├── summary.csv              # 各扫描点的原始汇总
├── quality_report.csv       # 61：详细质量审计
├── points_to_rerun.csv       # 61：建议检查或重算的点
└── eu/
    ├── eu_curve.png         # 62：E-U 图
    ├── eu_curve.csv         # 拟合曲线数据
    ├── analysis.json        # 原始点、系数、拟合范围与来源
    ├── report.md            # 顶点、R²、RMSE、最大残差等
    ├── quality_report.csv   # 仅基于汇总表标志的拟合检查
    └── points_to_rerun.csv  # 拟合层的排除/重算提示
```

查看顺序：**详细质量审计 → 原始五个点 → E-U 图与残差**。再次拟合会更新 `eu/` 下同名文件；
需要保留旧图时先备份，或在直接命令中指定新的输出目录。

默认电子数偏移为 `[-1.0, -0.5, 0.0, 0.5, 1.0]`，拟合必须含唯一中性点，不会静默删点。
根目录中性准备计算之外，扫描中还有独立中性点，通常总共是 **1 + 5 个任务**。
默认中性计算用全新密度，五点用中性 CHGCAR 初猜，**不读取 WAVECAR**。

使用结果前还需注意：当前电势处理取中性 LOCPOT 平面均值的最大值，不自动识别真空平台，
也不应用 `FERMI_SHIFT`；拟合顶点不自动等于 PZC，电子能量不自动等于完整 Gibbs 自由能。
应先确认你的 VASPsol 分支与[计算约定](docs/SCIENTIFIC_CONVENTIONS.md)相符。

### 多个体系的吸附能或反应曲线

每个体系先单独完成 E-U 分析，再用独立的 Reaction Spec 组合结果；通用工作流不绑定某一种反应。
修改[反应配置示例](examples/reaction-spec.example.json)中的路径、公式和常数后运行：

```bash
vaspsolkit reaction --spec "/path/to/reaction.json" --output "/path/to/reaction-results"
```

例如可使用 `E("*N2O2", U) - E("*NO", U) - G_NO`。需要先记录各体系的方法并核对共同电势范围；
如何填写与查看输出见[反应分析](docs/USER_GUIDE.md#反应分析)。

## 常用入口

| 编号 | 菜单名称 | 什么时候用 |
| --- | --- | --- |
| `02` | 执行推荐下一步 | 不确定下一步做什么 |
| `03` | 查看完整状态与诊断 | 出错、卡住或需要查看任务记录 |
| `14` | 补齐输入：模板、赝势与采样 | 缺少 INCAR、POTCAR 或采样设置 |
| `61` | 结果审计 | 拟合前检查计算质量 |
| `62` | 后处理与 E-U 分析 | 生成或重新生成五点拟合 |
| `00` | 退出 | 保留记录，下次继续 |

不想进入菜单，也可以在终端执行：

```bash
vaspsolkit next --dry-run   # 只看本地记录建议的下一步，不查询队列或提交
vaspsolkit next             # 交互执行一步；写入和提交仍会确认
vaspsolkit status          # 查询当前 Case 的任务状态
vaspsolkit monitor --watch # 持续查看，Ctrl-C 停止查看
```

批处理没有交互终端时，使用参数明确的子命令，不要把菜单答案用管道硬塞进去。
`--yes` 表示同意执行，**不是预览**；提交预览使用 `--dry-run`。

## 遇到问题与更多用法

- 命令找不到、离线安装、升级后仍是旧菜单：[安装与升级](docs/INSTALLATION.md)。
- 缺赝势、KSPACING/VASPKIT、批处理、失败任务恢复：[使用手册](docs/USER_GUIDE.md)。
- 换服务器、指定节点、自定义调度器：[服务器适配](docs/CLUSTER_CONFIGURATION.md)。
- 曲线异常、能量公式、SHE/PZC 与反应方法：[计算约定](docs/SCIENTIFIC_CONVENTIONS.md)。
- 已验证平台和当前限制：[兼容性说明](docs/COMPATIBILITY.md)。
- 报告问题或参与开发：[CONTRIBUTING](CONTRIBUTING.md)；版本变化：[CHANGELOG](CHANGELOG.md)。

不要在公开 issue 中上传 POTCAR、凭据或未公开计算数据。

## 许可

[MIT License](LICENSE)。VASP、VASPKIT 及赝势各自的授权条款不随本项目改变。
