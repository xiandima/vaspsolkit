# 计算与后处理约定

本页用于确认默认工作流是否适合你的计算方法，以及结果能说明什么。
高 R²、文件存在或队列结束都不能代替物理有效性验证。
当前公共包做固定 NELECT 扫描，不运行直接恒电势电子数控制，也不自动适配所有 VASPsol 版本。

## 计算前先核对

| 核对项 | 你需要确认什么 | 不符合时 |
| --- | --- | --- |
| 计算分支 | 实际 VASPsol 版本、补丁和能量输出约定 | 先读下方版本与对齐说明，不直接套用默认转换 |
| 结构与溶剂设置 | 默认中性及扫描点均优化结构，准备阶段会应用 profile | 固定几何或不同溶剂模型须先核对阶段配置 |
| 初猜 | 中性全新密度，扫描 CHGCAR-only，不读取 WAVECAR | 不能把其他重启方案当作同一默认流程 |
| 电势与 SHE | 参考能级和 SHE 标定适合实际方法 | 先确定参考及来源，再开始计算和收集 |
| 多体系比较 | 实际方法可比较，且已有方法记录编号 | 首次 collect 前填写 method_fingerprint，不靠相同编号掩盖差异 |

默认五点包含唯一中性点，根目录中性准备任务不替代扫描中的中性点。
下面的公式与默认参数是程序当前约定，不是跨体系通用推荐。

## 实际数据链

收集结果时，程序按下面的顺序计算电荷、电势和转换后能量：

```text
delta_electrons  = NELECT - NELECT_reference
electrode_charge_e = -delta_electrons
V_ref           = max(planar_average(neutral_LOCPOT, axis=z))
U_vs_SHE        = V_ref - E_Fermi - E_abs(SHE)
E(U)            = TOTEN - delta_electrons * E_Fermi
```

正 `delta_electrons` 表示加电子，不是正电极电荷。NELECT、TOTEN、E_Fermi 来自各点 OUTCAR。
中性参考优先取电子数匹配的根目录输出，否则寻找中性扫描点的 LOCPOT。默认五点中的
目录 `3` 是另一次扫描计算，不是把根目录中性结果重复列入拟合。

