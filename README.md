# VASPsolKit

VASPsolKit 是面向 VASPsol 恒电势带电扫描的状态驱动终端工作流。它把“中性结构优化 →
带电点结构优化 → 收敛检查 → 结果收集与 E–U 分析”组织成可恢复的 Case，并提供类似
VASPKIT 的固定编号菜单。

提交 PBS、Slurm 或自定义调度任务后程序立即返回，不会在前台等待数小时的 VASP 计算。

> **Alpha 软件。** 本项目不提供 VASP、VASPsol、POTCAR 或机构专属提交脚本。首次使用
> 请先在小体系上验证 VASPsol 版本、INCAR、赝势和服务器资源语法。

## 安装

需要 Python 3.9 或更高版本；后处理依赖 matplotlib。正式菜单不依赖 Textual、curses 或
图形桌面。

```bash
python -m pip install .

# 开发安装
python -m pip install -e ".[dev]"
```

无外网集群中，如果当前 Python 已具有运行依赖：

```bash
cd /path/to/vaspsolkit
python -m pip install --no-deps --no-build-isolation -e .
```

也可先在有构建环境的机器制作 wheel，再复制到集群：

```bash
python tools/build_release.py --outdir /tmp/vaspsolkit-release
python -m pip install --no-index --no-deps /path/to/vaspsolkit-*.whl
```

如果暂时不安装，可以显式指定源码路径：

```bash
PYTHONPATH=/path/to/vaspsolkit python -m vaspsolkit --help
```

`No module named vaspsolkit` 表示当前 Python 找不到该包，不等于项目没有封包。请确认
`which python` 和 `python -m pip --version` 指向同一环境。

## Case 基础输入

每个体系使用一个独立 Case 根目录：

```text
my-case/
├── POSCAR
├── INCAR
├── KPOINTS
├── POTCAR
└── vasp.pbs       # 名称可在初始化时修改
```

- `POSCAR` 是首先进行中性结构优化的初始结构。
- `POTCAR` 元素顺序必须与 `POSCAR` 一致；软件不会分发或伪造 POTCAR。
- 提交脚本中的队列、模块、MPI 命令和 VASP 可执行文件必须按服务器实际环境修改。
- 提交脚本必须使用 Unix 换行，避免 PBS 报 `script is written in DOS/Windows text format`。

### INCAR 以用户输入为准

VASPsolKit 不用固定模板覆盖已有 `INCAR`。`ENCUT`、泛函、色散、磁性以及
`IBRION/NSW/ISIF/EDIFFG` 等结构优化参数均保留用户设置。初始化只检查重复或冲突标签，
并在确认后补充缺少的 VASPsol 必要参数。

中性计算需要生成可用的 `CONTCAR` 和 `CHGCAR`。带电点使用中性 `CONTCAR` 作为
`POSCAR`，继承中性 `CHGCAR`，不依赖 `WAVECAR`；带电点明确使用 `ISTART = 0`、
`ICHARG = 1` 并调整 `NELECT`，其余结构优化参数保持不变。

## 最简单的使用方式

进入 Case 后直接运行：

```bash
cd /path/to/my-case
vaspsolkit
```

也可以显式指定目录：

```bash
vaspsolkit menu --workdir /path/to/my-case
```

程序先读取当前 Case，并且只查询当前 Case 状态文件中已经记录且尚未结束的 Job ID，
随后显示状态摘要和固定编号菜单。它不会扫描或认领用户全局队列，不会在启动时自动执行
`qsub`、`qdel` 或重新提交。

新手通常只需要反复选择：

```text
02) 执行推荐下一步
```

每次操作完成、失败或取消后，程序都会重新读取 Case、同步已记录任务并返回主菜单。
输入 `00`、按 `Ctrl+C` 或 `Ctrl+D` 会安全退出，不影响服务器任务。

### 终端显示

交互式终端默认使用少量 ANSI 样式区分可执行编号、推荐步骤、收敛状态、错误和不可用
任务；颜色只用于辅助识别，所有状态仍保留文字。菜单面向普通 SSH 环境按 80 列排版，
每次操作后继续向下输出新菜单，不会自动清屏，因此 Job ID、PBS 返回和错误历史不会消失。

如需关闭颜色，可运行：

```bash
NO_COLOR=1 vaspsolkit
```

非交互终端、重定向和管道输出会自动关闭 ANSI 样式。终端字体由 SSH 客户端控制，
VASPsolKit 不能修改字体或字号。中文环境推荐使用 `Sarasa Mono SC`；使用 JetBrains Mono
或 Cascadia Mono 时，应配置中文等宽回退字体。字体设置不是软件运行前提。

## 固定任务编号

### SHE reference 确认与修改

初始化时必须确认真空能级到 SHE 的参考值：

