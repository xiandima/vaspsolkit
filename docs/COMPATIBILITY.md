# 实测兼容性与限制

本页说明哪些用法已有软件测试、哪些仍需你在服务器上验证。
所列环境的软件回归于 2026-10-28 再次核验，对应 `main` 上的 `0.4.0` 开发版。
**离线测试或模拟调度器通过，不代表真实集群、VASP 版本或科学结果已经验收。**

## 先判断能否使用

| 你的环境或用法 | 当前支持情况 | 使用前要做什么 |
| --- | --- | --- |
| Linux x86_64、Python 3.9 或更新 | 已在下方两个 Python 环境测试 | 使用独立环境，按[安装指南](INSTALLATION.md)核对实际导入位置 |
| SLURM | 内置提交和查询；自动化测试使用模拟调度器 | 用本机小体系确认账户、分区、模块、MPI 和 VASP |
| PBS 或其他调度器 | 不内置 PBS；custom 需要自己的包装命令 | 按[自定义适配](CLUSTER_CONFIGURATION.md#custom-适配边界)验证提交、查询和取消 |
| 没有 VASPKIT | 可用原生输入补全 | 提供合法赝势库、元素映射与采样 |
| VASPKIT 1.5.x | 支持输入生成任务 103/102 | 核对版本；其他版本使用原生后端或自行提供输入 |
| 只有 KSPACING，没有 KPOINTS | 支持有限正数 KSPACING，可选 KGAMMA | 已有 KPOINTS 时仍优先使用文件，不自动回退 |
| Case、HOME、源码或环境目录含空格 | 已有测试 | Shell 中路径加引号；提交脚本仍用 vasp.slurm 等简单名称 |
| 原生 Windows | 不是当前支持目标 | 程序依赖 POSIX 文件锁 |
| macOS 或多用户同时写同一 Case | 尚未验收 | 不把单机或隔离目录测试当作该场景保证 |

首次换服务器或方法，请先按[服务器适配](CLUSTER_CONFIGURATION.md)做小体系验证。
`plan --offline` 和提交 `--dry-run` 可以检查部分文件流程，但不验证真实运行环境。

## 输入与文件要求

使用带元素名称行的 VASP5 POSCAR，自行确认 POTCAR 类型、顺序和授权。
ENMAX 推算的 ENCUT 只是建议，不替代收敛测试；保留已有 INCAR 的初始化分支不会自动填写缺失 ENCUT。
中性与扫描准备会再次应用阶段参数，须检查生成后的输入。

输入与提交脚本应为 Case 内的普通文件。初始化拒绝替换符号链接 INCAR，
指向 Case 外的输入或脚本链接也受限制。先复制模板为普通文件，不把链接方案当作通用支持。

### 补齐输入后如何判断

程序只生成缺失输入，不覆盖已有文件。赝势库、泛函和元素变体须由你确认，程序不会下载赝势。
VASPKIT 在临时目录运行，只允许输入生成任务，不携带提交脚本、不修改用户的 `~/.vaspkit`。
生成结果还会核对元素顺序、变体、泛函和确认库的内容，校验失败不会导入。

看到输入生成成功后，核对生成文件和 `vaspsolkit.inputs-<id>.json`，再做基础输入检查。
保存复用设置是后续独立步骤；保存失败或后来取消初始化，不表示已生成输入被删除。
保存的库路径为绝对路径，新元素需重新确认，不跨体系复用结构或显式网格。

存在活动任务、损坏或未决提交回执时，先处理任务状态，不能绕过保护补全输入。

### 写入失败或意外中断

输入所在文件系统须支持硬链接，VASPKIT 导入用它避免覆盖已有同名文件。
不要同时用编辑器、另一个菜单或脚本修改正在初始化、导入或恢复的同一 Case。

遇到 `rollback needs manual review` 或 `rollback incomplete` 时：

1. 保留错误提示、现有文件与列出的备份，不立即重复初始化。
2. 逐项核对提示中的文件；程序检测到外部修改时会保留修改，并要求人工检查。
3. 确认输入、配置和状态一致后再决定如何继续；不确定时按[恢复章节](recovery.md)整理诊断。

程序会对能捕获的写入异常尝试恢复，但进程被强制终止、系统崩溃或断电后，
**不保证整批文件全部写入或全部撤销，也不保证自动恢复所有文件**。

## 资源与提交范围

初始化不会自动导入脚本里的资源请求。默认
`compute / 1 节点 / 96 tasks / 72:00:00` 会在提交时显式传入，可能覆盖同名 `#SBATCH` 选项。
脚本文字保留不等于实际资源请求不变；受管理启动方式仍限 `mpirun + vasp_std`。
时限、账户、内存及复杂脚本的处理见[服务器适配](CLUSTER_CONFIGURATION.md)。

自动分配可以选择本机分区，不要求存在 compute；查询失败时须先恢复查询，程序不会猜分区。
custom 包装命令不经 Shell 执行；未配置状态查询时只能得到 UNKNOWN，不能据此重提。

`max_inflight` 仅限制当前 Case 的在途任务，不是全用户配额。
部分提交成功时保留已接受的 Job ID；超出上限的点保留 PREPARED，之后需要再次选择提交，
不是退出后自动补交。未决回执或 UNKNOWN 必须先查明原作业状态。

已有计算可使用外置 `job_root` 收集结果；submit/submit-selected 可只读使用外置 `--config`，
但保存资源设置仍要求配置位于 Case 内。已准备扫描点可使用各自的提交脚本，
不要求根目录脚本仍存在；中性提交仍需要根目录脚本，各点脚本仍须通过检查。

## 继续计算与恢复

已有 `vaspsolkit.json` 或 `vaspsolkit.state.json` 时，使用菜单或 status 继续，不删除状态重新 init。
Case 状态与用户级提交回执共同防止重复提交。活动记录默认位于
`${XDG_STATE_HOME:-~/.local/state}/vaspsolkit/cases`，可由 `VASPSOLKIT_STATE_ROOT` 指定。

迁移到新账户、目录或服务器不是经过验证的自动续跑方案。
先核对原 Job ID 与未决回执，不携带旧状态到新位置直接重提，也不切换状态目录绕过提交保护。

`plan`、`status`、`monitor` 可能查询调度器；`plan --offline` 不查询队列。
`--yes` 表示同意写入或提交，不是预览。重算或采集设置变更后重新 collect、audit 和 postprocess；
旧版缺少采集记录时也应重新收集。

队列 COMPLETED 与 VASP 收敛须分开判断。完整输出和 CONVERGED 仍不代替人工科学验收，
具体见[结果检查](results.md)和[计算约定](SCIENTIFIC_CONVENTIONS.md)。

## 安装来源排查

升级后仍显示旧菜单时，在独立 Case 目录执行：

```bash
python -c 'import vaspsolkit; print(vaspsolkit.__version__, vaspsolkit.__file__)'
```

先确认环境与导入路径；旧源码目录或 PYTHONPATH 可能遮蔽新安装，
`python -m vaspsolkit` 也不能绕过这种遮蔽。
同为 0.4.0 不代表相同修订，应同时核对[构建提交](versions.md)。

离线安装要带齐 matplotlib 等依赖，`--no-deps` 不会补齐它们。
自行构建且使用 `--no-build-isolation` 时，需要 `setuptools>=68` 和 `wheel`；
过旧工具可能产生 `UNKNOWN-0.0.0`，不能只凭 pip 返回成功判断产物正确。
具体命令见[安装与升级](INSTALLATION.md)。

## 测试证据与复现

以下记录供核对验证范围，不是使用程序的前置学习内容。

- 测试平台为同一台 Linux x86_64：Python 3.9.17 / pytest 8.3.2 / matplotlib 3.9.4 / setuptools 68.0.0，以及独立 Python 3.12.15 / pytest 9.1.1 / matplotlib 3.11.2 / setuptools 84.2.1 环境。
- 安装测试在含空格路径的新环境中检查安装来源及两个命令入口，覆盖初始化、离线计划、准备、提交预览和合成五点后处理；不是实际五点计算。
- 测试隔离 HOME、状态目录与 PATH。模拟调度器只返回预设 Job ID 或状态，不执行提交脚本；合成 POTCAR 仅供解析，不能用于计算。
- 已覆盖无交互缺参、输入格式、KSPACING、模板保留、取消与重试、重复初始化、部分提交和旧结果更新等路径，并通过模拟故障检查写入失败时的处理。
- VASPKIT 1.5.0 的临时输入生成测试也使用合成解析数据，不是合法赝势库的科学计算验收。
- 上述证据未运行真实 VASP 或访问真实调度器，不能推广为任意操作系统、集群或科学方法已通过验证。
- 在线手册另用 Chromium 检查桌面和手机布局、搜索、复制、目录与键盘导航；这不改变计算程序的系统要求。

测试入口：[文档流程](../tests/test_documented_workflows.py)、[输入适配](../tests/test_input_portability.py)、
[调度器适配](../tests/test_scheduler_portability.py)和[Case 路径检查](../tests/test_vaspsolkit_case_setup.py)。
维护者在具备开发依赖的源码环境中可执行：

```bash
python -m pytest -q tests/test_documented_workflows.py tests/test_documentation_examples.py
python -m pytest -q tests/test_input_portability.py tests/test_scheduler_portability.py
python -m pytest -q tests/test_kspacing_workflow.py tests/test_input_completion.py tests/test_quick_commands.py
```

测试自行隔离状态目录，不需要任何集群命令；安装测试从当前工作树复制包文件到临时目录构建，
不使用只打包已提交 HEAD 的发布脚本。
