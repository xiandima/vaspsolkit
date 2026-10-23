# 实测兼容性与限制

本页对应 `main` 上的 `0.4.0` 开发版，验收日期为 2026-10-24。以下“通过”仅指列出的离线或
假程序测试，不代表真实集群、VASP 版本或科学结果已经验收。

## 测试环境

- Linux x86_64：Python 3.9.17 / pytest 8.3.2 / matplotlib 3.9.4 / setuptools 68.0.0；
  独立 Python 3.12.15 / pytest 9.1.1 / matplotlib 3.11.2 / setuptools 84.2.1 环境。
- Python 3.12 环境重新下载并安装运行、测试及文档依赖；它仍是同一台 Linux 机器，
  不等于所有操作系统、CPU 或集群均已验证。
- wheel 回归测试在含空格路径的新 venv 中复用测试环境依赖，但移除源码路径注入，
  检查实际安装来源，并用安装后的命令完成初始化、离线计划、准备、提交预览和合成五点后处理。
- 每个 CLI 子进程使用临时 HOME、状态目录和 PATH。无集群场景的 PATH 为空；假调度器
  仅记录 argv/cwd 并返回固定 Job ID 或状态，绝不执行提交脚本。
- POSCAR/POTCAR 等均为测试合成文本，POTCAR 仅含解析用元数据，不可用于计算。
  未访问真实调度器、未启动 VASP、未复制真实 POTCAR 或凭据。

## 兼容矩阵

主要黑盒证据位于 [test_documented_workflows.py](../tests/test_documented_workflows.py)，
输入边界和故障证据位于 [test_input_portability.py](../tests/test_input_portability.py) 与
[test_scheduler_portability.py](../tests/test_scheduler_portability.py)；Case 规划的路径限制见
[test_vaspsolkit_case_setup.py](../tests/test_vaspsolkit_case_setup.py)。

| 场景 | 实测结果 | 范围与限制 |
| --- | --- | --- |
| Case、HOME、源码及 venv 路径含空格 | 通过 | Shell 中路径加引号；不等于任意脚本文件名都被受管理资源接口接受 |
| wheel 构建、安装与两个入口 | 通过 | 检查 wheel 包名、安装模块位于新 venv、`direct_url.json` 指向该 wheel；验证两个入口及手册中的离线初始化到合成五点画图路径 |
| 无 VASPKIT、无 VASP、无集群命令 | 通过 | 输入齐全时 `init --yes`、`plan --offline`、`prepare-neutral --yes`、中性提交 `--dry-run`；并非实际计算 |
| 无交互 `init --yes` | 通过 | 显式给出 scheduler/script/SHE reference；缺 INCAR 还须 profile；保持 stdin 打开也不等待输入 |
| 已有 INCAR、不指定 profile | 通过 | 保留用户值，补必要标签；冲突和重复标签须人工处理。后续 prepare 使用阶段 profile，须另行核对 |
| 用户提交脚本保留 | 通过 | 初始化、准备、中性 CLI dry-run/假提交不覆盖脚本；测试保留 account/qos、自定义正文与文件权限 |
| 已有 Case 续跑 | 通过 | 假提交后 status/run 保留 Job ID、不重复投递；再次 init 被拒绝且原文件不变；更新 SHE 来源不清空状态 |
| CLI 通用初始化分支写入失败 | 故障注入覆盖 | INCAR 原子写入；失败时尝试恢复本次已写文件。检测到外部改动时保留改动，并报 manual review 路径；不是崩溃事务 |
| 无 sinfo 的资源配置 | 安全失败 | `configure-scheduler` 显示错误，不改已有配置；不是自动推断节点模式 |
| custom 命令路径含空格 | 通过 | CLI 分词保留参数，假提交/查询/续跑可用；分号和 `$HOME` 作为字面参数传递，不经过 Shell |
| custom 未配置 status | 明确提示 | 初始化可成功，状态为 UNKNOWN；不表示已具备完整跟踪能力 |
| 交互 VASPKIT 补缺失输入 | 假程序通过 | 临时目录运行、仅导入缺失 POTCAR/KPOINTS；已有 INCAR、结构、脚本及另一输入不被工具改动覆盖 |
| VASPKIT 失败或输入并发变化 | 安全拒绝 | 对退出码、空文件、符号链接、错误元素、格式错误及交互期间源文件变化有回归测试 |
| 元素标签与辅助程序路径 | 通过 | 路径型 POSCAR 标签在暂存前拒绝；支持分数氢 POTCAR 变体；PATH 的相对目录、空格与辅助程序软链接有回归测试 |
| VASPKIT 暂存或部分发布失败 | 故障注入覆盖 | 全部校验数据先在 Case 同盘暂存，再以 hard link 无覆盖发布；回滚前检查归属和内容，检测到外部变化或回滚失败时明确报路径 |
| 四行规则 KPOINTS | 通过 | Gamma/Monkhorst-Pack 网格的偏移行可省略，默认零偏移；显式非法偏移被拒绝 |
| KSPACING-only、无 KPOINTS | 通过 | 有限正数 KSPACING、可选 KGAMMA；初始化、中性/带电准备、快照和提交前检查均支持；包括 D 指数、非法值与原有 KPOINTS 优先级测试 |
| `inputs` / `init --yes` 自动补全 | 通过 | 显式 mesh，或 VASPKIT 1.5.x 的 103/102；缺参数一次报出，不猜测赝势；native 可不依赖 VASPKIT |
| 保存输入配置并复用 | 通过 | 显式 --save-settings 或交互补全时确认保存，默认 XDG_CONFIG_HOME；复用库/映射/密度设置，不跨体系复用显式网格 |
| 菜单首次补全、取消与重试 | 通过 | 14/02 提供保存询问，默认不保存；取消不写输入/配置；错误提示可重新选择补全，再进入初始化 |
| 自动分配在不同分区的服务器上使用 | 假程序通过 | 可选择本机分区，不要求存在 compute；空列表/查询失败/无效分区可取消且原配置不变；自动分配不绑定节点 |
| 无 TTY 缺参数、stdin 保持打开 | 通过 | init 缺基础必填项和 configure-reference 缺参考值时立即报错；完整批处理应显式 --yes，不以开放管道代替交互终端 |
| 菜单 14 / inputs / next | 通过 | 统一补全入口；next 一次执行一个动作，dry-run 不查询队列；写入和提交保留确认 |
| 内置 SLURM 提交与状态查询 | 假程序通过 | 不代表账户/分区、控制器、MPI、模块或真实作业已验证 |
| 部分批次失败与并发限制 | 故障注入覆盖 | CLI 通过逐任务回执保存已接受的 Job ID；未决回执阻止重提；选点提交将超出 max_inflight 的任务留为 PREPARED |
| 重算后结果新鲜度 | 通过 | Job ID、重启次数、配置或源文件 stat 信息变化后推荐重新 collect；旧版缺少采集记录时也需重新 collect |
| 内置 PBS | 不支持 | 旧 PBS 配置不自动迁移；custom 包装器须自行实现、验证和维护 |
| 原生 Windows、macOS、多用户共享写同一 Case | 本轮未验收 | 核心使用 POSIX `fcntl` 锁，原生 Windows 不是当前支持目标；隔离 HOME 测试不等于多账户权限测试 |