```text
SHE reference [4.70 eV] >>
```

`4.70 eV` 是建议默认值，用户应按项目约定确认。菜单 `13)` 或以下命令可修改：

```bash
vaspsolkit configure-reference --workdir /path/to/case
```

配置中的 `she_reference_source` 可记录来源。修改后不需要重跑 VASP，但已有派生结果会被标记为过期，需要重新执行 `60 → 61 → 62`；旧分析历史不会删除。

任务编号不会随计算阶段变化；`2` 与 `02` 等价，`0` 与 `00` 等价。

| 编号 | 功能 |
|---:|---|
| `01` | 刷新当前 Case 状态 |
| `02` | 执行当前推荐下一步 |
| `03` | 查看完整状态与诊断 |
| `10` | 检查基础输入文件 |
| `11` | 初始化 Case 配置 |
| `12` | 设置调度器、队列、节点和核心数 |
| `13` | 设置电化学参考参数 |
| `20` | 准备中性结构优化 |
| `21` | 提交中性任务 |
| `22` | 检查中性任务与收敛 |
| `30` | 准备带电点目录 |
| `31` | 检查带电点输入 |
| `32` | 选择并提交带电点 |
| `40` | 监测已记录任务 |
| `41` | 更换排队任务节点：先取消并改资源，不自动重交 |
| `42` | 取消排队任务并恢复为 `PREPARED` |
| `50` | 收敛检查与错误诊断 |
| `51` | 预览并修复失败任务，不自动提交 |
| `60` | 收集结果 |
| `61` | 结果审计 |
| `62` | 后处理与 E–U 分析 |
| `90` | 帮助与等效命令 |
| `00` | 退出 |

当前不能执行的编号仍会显示，并说明阻断原因。例如带电点尚未生成时，`32` 不会被强行
传给底层工作流。

## 三类确认

菜单根据副作用使用三种确认方式：

1. 只读检查直接执行；
2. 创建、归档或修改 Case 文件时，先显示等效命令或差异，只有输入 `y` 才执行；
3. 提交任务必须精确输入 `SUBMIT`，取消任务必须精确输入 `CANCEL`。

`y` 不能确认提交，`SUBMIT` 不能确认取消。换节点严格拆成取消旧任务和以后重新提交两个
动作，取消后不会自动调用新的 `qsub`。

## 每次提交前的资源配置

选择 `21` 提交中性任务或选择 `32` 提交带电点时，都会先出现“提交资源配置”，明确
展示当前调度器、队列、节点策略、节点、核心数、walltime 和提交脚本：

```text
提交资源配置

当前配置
  调度器：PBS
  队列：normal
  节点策略：指定节点
  节点：compute-a.example.org
  核心数：48
  Walltime：48:00:00
  提交脚本：vasp.pbs

1. 使用以上配置
2. 自动分配节点，重新设置核心数
3. 指定节点，重新设置核心数
0. 取消提交
```

选择自动分配节点会清除本次提交的节点约束并要求输入核心数。PBS 下选择指定节点会先
读取节点状态，显示总核心数、已使用和空闲核心数；不存在、down/offline 或空闲核心不足
的节点不会进入提交预览。当前版本一次提交最多指定一个节点。

修改资源后程序询问：

```text
是否保存为当前 Case 默认配置？[y/N]
```

- 回车或输入 `n`：只用于本次提交，不修改 `vaspsolkit.json`；
- 输入 `y`：通过最终确认后保存为当前 Case 默认资源；
- 输入 `0` 或未输入 `SUBMIT`：取消操作，不提交任务，也不保存资源配置。

资源选择结束后还会再次显示最终任务、目录、节点、核心数、队列、walltime 和脚本。只有
精确输入 `SUBMIT` 才进入 durable submission 屏障并调用调度器。

## 推荐全流程

### 1. 初始化

选择 `02` 或 `11`。程序检查基础输入、POTCAR 顺序、INCAR 必要参数和提交脚本，并生成
Case 内的 `vaspsolkit.json`。

等效命令：

```bash
vaspsolkit init --workdir /path/to/my-case
```

### 2. 准备并提交中性结构优化

依次选择 `20` 和 `21`。准备步骤可能归档旧输出，必须先确认文件变化；提交步骤显示资源
并要求输入 `SUBMIT`。成功后保存 Job ID 并立即返回。

```bash
vaspsolkit prepare-neutral --workdir /path/to/my-case
vaspsolkit submit-neutral --workdir /path/to/my-case
```

返回菜单不代表 VASP 已完成，不要因此重复提交。

### 3. 监测与中性收敛检查

选择 `01`/`40` 同步调度状态，任务结束后选择 `22`。PBS/Slurm 中已经找不到 Job ID 时，
仍需结合本地 `OUTCAR`、`CONTCAR` 和 `CHGCAR` 判断是否收敛。

