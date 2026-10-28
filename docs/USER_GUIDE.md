# 使用手册（合订查询版）

[手册首页](index.md) · [安装与升级](INSTALLATION.md) · [服务器适配](CLUSTER_CONFIGURATION.md)

第一次使用请按[第一次计算](quickstart.md)操作。本页汇集输入、直接命令、反应分析和恢复说明，
与各专题页共用正文，供集中查询，不是另一套入门路线。
以下命令均在独立 Case 目录执行；也可用 `--workdir "/path/to/my case"` 指定目录。
命令中的示例路径、资源和物理参数应换成自己的值。

不切换目录时，也可以指定 Case 打开菜单：

```bash
vaspsolkit menu --workdir "/path/to/my case"
```

按需要阅读：[输入准备](#输入准备)、[初始化](#初始化时确认什么)、[直接命令流程](#直接命令流程)、
[反应分析](#反应分析)、[问题处理](#遇到问题)、[旧配置迁移](#更新旧配置)。

## 输入准备

### 已有完整输入

保留 POSCAR、INCAR、POTCAR、KPOINTS 和本机提交脚本，运行 `vaspsolkit`，选择 `11` 初始化。
已有 Case 则继续，不再初始化。不需要 VASPKIT，也不需要先完成中性计算。

POSCAR 需有 VASP5 格式的元素名称行，POTCAR 顺序必须与之相同。请使用 Case 内的普通文件；
不要依赖指向 Case 外的符号链接。补全操作只生成缺失文件，不替换已有文件。

**完成信号：** 菜单 `10` 显示基础输入检查通过。新 Case 继续[初始化](inputs.md#初始化时确认什么)，
已有 Case 选 `03` 查看当前阶段。若检查报错，先修正指出的输入，不重复初始化。

### 只有结构、模板和脚本

先放好 POSCAR 和本机脚本，再运行：

```bash
vaspsolkit inputs --incar-template "/path/to/INCAR.template" --save-settings
```

INCAR 已在 Case 内时省略 `--incar-template`。依次确认：

1. 缺失的 INCAR 使用哪个模板。
2. POTCAR 缺失时，使用哪一个泛函、版本的合法赝势库。
3. 每个元素采用什么变体，例如 Ni 或 Ni_pv；不能只按名称猜测。
4. 采样使用现有 KPOINTS、模板中的 KSPACING，还是生成新的网格。
5. 预览无误后确认写入。

`--potcar-root` 应指向含 `C/POTCAR`、`Ni_pv/POTCAR` 等子目录的同一泛函/版本库，
而不是某个单独 POTCAR。不要把不同版本混在一个库中。没有授权库时，需要自行提供合法 POTCAR；
程序不会下载赝势。

菜单会询问是否保存赝势设置，直接回车表示不保存；命令行可用 `--save-settings` 明确要求保存。
默认位置是
`${XDG_CONFIG_HOME:-~/.config}/vaspsolkit/inputs.json`，也可指定 `--settings PATH`。
保存库路径、已确认的元素映射和后端等选项，不保存结构、INCAR 模板路径或显式网格。
新元素需再次确认，换服务器后需重新指定本机库路径。

生成记录在 `vaspsolkit.inputs-<id>.json`，不含 POTCAR 正文。输入生成和初始化是两个确认步骤：
生成已成功后取消初始化，不会删除已经生成的输入。

**下一步：** 核对生成文件和记录，选 `10` 检查后再初始化。若提示保存设置失败，先检查
输入是否已经生成；保存失败不等于输入生成失败，不要删除已有输入重做。

### KSPACING 或显式网格

INCAR 已明确设置有效正数 KSPACING 时，允许没有 KPOINTS；可同时设置 KGAMMA。
初始化、中性准备、五点准备和提交检查均支持这一方式，不生成占位 KPOINTS。

有 KPOINTS 时以文件为准，程序不会自动删除它。要改用 KSPACING，请在**没有活动任务**时
自行审查并移走不再使用的 KPOINTS，随后重新检查输入；无效 KPOINTS 不会自动被忽略。

没有采样设置时，可以明确提供网格：

```bash
vaspsolkit inputs --mesh 3 3 1 --centering gamma
```

这里的 `3 3 1` 仅演示语法，不是通用收敛建议。不同晶胞、表面和分子体系需自行验证采样精度。

### 使用 VASPKIT

自动补全支持已安装且 PATH 中可用的 **VASPKIT 1.5.x**：

```bash
vaspsolkit inputs --backend vaspkit --kpr 0.04 --centering gamma
```

POTCAR 缺失时仍须提供或保存赝势库、泛函及元素映射。程序只调用输入生成任务 103/102，
不会要求你再操作 VASPKIT 菜单，不运行其提交任务，也不修改你的 `~/.vaspkit`。
`--kpr` 单位为 `2*pi/Angstrom`，不能把 KSPACING 的 `1/Angstrom` 数值原样照抄。

未安装、版本不匹配或工具运行失败时，可直接改用原生后端：

```bash
vaspsolkit inputs --backend native --mesh 3 3 1 --centering gamma
```

如果模板已有 KSPACING，不需要 `--mesh`。原生后端不依赖 VASPKIT。
工具输出校验不通过时不会导入；查看报错中的元素、变体或采样问题后重试。
已存在文件不会因切换后端被重新生成。

**完成信号：** 缺失输入已经导入，基础输入检查通过。仍报元素或采样错误时，保留报错，
核对库路径与元素映射；不要通过改文件名或放宽检查掩盖不匹配。

## 初始化时确认什么

仅对没有配置和任务状态的新 Case 执行：

```bash
vaspsolkit init
```

已有 INCAR 且不指定 `--profile` 时，保留用户值并检查必要的中性 VASPsol 标签。界面列出
ENCUT、自旋、电子收敛、优化、色散、偶极和溶剂相关参数，可以输入 `TAG=VALUE` 覆盖项。
冲突或重复标签须先解决。显式 profile 会覆盖该配置档控制的参数，不等于“只补缺项”。

缺少 INCAR 且需要以程序配置档为起点时：

```bash
vaspsolkit init --profile vaspsol-neutral-relax
```

ENCUT 建议取自 POTCAR 的 ENMAX，不代替收敛测试。保留已有 INCAR 时，打印建议不代表
自动写入 ENCUT。中性和带电准备还会应用相应阶段设置，不能仅检查初始化时的模板。
默认菜单流程是结构优化加五点扫描；不能把任意静态或其他溶剂模板直接当作等价流程。

**完成信号：** `vaspsolkit.json` 和 `vaspsolkit.state.json` 已生成，此时不提交任务。
下一步是准备中性计算。若已有配置或状态，请继续原 Case，不删除记录重新初始化。

要组合多个体系时，在首次 collect 前填写[方法记录](reaction.md#先记录方法)。
`workflow.method_fingerprint` 是人工方法编号，不是程序自动验证的输入指纹。

SHE 参考值与来源应匹配实际方法。修改已保存的参考值使用：

```bash
vaspsolkit configure-reference
```

修改后重新收集和拟合。方法与默认 profile 的完整说明见[计算约定](SCIENTIFIC_CONVENTIONS.md)。

## 直接命令流程

以下各段对应不同阶段，**不要把所有命令连起来执行**。提交后等待任务结束并检查通过，
再进入下一段。`next` 只执行一个推荐动作，`run` 也不是全流程后台控制器。
从中途继续时，只执行当前阶段；每段中的 `CASE` 都应改成同一个真实计算目录。
命令中的 `--yes` 授权写入或提交，`--dry-run` 才是对应提交命令的预览。

### 新 Case：初始化和预览

交互初始化见[初始化时确认什么](inputs.md#初始化时确认什么)。无交互终端时必须明确填写参数：

```bash
CASE="/path/to/my case"
vaspsolkit init --workdir "$CASE" --yes \
  --scheduler slurm --script vasp.slurm \
  --she-reference 4.70 --she-reference-source "替换为实际参考来源"
vaspsolkit plan --workdir "$CASE" --offline
vaspsolkit prepare-neutral --workdir "$CASE" --yes
vaspsolkit submit-neutral --workdir "$CASE" --dry-run
```

示例假定输入齐全；缺 INCAR 时增加 `--incar-template "/path/to/INCAR.template"` 或明确 profile。
`4.70` 是示例值，应核对自己的标定。init / prepare 会写文件，prepare 还会归档旧输出，
这不是只读操作。`--yes` 表示授权执行，不是“只检查”。

这一段不真实提交；在输入已齐全时不需要集群命令。换服务器的用户可用它检查文件流程，
但 dry-run 不能验证账户、分区、模块、MPI 或 VASP 是否可用。
已有配置或状态的 Case 请跳过初始化，不能覆盖记录后重来。

**完成信号：** 中性任务记录为 PREPARED，提交预览显示 `neutral: DRY-RUN`，没有真实 Job ID。
核对准备后的 INCAR 与预览资源，再进入中性提交；预览失败时按提示修正输入或脚本，不直接提交。

无交互补全也可明确写出全部参数。下面假设模板已有 KSPACING：

```bash
vaspsolkit inputs --workdir "$CASE" --incar-template "/path/to/INCAR.template" \
  --potcar-root "/path/to/licensed/potpaw_PBE" --functional PBE \
  --potential C=C --potential N=N --potential Ni=Ni_pv --dry-run
```

元素映射按自己的 POSCAR 修改。预览确认后将 `--dry-run` 换成 `--yes` 执行；
需要保存复用设置再加 `--save-settings`。缺参数时会列出缺项退出，不等待输入。

### 中性提交与检查

首次真实提交前，先确认[本机资源](CLUSTER_CONFIGURATION.md)。下面的提交使用已保存的配置：
已有中性 Job ID 时，不再执行下面的 submit-neutral；直接使用本节后面的查询与检查命令。
从中途进入本节时，先将 `CASE` 设为原计算目录。

```bash
CASE="/path/to/my case"
vaspsolkit plan --workdir "$CASE"
vaspsolkit submit-neutral --workdir "$CASE"
```

**提交完成信号：** 返回 Job ID。等待作业结束后，再检查：

```bash
vaspsolkit monitor --workdir "$CASE"
vaspsolkit check-neutral --workdir "$CASE"
```

提交命令先询问确认；返回 Job ID 后等待计算完成。`monitor` 刷新一次，`monitor --watch`
持续查看。Ctrl-C 只停止查看，不取消作业。

进入五点前，中性计算须通过检查，并有可用的 OUTCAR、CONTCAR、CHGCAR、LOCPOT。
队列显示 COMPLETED 并不等于 VASP 已收敛。

**继续条件：** 输出中性状态 `CONVERGED` 后进入五点准备。QUEUED / RUNNING 继续等待；
NEEDS_REVIEW / FAILED 停止推进并检查输出；UNKNOWN 先查原 Job ID，不重提。
具体处理见[检查与恢复](recovery.md)。

### 五点准备与提交

仅在中性状态为 CONVERGED、且尚未生成扫描点时准备。已有扫描目录请先查看状态，
不要为了补交一批任务重新 prepare-charge。

```bash
CASE="/path/to/my case"
vaspsolkit prepare-charge --workdir "$CASE"
vaspsolkit check-prepared --workdir "$CASE"
vaspsolkit submit-selected --workdir "$CASE" 1 2 3 4 5 --dry-run
```

**准备完成信号：** 默认生成 `charge_sweep/1` 至 `charge_sweep/5`，输入检查通过。
核对每点 INCAR 和提交预览后，另行确认真实提交：

```bash
vaspsolkit submit-selected --workdir "$CASE" 1 2 3 4 5
```

默认目录 3 是中性扫描点；根目录中性计算不替代它。五点从中性 CHGCAR 启动，
默认 ISTART=0、ICHARG=1，不复制或读取 WAVECAR。默认各点优化结构，须核对构型和磁态是否连续。

`max_inflight` 限制当前 Case 的在途数，不是全用户配额；一次提交后不会自动补交下一批。
超出上限的所选任务保留为 PREPARED，需要再次选择提交。目录名或扫描设置改过时，以实际任务列表为准。

**提交完成信号：** 已接受的点各有 Job ID；后续只选择仍为 PREPARED 的点补交，不反复选择全部点。
检查不通过先修正对应输入；任务失败或 UNKNOWN 按[恢复章节](recovery.md)处理。

### 收敛、收集和拟合

等待各点完成后执行：

```bash
CASE="/path/to/my case"
vaspsolkit status --workdir "$CASE"
vaspsolkit check --workdir "$CASE"
```

**继续条件：** 五个点均为 CONVERGED，且原始输出可接受。然后收集与审计：

```bash
vaspsolkit collect --workdir "$CASE"
vaspsolkit audit --workdir "$CASE"
```

`02` 不会自动执行 `61`；使用推荐流程也须主动审计。
先看 `results/quality_report.csv` 的 `warnings` 列、`points_to_rerun.csv` 和各点输出。
`wrote ...` 只表示报告已写出，不表示没有警告；[核对警告与人工验收项](results.md#先读质量报告)，
确认所有点可接受后再运行：

```bash
vaspsolkit postprocess --summary "$CASE/results/summary.csv" --output "$CASE/results/eu"
```

输出和含义见[结果章节](results.md#输出文件)。不要把 postprocess 输出直接放进
`results/`，否则它的简要质量表会覆盖 audit 的详细表。要保留多个分析版本，为每次分析指定不同目录。

postprocess 只读取汇总 CSV，不重新检查 OUTCAR。默认要求五点全部纳入且有唯一中性点。
重算、修改输入或采集设置后，应重新 collect、audit 和 postprocess。菜单与 next 会检查
采集记录是否仍对应当前任务；旧版没有采集记录时，会要求先重新 collect，即使结果目录已存在。
`--allow-excluded` 只供明确排除后的诊断，报告和图显示排除信息，不满足五点验收；
低层 `analyze` 不提供相同保护，不要用它绕过检查。

**完成信号：** 输出目录包含 `analysis.json`、`eu_curve.png`、`eu_curve.csv` 和 `report.md`。
接着做[结果验收](results.md#怎么判断结果是否可用)；生成图不代表科学结论已验证。

## 反应分析

### 先记录方法

计划比较多个体系时，**在首次 collect 和拟合之前**核对各体系的实际方法，
并在各 Case 的 `vaspsolkit.json` 中填写 `workflow.method_fingerprint`，引用自己保存的方法记录编号。
例如只在已有 `workflow` 对象中补充下面的字段，不要用片段替换整个配置文件：

```json
{
  "workflow": {
    "method_fingerprint": "method-record-001"
  }
}
```

示例编号应换成你的真实记录编号。编辑配置时不要同时运行其他写入 Case 的操作。
该字段是人工声明，不是程序自动验证的哈希；相同编号不能证明实际参数相同。
按[方法记录清单](SCIENTIFIC_CONVENTIONS.md#she-与方法记录)核对 VASP/VASPsol 版本、数值设置、
溶剂、能量与电势约定。只有实际可比较的方法才使用同一编号，不为了通过检查把不同方法写成相同值。

**已有分析结果但缺编号：** 若实际方法已核对一致、只是漏填记录，补齐后重新 collect、audit、postprocess，
更新各体系的 `analysis.json`；仅补元数据不要求重跑 VASP。若实际方法不一致，须先解决计算可比性，
不能靠补编号修复。

### 准备各体系与反应配置

分别完成[五点收集与拟合](workflow.md#收敛收集和拟合)，检查每个 `analysis.json` 的
`provenance.method_fingerprint` 已记录正确编号，再复制并修改
[reaction-spec.example.json](../examples/reaction-spec.example.json)。

1. `systems` 指向各体系的 `analysis.json`；相对路径以 reaction.json 所在目录为准。
2. `constants` 填写参考能量数值，保持单位和标准态一致；来源另写在方法记录中，不放进数值字段。
3. 在 `curves` 中为每条曲线填写 `name` 和 `formula`，例如 `E("*N2O2", U) - E("*NO", U) - G_NO`。
4. `grid` 选择所有体系共同采样范围内的电势区间。

默认要求各体系的方法标识、SHE、能量/电势约定等一致，并检查共同拟合范围。
程序不替你验证气相常数、反应计量或自由能修正。使用 `E("体系名", U)`，
在 E 外组合常数和系数，不在 E 内平移或缩放 U。

### 运行并检查输出

```bash
vaspsolkit reaction --spec "/path/to/reaction.json" --output "/path/to/reaction-results"
```

**完成信号：** 输出目录生成 `reaction_curves.csv`、`reaction_curves.png` 和 `reaction_report.md`。
打开报告核对实际路径、公式、常数、来源与范围，不能仅凭图像判断反应式或单位正确。

默认不接受未知来源或越过共同拟合范围。旧数据需要重建方法记录并重新分析；
不要为了画出图就开启豁免。确需诊断性放行时，开关及影响见
[反应层边界](SCIENTIFIC_CONVENTIONS.md#反应层边界)。

常数命名为 G_NO 不会自动添加 ZPE 或熵；只有明确加入这些修正，才能称为相应的自由能曲线。

## 遇到问题

先选 `03` 查看任务状态、Job ID 和诊断。中性输出在 Case 根目录，默认扫描输出在
`charge_sweep/1` 至 `charge_sweep/5`；改过目录配置时以实际路径为准。

| 现象 | 先做什么 | 何时继续 |
| --- | --- | --- |
| 缺 POSCAR、POTCAR 或采样 | 按[输入准备](inputs.md)补缺项；已有错误文件先人工核对 | 基础输入检查通过后初始化或继续原阶段 |
| INCAR 与模板不一致 | 决定采用哪份输入；保留现有文件时省略模板参数 | 参数冲突解决后再确认 |
| KSPACING 已设置，仍报 KPOINTS 有错 | 检查现有 KPOINTS，它优先于 KSPACING，不自动回退 | 无活动任务时修正采样，再检查输入 |
| 新服务器没有 compute 分区 | 在提交资源选择中换成本机分区 | 资源与最终脚本核对无误后提交 |
| 出现旧 PBS 文案 | 按[安装排查](INSTALLATION.md#命令或版本不对)核对导入位置 | 确认使用正确安装后继续原 Case |
| UNKNOWN 或队列查询失败 | 用 squeue/sacct 核对原 Job ID | 查明原作业状态前不重提、不删记录 |
| 中性 FAILED / NEEDS_REVIEW | 运行 check-neutral，检查根目录输出 | 查明原因并制定恢复方案，不套用扫描点 repair |
| 扫描点 FAILED / NEEDS_REVIEW / BLOCKED | 查看 `03` / `50` 和该点输出 | 按[修复失败点](recovery.md#修复失败点)处理后重新检查 |
| 五点中仍有 PREPARED | 检查 max_inflight 和在途任务数 | 容量允许后只选择剩余就绪点提交 |
| 审计写出文件但有 warnings | 对照[警告说明](results.md#先读质量报告)核对原始输出 | 异常已解决或科学上已说明，再拟合 |
| 拟合拒绝五点数据 | 检查点数、中性点、收敛和纳入状态 | 修复数据后重新收集，不删中性点凑曲线 |
| reaction 报来源或范围不匹配 | 核对[方法记录](reaction.md#先记录方法)和共同电势范围 | 更新分析结果或缩小到实际共同区间后重试 |
| 当前没有交互终端 | 使用完整参数的直接命令，先看子命令 --help | 缺参补齐后执行，不用答案管道模拟菜单 |

需要详细错误信息时：

```bash
CASE="/path/to/my case"
vaspsolkit --debug status --workdir "$CASE"
```

### 取消排队任务或换节点

菜单 `42` 可选择取消当前 Case 中已记录的排队带电任务，核对任务列表后须输入 `CANCEL`。
取消后任务恢复为 PREPARED，不会自动重新提交。菜单 `41` 则先取消排队任务，再修改节点配置，
同样需要后续单独确认重提。这不是运行中任务或中性任务的通用取消入口。

退出菜单不会取消作业。对状态不明确的任务，先用 `status` 以及集群的 squeue/sacct 核对原 Job ID，
不要把查询失败当成可以取消或重提的依据。

### 修复失败点

菜单 `51` 一次选择一个失败扫描点，确认后归档旧输出并修改输入，**不立即重提**。
直接命令可用：

```bash
vaspsolkit repair --workdir "$CASE" --job 3 --no-submit
```

它展示修复建议并询问，拒绝才是仅预览；确认后会改文件。`--no-submit` 只表示不提交，
不表示不修改。省略它可能提交不止一个就绪点，建议修复后另行检查、选择需要的点提交。
默认恢复不自动开启 WAVECAR。

**修复后：** 阅读归档与修改记录，重新检查该点输入，确认原作业不再活动后单独选择提交。
新计算完成后重新 check、collect、audit、postprocess；修复命令结束不代表该点已经收敛。

中性任务失败时先查看 `check-neutral` 的诊断；当前扫描点 repair 入口不能当作中性任务的通用恢复器。
不要删除状态来强行回到第一步，应保留原输出和 Job ID，核对队列后再制定恢复方案。

### 提交时断线或出现未决回执

先确认原作业是否已经创建，不要以终端中断判断“没有提交”。
保留 `vaspsolkit.state.json` 和用户级活动记录
`${XDG_STATE_HOME:-~/.local/state}/vaspsolkit/cases`，通过 squeue/sacct 核对 Job ID。
UNKNOWN、SUBMIT_UNKNOWN 或损坏回执都需要先查明，不能删除文件绕过防重复提交保护。

迁移运行中的 Case 到新目录、账户或服务器，不是自动续跑功能。先在原位置完成或明确处理作业与
未决回执。新服务器重新开始计算时，使用独立目录和新服务器脚本，不携带旧任务状态直接重提。
遇到回滚不完整的提示，先检查列出的文件和备份路径，不要再次覆盖。

## 更新旧配置

SLURM v1 配置可迁移到 v2：

```bash
vaspsolkit migrate --input "/path/to/old-slurm.json" --output "/path/to/new-vaspsolkit.json"
```

写入前会显示差异并确认；覆盖已有目标需要明确 `--force`。
旧 PBS 配置不自动转换。非 SLURM 用户见[自定义适配](CLUSTER_CONFIGURATION.md#custom-适配边界)。

## 需要帮助时提供什么

提供运行命令、程序版本/导入位置、Python 版本、调度器类型、Case 当前阶段，以及脱敏后的错误。
使用 `--debug` 获取的完整信息发送前也应检查路径和敏感内容。不要提供 POTCAR、访问令牌或整个计算目录。