## 使用边界

### 安装与版本来源

在独立 Case 目录检查当前 Python 实际导入的包：

```bash
python -c 'import vaspsolkit; print(vaspsolkit.__version__, vaspsolkit.__file__)'
```

旧源码目录或残留 `PYTHONPATH` 会遮蔽新安装，尤其是 `python -m vaspsolkit`；先更换工作目录、
清理旧路径并确认虚拟环境，再解释旧菜单或 PBS 提示。版本号同为 `0.4.0` 也不能单独证明
导入了当前 main 修订，必须核对文件来源及[手册构建提交](versions.md)。

离线构建使用 `--no-build-isolation` 时，调用环境必须已具备 `setuptools>=68` 和 `wheel`。
不能仅依据 pip 返回 0 判断 wheel 正确：过旧构建工具可能产生 `UNKNOWN-0.0.0`。安装测试
因此检查产物元数据和实际安装来源。`--no-deps` 不会替用户补齐 matplotlib 等依赖。

### 输入与脚本

使用 VASP5 元素行的 POSCAR，并自行确认 POTCAR 类型、顺序和许可证。ENMAX 建议不等于
ENCUT 已做收敛测试；保留已有 INCAR 的初始化路径不会自动填写缺失 ENCUT。

CLI 通用初始化分支拒绝替换 symlink INCAR；请先将模板复制为 Case 内的普通文件，避免
失败恢复破坏链接语义。Case 规划还限制指向 Case 外部的输入或脚本链接，不能把模板链接
方案视为所有初始化入口通用支持。

将脚本放在 Case 内，使用 `vasp.slurm` 等简单文件名。本轮验证的是目录路径含空格，不能
推断绝对脚本路径、向 Case 外的链接或任意复杂脚本都支持。初始化不自动导入脚本里的
资源：默认 compute / 1 节点 / 96 tasks / 72:00:00 会在提交时显式传入，可能覆盖 `#SBATCH`
头。脚本文本保留不等于最终资源请求不变；受管理 profile 的 launcher 仍限 `mpirun + vasp_std`。

