# E-U 结果与示例

本页从“五点已完成计算”开始。尚有任务未结束，请先回到[收敛检查](workflow.md#收敛收集和拟合)。
使用顺序是：**检查各点输出 → 详细审计 → 原始五点 → 拟合曲线与残差**。

## 先读质量报告

菜单选 `50` 检查五点，全部为 CONVERGED 后选 `60` 收集，再选 `61` 审计。
`02` 不会自动执行 `61`，推荐拟合也不表示人工审查已经完成。

**报告生成成功不等于质量合格。** 命令显示 `wrote ...` 或正常退出，只表示相应操作完成。
请打开 `results/quality_report.csv`，逐点查看收敛标志和 `warnings` 列。
有警告时按[字段对照表](#警告字段查询)核对对应输出；警告为空只表示未触发当前自动检查，
仍须人工核对结构、磁态和电势参考。

`results/points_to_rerun.csv` 汇总带警告的点，是人工检查清单，不是自动批准重算的指令。
警告阈值不是跨体系的科学验收标准；有真实结构或磁态变化时，应解释分支而非盲目重算。

默认扫描原始文件位于 `charge_sweep/1` 至 `charge_sweep/5`，根目录保存中性准备任务输出。
若发现异常，先按[检查与恢复](recovery.md)处理，再重新 collect、audit 和 postprocess。
五点全部可接受后选 `62`，或使用[直接拟合命令](workflow.md#收敛收集和拟合)。

## 输出文件

| 文件 | 怎么用 |
| --- | --- |
| `results/summary.csv` | 五点汇总，核对原始能量、电荷、计算得到的电势与转换后能量；不是所有列都来自 VASP 原始输出 |
| `results/quality_report.csv` | audit 的详细质量表，先读 warnings |
| `results/points_to_rerun.csv` | 带警告点的检查清单 |
| `results/eu/eu_curve.png` | 核对原始点与二次拟合曲线 |
| `results/eu/eu_curve.csv` | 由拟合式生成的曲线，不是额外计算点 |
| `results/eu/analysis.json` | 查看原始点、系数、残差、fit_domain 和来源记录 |
| `results/eu/report.md` | 查看顶点、R²、RMSE、最大残差及排除信息 |

**拟合完成信号：** `results/eu/` 中生成上述图、CSV、JSON 和报告，随后还要做下方的结果验收。
`eu/` 内的质量表只根据汇总表标志生成，不能替代 `results/` 的详细审计。
postprocess 只读取 CSV，不重新检查 OUTCAR；缺失的质量标志可能按默认值纳入，不能据此证明收敛。

再次分析会覆盖输出目录中的同名文件。保留旧结果时，先备份或使用新的输出目录，例如：

```bash
vaspsolkit postprocess --summary results/summary.csv --output results/eu-review-02
```

不要把输出指定为 `results/`，否则简要质量表会覆盖详细审计表。
修改输入、重算或更改收集设置后，先重新收集和审计，不直接复用旧 CSV 拟合。

## 怎么判断结果是否可用

1. 原始数据确有五点，且唯一中性点参与拟合；默认五点全部纳入。
2. 各点正常结束、电子与离子收敛、输出完整，结构与磁态可比较。
3. 所用 VASPsol 分支、SHE 标定、能量与电势对齐符合实际方法，并有来源记录。
4. 查看每个点的残差，不仅比较 R²；不通过删中性点或使用 `--allow-excluded` 追求更漂亮的最终曲线。
5. 用 `analysis.json.fit_domain` 确认目标电势在实际拟合点范围内，不用图或曲线 CSV 的两端代替取样范围。

图和曲线 CSV 在两端各扩展取样跨度的 20% 用于显示，延伸部分不增加计算证据。
五点的高 R² 不能代替结构、磁态或能量零点的一致性检查。

二次顶点 U_vertex 不自动等于 PZC，当前电子能量不是自动修正好的完整 Gibbs 自由能。
内置电势处理取中性 LOCPOT 平面均值的最大值，不自动识别真空平台，也不应用 FERMI_SHIFT。
公式、适用前提和人工验收项见[计算约定](SCIENTIFIC_CONVENTIONS.md)。

要组合多个体系，继续[反应分析](reaction.md)，先确认方法记录完整，不用诊断豁免代替方法一致性。

## 合成示例：认识一张 E-U 图

下面的数据由解析式 **E(U) = -10 - (U - 0.2)²** 生成，不是 VASP 计算结果，不对应任何真实材料。
它只用于检查后处理和展示输出，不能用于论文结论或物理参数标定。

![合成五点 E-U 拟合，五个点全部纳入，顶点为 0.2 V。](_static/example-eu.png)

图由 postprocess 生成，包含五个点及唯一的零电子偏移点。
示例故意采用精确二次数据，所以 R² 为 1；真实计算不应以“必须达到同样的 R²”为验收标准。

[下载合成五点 CSV](_static/example-summary.csv)，放到独立演示目录，再执行：

```bash
vaspsolkit postprocess --summary example-summary.csv --output example-eu
```

**完成信号：** `example-eu/` 内生成图、曲线 CSV、`analysis.json` 和 `report.md`。
这个演示不运行 VASP、不查询或提交集群任务。CSV 的 converged 标志仅为演示填写，
不代表进行过真实收敛检查；synthetic 方法标识也不是可用于真实反应比较的方法记录。

若命令报找不到 CSV，确认当前目录和下载文件名；若改动后的数据被拒绝，先检查五点与中性点，
不要把诊断性排除作为完成演示的必要步骤。

## 警告字段查询

在详细质量表中遇到警告时，再按名称查这里。检查完成后，回到[结果验收](#怎么判断结果是否可用)。

| 字段或警告 | 含义 | 应检查什么 |
| --- | --- | --- |
| converged 为 0 / unconverged | 收集器未判定该点收敛 | 回看该点 OUTCAR 与 `50` 的诊断，不纳入最终拟合 |
| nelect_mismatch | 输出电子数与计划偏移不一致 | 核对参考电子数、该点 INCAR 和 OUTCAR |
| incar_outcar_nelect_mismatch / incar_outcar_echo_nelect_mismatch | 当前输入与输出中的电子数不一致 | 核对实际运行的输入，不能把旧输出当作新计算 |
| outcar_echo_nelect_missing | OUTCAR 缺少可核对的输入电子数记录 | 检查输出完整性与版本，不把缺记录当作一致 |
| input_modified_after_start | 输入的修改时间晚于输出记录的启动时间 | 确认计算期间是否改过输入，保留实际运行来源 |
| seed_mixing_mesh_changed | 检测到初猜密度与混合网格相关提示 | 回看 CHGCAR 来源和 OUTCAR，不能仅凭此项判定能量错误 |
| spin_jump | 相邻扫描点磁矩变化超过审计阈值 | 核对结构和磁态是否属于可比较分支 |
| high_residual | 某点拟合残差超过审计阈值 | 查看原始点、残差及结构变化，不单纯追求更高 R² |
| warnings 为空 | 未触发当前自动警告 | 仍须完成结构、磁态、电势参考等人工验收 |