```bash
vaspsolkit monitor --workdir /path/to/my-case
vaspsolkit check-neutral --workdir /path/to/my-case
```

调度器临时不可用只会在菜单顶部产生警告，不会阻止读取本地 Case。

### 4. 准备并提交带电点

中性任务收敛后选择 `30` 生成带电点目录，选择 `31` 做提交前检查，再用 `32` 明确选择
要提交的 `PREPARED` 点。

```bash
vaspsolkit prepare-charge --workdir /path/to/my-case
vaspsolkit check-prepared --workdir /path/to/my-case
vaspsolkit submit-selected --workdir /path/to/my-case 1 2 3 4 5
```

软件默认不设置自己的最大并发上限；是否可以同时提交多个任务取决于服务器政策和
`vaspsolkit.json` 中的实际调度配置。

### 5. 收敛检查、结果与后处理

带电点完成后使用 `50` 检查输出。所有点收敛后依次选择 `60`、`61` 和 `62`。

```bash
vaspsolkit check --workdir /path/to/my-case
vaspsolkit collect --workdir /path/to/my-case
vaspsolkit audit --workdir /path/to/my-case
vaspsolkit postprocess \
  --summary /path/to/my-case/results/summary.csv \
  --output /path/to/my-case/results
```

## 调度器与指定节点

任务 `12` 可设置调度器类型、队列、单任务核心数、walltime、提交脚本和节点。自动分配时
不写节点约束；指定节点时当前版本一次最多填写一个节点。空队列表示使用集群默认队列。

不同 PBS/Slurm 集群的资源语法并不完全相同。请以服务器管理员文档和已验证提交脚本为
准；VASPsolKit 不会猜测站点专属节点、模块或队列。

PBS 排队任务不能原地迁移。安全换节点顺序为：

1. 选择 `40` 查询已记录 Job ID 的实时状态；
2. 选择 `41`，选中 `QUEUED/SUBMITTED` 任务并输入 `CANCEL`；
3. 旧任务成功取消后修改节点与核心数，任务回到 `PREPARED`；
4. 回到 `32`，重新预览资源并输入 `SUBMIT`。

`RUNNING` 或状态不明的任务不会被当作普通排队任务取消。旧 Job ID 已消失时，程序结合
本地输出决定恢复为 `PREPARED` 还是要求人工检查，不会无条件重复 `qdel`。

## Durable submission 屏障

中性提交会在调用调度器前记录提交意图，收到合法 Job ID 后记录已接受状态，再原子更新
Case 状态。若提示 `SUBMITTING`、`ACCEPTED`、`SUBMIT_UNKNOWN` 或 receipt 无法读取：

1. 不要再次提交；
2. 选择 `03` 查看技术诊断；
3. 人工核对调度队列与 Case 后再处理真实 Job ID。

程序不会从全局队列猜测 Job ID，也不会自动认领其他 Case 的任务。

## 独立 CLI 与自动化

固定菜单是新手入口，原有独立子命令仍适合脚本化：

```bash
vaspsolkit init --help
vaspsolkit prepare-neutral --help
vaspsolkit submit-neutral --help
vaspsolkit check-neutral --help
vaspsolkit configure-scheduler --help
vaspsolkit prepare-charge --help
vaspsolkit check-prepared --help
vaspsolkit submit-selected --help
vaspsolkit monitor --help
vaspsolkit repair --help
vaspsolkit collect --help
vaspsolkit postprocess --help
```

自动化脚本应显式传入 `--workdir`。只有已经由外部流程完成风险确认时才使用 `--yes`；
不要把需要人工输入的 `vaspsolkit menu` 放进批处理脚本。

## 已归档 UI

旧 Textual 工作台和 curses 界面已归档，不进入安装包，也不再是默认运行路径：

- `vaspsolkit ui`
- `vaspsolkit workbench-ui`
- `vaspsolkit legacy-ui`

这些命令只显示“已归档”提示，不导入 Textual。已停用界面的源码与研发设计资料不随
公开仓库发布；正式版本只保留兼容提示。`vaspsolkit wizard` 现在是 `vaspsolkit menu`
的兼容别名。

## 开发与发布检查

```bash
python -m pytest -q
python -m compileall -q vaspsolkit tests
python -m build --wheel --no-isolation
```

运行活动记录默认写入 `${XDG_STATE_HOME:-~/.local/state}/vaspsolkit/cases`。在只读 HOME、
容器或 CI 中，可用 `VASPSOLKIT_STATE_ROOT=/path/to/state` 显式指定状态目录。

贡献前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md) 和
[公开发布范围](docs/maintainer/PUBLIC_RELEASE_SCOPE.md)。

## License

本项目采用 [MIT License](LICENSE)。