自动补全默认使用原生后端；可选 VASPKIT 1.5.x。已对本机 1.5.0 执行只生成 POTCAR、
只生成 KPOINTS、同时生成两者的临时冒烟测试，使用合成解析数据，**不是合法赝势库的
科学计算验收**。其他错误路径使用假外部程序覆盖。不同版本目前明确拒绝自动任务，
应使用 native 后端或用户提供的输入。

临时运行只提供结构和相关输入，不传提交脚本；隔离 HOME、显式关闭自动提交、固定允许
任务 103/102，并设 120 秒超时。赝势映射先从用户授权的库校验，再构造临时选择目录，
不修改用户的 ~/.vaspkit。不默认采用工具自己的推荐赝势，输出还须通过顺序、变体和
泛函检查，并与确认库的有序拼接逐字节一致；工具改动临时 POSCAR 时拒绝导入。
活动任务、损坏或未决提交回执均阻止补全，发布前会再次检查。保存的赝势库统一为绝对路径，
避免换目录后误选同名不同版本的库。已存在输入不会被替换。补全成功后再取消初始化不删除已确认生成的文件；
可复用配置保存是后续独立步骤，保存失败会提示输入已完成，不能误认为整项操作未执行。

VASPKIT 导入在 Case 所在文件系统暂存全部已校验数据，再通过 hard link 逐一发布，已有
同名文件不会被覆盖；目标文件系统须支持 hard link。暂存失败不发布输入；发布途中失败时，
仅撤销检查时仍属于本次导入、且内容未改变的文件。检查时检测到的外部替换或原位改动
会被保留，并报告需人工检查的具体路径。锁是协作式锁，不能隔离不遵守锁的其他程序；
不要同时用编辑器或其他脚本修改正在初始化、导入或回滚的同一 Case。

初始化与输入导入的回滚只针对程序能捕获并处理的异常。CLI 通用初始化分支对 INCAR 使用
原子写入，后续失败时尝试恢复本次已写文件；内容不再匹配或恢复操作失败时报告
`rollback needs manual review`。默认 Slurm 初始化也会先暂存原始字节和权限，发布失败时
尝试恢复；检测到变化或无法恢复时报告 `rollback incomplete` 及保留的备份路径。
此时应先核对所列文件再决定如何重试。**逐文件原子写入、
无覆盖发布和异常回滚均不等于跨文件、跨进程的 crash transaction**：进程被强制终止、
系统崩溃或断电后不保证自动恢复全部文件，也不保证整个初始化全成或全败。

### 离线与恢复

`plan --offline` 不查询队列，显示的计划不是集群就绪证明；一般 `plan`、`status`、`monitor`
可能调用调度器。`--yes` 是跳过确认并允许写入/提交，不是预览；只有带 `--dry-run` 的对应
提交命令才是不提交路径，也不等于真实资源验收。

已存在 `vaspsolkit.json` 或 `vaspsolkit.state.json` 时应使用 status/menu 和配置命令续跑，
不要删除状态以绕过 init 保护。Case 状态和用户级 submission receipt 都参与恢复；活动状态
默认在 `${XDG_STATE_HOME:-~/.local/state}/vaspsolkit/cases`，可通过 `VASPSOLKIT_STATE_ROOT`
设置。迁移到新账户或新路径不是经过验证的自动恢复方案，须先核对真实 Job ID 和未决回执，
不能仅复制配置后直接重提。

调度器完成与 VASP 收敛是独立判定。本轮假任务不生成有效 OUTCAR/CHGCAR/LOCPOT，不能
据此声称完整五点扫描或 E-U 科学工作流已在新服务器验收。科学边界见
[计算约定](SCIENTIFIC_CONVENTIONS.md)，真实集群适配见[集群配置](CLUSTER_CONFIGURATION.md)。

## 复现实测

在当前源码根目录、具备开发依赖及上述构建工具的环境中执行：

```bash
python -m pytest -q tests/test_documented_workflows.py tests/test_documentation_examples.py
python -m pytest -q tests/test_input_portability.py tests/test_scheduler_portability.py
python -m pytest -q tests/test_kspacing_workflow.py tests/test_input_completion.py tests/test_quick_commands.py
python -m pytest -q
```

测试自行隔离状态目录，不需要全局设置 `VASPSOLKIT_STATE_ROOT`，也不需要任何集群命令。
安装测试从当前工作树复制必要包文件到临时目录构建，排除源码路径注入后安装和运行，
不使用只打包已提交 HEAD 的维护者发布脚本。
