# VASPsolKit 用户手册

本手册面向 Linux / SLURM 上的 VASP/VASPsol 使用者。每个体系使用一个独立计算目录（Case），
从输入准备开始，完成中性优化、五点电荷扫描和 E-U 分析，不需要预先算好中性参考。

当前为 `0.4.0` 开发版，手册与 GitHub `main` 的同次程序提交一起构建。
安装或升级时，可在[版本说明](versions.md)核对源码提交。

## 第一次使用

先完成[安装](INSTALLATION.md)，再按[第一次计算](quickstart.md)逐步操作。
教程会说明每一步写入什么、看到什么才能继续，以及异常时到哪里检查。
只想先认识输出、不运行 VASP，可查看[合成示例](results.md#合成示例认识一张-e-u-图)。

开始前请备好 VASP/VASPsol、合法赝势和本机运行脚本，并核对[计算约定](SCIENTIFIC_CONVENTIONS.md)与自己的方法相符。
默认资源不是通用推荐值，换服务器先读[服务器适配](CLUSTER_CONFIGURATION.md)。

## 继续已有计算

进入原 Case 打开菜单，不重新初始化，也不删除状态文件：

```bash
cd "/path/to/my-case"
vaspsolkit
```

选 `03` 查看阶段与诊断，选 `02` 执行当前推荐的一步。
`02` 不会自动执行 `61` 详细审计；收集结果后须主动审查，再拟合。
选 `00` 退出不取消服务器作业，程序也不会在退出后自动补交下一阶段。

| 当前情况 | 去哪里继续 |
| --- | --- |
| 缺 INCAR、POTCAR 或采样 | [准备输入与 VASPKIT](inputs.md) |
| 要改分区、节点或任务数 | [服务器与资源配置](CLUSTER_CONFIGURATION.md#选择资源) |
| 中性任务已提交，正在等待 | [中性检查](quickstart.md#4-等待并检查中性点) |
| 中性状态已是 CONVERGED | [五点准备与提交](workflow.md#五点准备与提交) |
| 五点仍有 PREPARED，需要提交下一批 | [五点准备与提交](workflow.md#五点准备与提交) |
| FAILED、NEEDS_REVIEW、UNKNOWN 或提交断线 | [检查与恢复](recovery.md) |
| 五点完成，准备收集与审计 | [结果检查](quickstart.md#6-收集并审查五点) |
| 已生成 E-U 图，想判断能否使用 | [结果验收](results.md#怎么判断结果是否可用) |
| 要组合多个体系 | [先记录方法](reaction.md#先记录方法)，再做反应分析 |

## 计算前确认

- **计算方式：** 固定 `NELECT` 扫描后建立 E-U 关系，不是直接恒电势电子数求解器。
- **五点：** 包含唯一中性点，不静默删点；独立中性准备任务加五点扫描，默认共 **1 + 5 个任务**。
- **初猜：** 中性计算从全新密度开始，扫描使用中性 CHGCAR（CHGCAR-only），不读取 WAVECAR。
- **任务：** UNKNOWN 先查原 Job ID，不重提；队列 COMPLETED 不等于 VASP 收敛。
- **结果：** 程序检查不能替代结构、磁态和电势参考验收；顶点不自动等于 PZC，电子能量不自动等于完整 Gibbs 自由能。

完整命令见[命令查询](commands.md)，测试范围见[兼容性说明](COMPATIBILITY.md)。
[合订使用手册](USER_GUIDE.md)供集中查询，不是另一套入门路线。

```{toctree}
:hidden:
:caption: 开始使用
:maxdepth: 2

INSTALLATION
quickstart
```

```{toctree}
:hidden:
:caption: 计算流程
:maxdepth: 2

inputs
CLUSTER_CONFIGURATION
workflow
recovery
```

```{toctree}
:hidden:
:caption: 结果与方法
:maxdepth: 2

results
reaction
SCIENTIFIC_CONVENTIONS
```

```{toctree}
:hidden:
:caption: 查询与参考
:maxdepth: 1

commands
COMPATIBILITY
USER_GUIDE
versions
```
