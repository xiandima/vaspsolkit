# VASPsolKit

在 SSH 终端中，用编号菜单完成 **输入准备 → 中性计算 → 五点电荷扫描 → 收敛检查 → E-U 图与报告**。
每个体系使用一个独立计算目录（Case），不需要提前算好中性参考。

适用于 Linux 集群上的 VASP/VASPsol 计算，内置支持 **SLURM**，VASPKIT 可选。
程序执行固定 `NELECT` 扫描后建立 E-U 关系，**不是运行中自动调电子数的恒电势求解器**。

> 当前为 `0.4.0` 开发版。VASP/VASPsol、合法赝势和本机运行脚本由你提供；
> 首次换服务器或计算方法，请先用小体系验证。

[在线用户手册](https://xiandima.github.io/vaspsolkit/) · [第一次计算](https://xiandima.github.io/vaspsolkit/quickstart.html) · [E-U 结果示例](https://xiandima.github.io/vaspsolkit/results.html)

在线手册与通过测试的 GitHub `main` 同次提交一起构建。
可在[版本说明](https://xiandima.github.io/vaspsolkit/versions.html)核对提交，文档源码从[手册首页](docs/index.md)进入。

## 安装

需要 Python 3.9 或更新版本。在可联网的服务器上执行：

```bash
git clone https://github.com/xiandima/vaspsolkit.git
cd vaspsolkit
python -m venv "$HOME/.venvs/vaspsolkit"
source "$HOME/.venvs/vaspsolkit/bin/activate"
python -m pip install .
vaspsolkit --help
```

显示命令帮助后，进入自己的计算目录开始使用，不在源码目录计算。
已有源码可跳过前两行；重新登录后激活同一个环境：

```bash
source "$HOME/.venvs/vaspsolkit/bin/activate"
```

离线安装、Conda、升级或命令找不到，见[安装与升级](docs/INSTALLATION.md)。

## 开始或继续计算

| 你现在的情况 | 下一步 |
| --- | --- |
| 第一次使用 | 按[第一次计算](docs/quickstart.md)完成菜单教程 |
| 只有 POSCAR、INCAR 模板和本机脚本 | 先按[输入准备](docs/USER_GUIDE.md#输入准备)补齐赝势与采样 |
| 已有 Case，回来查看进度 | 进入原目录打开菜单，选 `03` 查看阶段，不重新初始化 |
| 任务失败、UNKNOWN 或提交断线 | 先按[检查与恢复](docs/recovery.md)核对原 Job ID |
| 五点完成 | 按[结果解读](docs/results.md)先审计，再拟合 |
| 将来要组合多个体系 | 首次 collect 前先填写[方法记录](docs/USER_GUIDE.md#先记录方法) |

打开已有 Case 的菜单：

```bash
cd "/path/to/my-case"
vaspsolkit
```

`02` 每次只执行一个推荐动作，退出后不会自动提交下一阶段或补交剩余扫描点。
**收集后请主动选 `61` 审计，再执行 `62` 拟合；推荐拟合不代表审计已通过。**
选 `00` 退出不会取消服务器任务，下次回到同一目录继续；状态为 `UNKNOWN` 时先核对队列，不要直接重提。
任务跟踪只查询当前 Case 已记录的 Job ID，不会认领你在其他目录提交的作业。

## 提交前确认

- 每个 Case 准备 POSCAR、INCAR、合法 POTCAR、本机脚本，以及 KPOINTS 或有效 KSPACING；两种采样同时存在时以 KPOINTS 为准。
- 默认流程为中性结构优化加五点扫描。初始化保留已有 INCAR 不等于后续不改参数，准备后仍须核对优化、自旋、溶剂和初猜设置。
- 默认资源为 `compute / 1 节点 / 96 tasks / 72:00:00`，不是通用推荐值，可能覆盖脚本中的同名资源选项。提交前按[服务器适配](docs/CLUSTER_CONFIGURATION.md)调整。
- 提交时可选自动分配或指定节点；指定节点先选分区，再从该分区的节点列表中选择。
- 菜单提交需要输入 `SUBMIT`。直接命令的 `--yes` 表示授权执行，不是预览；提交预览使用 `--dry-run`。

## 结果与科学边界

默认先读 `results/quality_report.csv` 和原始五点，再看 `results/eu/eu_curve.png` 与残差。
文件用途、成功信号及合成演示见[结果章节](docs/results.md)。生成图或报告不等于科学验收通过。

- 五点默认电子数偏移为 `[-1.0, -0.5, 0.0, 0.5, 1.0]`，必须含唯一中性点，不静默删点；根目录中性准备任务之外还有独立中性扫描点，默认共 **1 + 5 个任务**。
- 默认中性计算使用全新密度，五点使用中性 CHGCAR 初猜（CHGCAR-only），**不读取 WAVECAR**。
- 当前电势处理取中性 LOCPOT 平面均值的最大值，不自动识别真空平台，也不应用 `FERMI_SHIFT`。
- SHE 参考值须匹配实际方法；拟合顶点不自动等于 PZC，电子能量不自动等于完整 Gibbs 自由能。

开始计算前请核对[计算约定](docs/SCIENTIFIC_CONVENTIONS.md)，不要把不同 VASPsol 分支的输出直接当作等价输入。

## 常用入口

| 编号 | 菜单名称 | 什么时候用 |
| --- | --- | --- |
| `02` | 执行推荐下一步 | 不确定下一步做什么 |
| `03` | 查看完整状态与诊断 | 出错、卡住或需要查看任务记录 |
| `14` | 补齐输入：模板、赝势与采样 | 缺少 INCAR、POTCAR 或采样设置 |
| `61` | 结果审计 | 拟合前检查计算质量 |
| `62` | 后处理与 E-U 分析 | 生成或重新生成五点拟合 |
| `00` | 退出 | 保留记录，下次继续 |

直接命令与批处理查阅[使用手册](docs/USER_GUIDE.md)或[命令查询](docs/commands.md)。
已验证环境见[兼容性说明](docs/COMPATIBILITY.md)；报告问题见[CONTRIBUTING](CONTRIBUTING.md)，版本变化见[CHANGELOG](CHANGELOG.md)。
不要在公开 issue 中上传 POTCAR、凭据或未公开计算数据。

## 许可

[MIT License](LICENSE)。VASP、VASPKIT 及赝势各自的授权条款不随本项目改变。