**程序取平面平均电势的最大值，没有自动识别真空平台、选择上下表面、检查真空厚度或
排除原子区域峰值。** `V_ref` 只有在人工确认后才可以解释为该方法所需的参考能级。
VASP 官方功函数定义为真空平台与费米能级之差，强调平台和足够的真空区，不能仅凭最大值
断言功函数可靠。[VASP work-function 文档](https://www.vasp.at/wiki/index.php/Computing_the_work_function)

内置 collect 不调用 VASPKIT，也不逐点独立提取真空平台。VASPKIT 可以用于独立复核，但
得到不同数值时须先核对 LOCPOT 的势类型、平面平均方向、平台选择和能量零点。

上述 E(U) 是当前实现的项目处理约定，不是对任意 VASP 输出都成立的通用转换。
`TOTEN` 含电子展宽相关自由能项及所用模型贡献，不是自动得到的完整 Gibbs 自由能；程序
不添加振动 ZPE、气相平动/转动熵、温度/压力/标准态等修正。若改用 E0 或其他能量列，
应重新明确转换公式及来源记录，不能只改图的纵轴名称。

## VASPsol 版本与对齐

| 分支 | 原始资料中的处理 | 当前包边界 |
| --- | --- | --- |
| 原始 VASPsol 电解质工作流 | 某些补丁提供 FERMI_SHIFT，用于费米能级/电解质参考对齐，并要求一致的能量修正 | 不读取也不应用该量 |
| VASPsol++ 固定电子数 | 有离子屏蔽时按其输出约定对齐，不能再机械添加原始 VASPsol 的修正 | 未实现自动版本识别与专用转换 |
| VASPsol++ 直接恒电势 | 输出可已是 grand potential，不能重复做电子库项变换 | 不属于当前固定 NELECT 扫描后处理分支 |

原始版本的对齐规则见 [VASPsol USAGE](https://github.com/henniggroup/VASPsol/blob/master/docs/USAGE.md)；
新版输出与恒电势约定见 [VASPsol++ output](https://github.com/VASPsol/VASPsol#output)。
使用前核对实际源码版本、补丁和输出，不能仅凭目录名或 `LSOL` 判别分支。

当前 `postprocess` 根据列名固定写入 `vacuum-reference-vs-she`、
`toten-minus-delta-electrons-times-efermi`（或 `raw-toten`）和 `fermi_shift_handling=not-applied`。
**不能把外部校正后的数据塞入同名 CSV 列，就认为这些标签真实。** 目前没有通用的外部
能量/电势约定导入接口。此类数据应保留独立处理脚本、公式和真实来源记录，
不能用默认报告标签替代实际方法验证。

## SHE 与方法记录

`workflow.she_reference` 表示所选绝对 SHE 标定值；默认 4.70 只是项目起始值，不是
适用于所有泛函和溶剂模型的普适常数。SHE 标定需要匹配实际模型，参阅
[VASPsol++ SHE calibration](https://github.com/VASPsol/VASPsol#recommended-incar-for-an-aqueous-electrolyte)。
报告数值外，还应记录来源、模型、参考零点、SHE/RHE 定义以及需要时的 pH 转换。
程序确认数值不等于验证了来源；来源字符串可留空，reaction 也不比较来源文本。

`workflow.method_fingerprint` 是**人工填写并由程序比较的字符串**，不是从 INCAR、
KPOINTS、POTCAR 或可执行文件计算的哈希。相同标识不能证明实际参数相同。
需要做 reaction 分析时，应在首次 collect 前按[方法记录步骤](reaction.md#先记录方法)填写。
在 Case 旁保留方法记录，并让此字段引用其稳定编号，至少核对：

- VASP/VASPsol 版本与补丁，固定电子数还是直接恒电势分支。
- 泛函、赝势版本与各元素种类（不公开 POTCAR 内容）、ENCUT、k 点与晶胞策略。
- smearing、电子收敛、离子优化策略、固定原子、ISPIN/MAGMOM 和最终磁态。
- 色散、偶极修正、溶剂/电解质参数与势的输出类型。
- 电荷符号、能量转换、参考能级、SHE 标定及自由能修正来源。

不同吸附组成无需 POSCAR 或整个 POTCAR 文件逐字相同，但共同元素赝势、基底/晶胞及数值
方法必须可比较。`summary_sha256` 只证明汇总文件版本，不证明上述物理一致性。

## U_vertex 与 PZC

```text
E(U) = a U^2 + b U + c
U_vertex = -b / (2a)
q(U) = m U + n
pzc_estimate = -n / m
```

二次顶点 `U_vertex` 不是自动验证的 PZC。兼容字段 `u0` 仍指同一顶点，`pzc` 不再
无条件赋值。有电极电荷数据时，程序另行线性拟合 q(U)，报告零点及是否落在拟合区间。
该电荷来自相对 `NELECT_reference` 的差值；只有参考确实电中性且响应分支适用时，才有
物理 PZC 的意义。零点估计不是独立实验校准，也没有自动的线性质量/磁态连续性验收。

曲率导出的 `-2a` 只有在合适的能量约定和电容模型下才有电容含义；它不是已经完成界面数、
面积和单位换算的面积比电容，不能直接当成微法每平方厘米。

## 五点验收

`postprocess` 自动要求原始数据恰有五点、恰有一个 `delta_electrons=0`，且默认全部纳入。
显式 `--allow-excluded` 是诊断性豁免，图例显示纳入数/原始数和排除目录，报告保留记录；
它不满足“五点含中性”的最终验收。低层 `analyze` 不提供同一保护。

`postprocess` 只读 CSV，不重查 OUTCAR，缺失 `converged/fit_included` 标志可能按默认值纳入。
应先运行 collect 和 audit，再确认实际正常结束、电子/离子收敛以及输出完整性。
以下项目须人工验收，程序未自动完成：

- U 随电荷变化是否单调，目标电势是否由实际点覆盖。
- 各点是否保持可比较的构型、磁态和电子分支；逐点优化可产生真实结构差异。
- 中性点的能量/势零点、参考文件和初猜是否与其他点一致；不能仅凭 R² 归因于 WAVECAR。
- 查看每个原始点、残差及方法记录；三参数二次拟合中五点的高 R² 不代表充分验证。

`analysis.json.fit_domain` 描述实际参与拟合的 U 区间。当前 E-U 图两端额外延伸用于显示，
曲线 CSV 也在拟合点区间两侧各扩展 20%；图像绘制范围不是采样范围，外推不增加计算点。
`eu_curve.csv` 为派生数据，原始点应从 summary.csv 和 analysis.json 复查。

## INCAR profile 行为

profile 是程序的阶段参数配置。初始化时保留模板，不代表后续准备不改参数。
执行菜单 `20` 和 `30` 后，分别核对根目录与各扫描目录的 INCAR。以下为默认值，不是跨体系推荐方法：

| 类别 | 行为 |
| --- | --- |
| 通用缺省补充 | PREC=Accurate、EDIFF=1E-5、NELM=300、ALGO=Fast、ISMEAR=0、SIGMA=0.05、LREAL=Auto、LASPH=.TRUE.、ISYM=0 |
| relax profile 覆盖 | IBRION=2、NSW=200、POTIM=0.2、ISIF=2、EDIFFG=-0.05 |
| static profile 覆盖 | IBRION=-1、NSW=0 |
| VASPsol profile 覆盖 | LSOL=.TRUE.、ISOL=1、EB_K=80、TAU=0、LAMBDA_D_K=3.0、LVHAR=.TRUE.、LCHARG=.TRUE. |
| 缺失 ENCUT 建议 | 1.3 × 最大 ENMAX，向上取到 5 eV 的倍数，不替代收敛测试 |

`vaspsol-neutral-relax` 与 `vaspsol-charge-relax` 还关闭 LWAVE，分别采用中性全新密度和
CHGCAR-only 初猜。显式 profile 会覆盖 profile 标签；通用缺省仅补缺；`--set` 最后应用。
保留已有 INCAR 的初始化分支不等于应用整个 profile，且可能因必要溶剂标签冲突而拒绝。
不同溶剂模型（尤其 ISOL/离子屏蔽）不能未经检查就套入此路径。默认 TAU=0 等选择影响
物理项，应根据所用方法确认。

## 反应层边界

默认比较 `she_reference_eV`、`potential_convention`、`energy_convention`、
`fermi_shift_handling`、`method_fingerprint`，并要求请求网格位于所有体系共同 fit_domain。
它不校验实际计算文件、来源文献、气相常数、化学计量或单位。

反应配置的公式只允许 E、数值、常数、U 及基本四则运算，不执行任意 Python。
建议始终使用 `E("system", U)`，在 E 外组合系数和常数。当前解析器也接受 E 的电势参数
表达式，但范围保护检查的是请求 U 网格，**不会传播检查 `E("system", U+10)` 等变换后的
实际取值范围**。不要用它绕过共同区间要求。

```json
{
  "compatibility": {
    "allow_unknown_provenance": false,
    "allow_mixed_provenance": false,
    "allow_extrapolation": false
  }
}
```

这是 spec 的部分字段，三个开关分别允许缺失来源、已知约定不一致、越界外推。任一启用
都会在报告披露；未知来源不会跳过已有的有效域检查，未知区间也不会伪造成绘图区间。
已有区间若非有限数或顺序颠倒仍拒绝；各体系完全无公共区间时，外推开关也不放行。
报告保存路径、来源信息、常数、公式和警告，但不会替你归档所有实际输入文件。

输出的能量表达式不自动成为 Gibbs 自由能。命名为 G_NO 或修改纵轴标题不会
添加物理修正；反应式、标准态、温度/压力以及热力学或动力学含义由用户提供和验证。
热力学曲线交点不等于反应势垒或动力学速控步骤。

## 核对实现

需要追溯具体处理时，可查阅 [workflow.py](../vaspsolkit/workflow.py)、
[analysis.py](../vaspsolkit/analysis.py)、[postprocess.py](../vaspsolkit/postprocess.py) 和
[inputs.py](../vaspsolkit/inputs.py)。日常检查先按[质量报告与结果验收](results.md)操作。
