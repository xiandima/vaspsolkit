# 菜单与命令查询

在 Case 目录运行 `vaspsolkit` 打开菜单。写入操作需要确认，菜单提交须输入 SUBMIT，
取消排队任务须输入 CANCEL。`--yes` 表示同意执行，不是预览。

## 常用任务

| 菜单 | 操作 | 直接命令 |
| --- | --- | --- |
| `02` | 执行推荐下一步 | `vaspsolkit next` |
| `03` | 查看完整状态与诊断 | `vaspsolkit status` |
| `10` | 检查基础输入 | 菜单内只读检查 |
| `11` | 初始化 Case | `vaspsolkit init` |
| `12` | 配置指定节点与资源 | `vaspsolkit configure-scheduler` |
| `13` | 设置 SHE 参考 | `vaspsolkit configure-reference` |
| `14` | 补齐输入 | `vaspsolkit inputs` |
| `20` | 准备中性计算 | `vaspsolkit prepare-neutral` |
| `21` | 提交中性任务 | `vaspsolkit submit-neutral` |
| `22` | 检查中性收敛 | `vaspsolkit check-neutral` |
| `30` | 准备五点 | `vaspsolkit prepare-charge` |
| `31` | 提交前检查 | `vaspsolkit check-prepared` |
| `32` | 选择扫描点提交 | `vaspsolkit submit-selected 1 2 3 4 5` |
| `40` | 监测已记录作业 | `vaspsolkit monitor` |
| `50` | 收敛与错误检查 | `vaspsolkit check` |
| `51` | 修复一个失败点 | `vaspsolkit repair --job 3 --no-submit` |
| `60` | 收集结果 | `vaspsolkit collect` |
| `61` | 详细审计 | `vaspsolkit audit` |
| `62` | 五点拟合 | 见下方输出命令 |
| `90` | 查看菜单帮助 | `vaspsolkit --help` |
| `00` | 退出 | 不取消服务器作业 |

菜单 `41` / `42` 涉及取消排队任务，先阅读[恢复章节](recovery.md#取消排队任务或换节点)。

## 查询参数

```bash
vaspsolkit --help
vaspsolkit inputs --help
vaspsolkit submit-neutral --help
vaspsolkit postprocess --help
vaspsolkit reaction --help
```

## 常用预览

```bash
vaspsolkit next --dry-run
vaspsolkit inputs --dry-run
vaspsolkit plan --offline
vaspsolkit submit-neutral --dry-run
```

缺少补全参数时，inputs 的 dry-run 会列出需要的信息。离线计划和提交预览不代表集群环境已经验证。

## 输出五点拟合

```bash
vaspsolkit postprocess --summary results/summary.csv --output results/eu
```

不要输出到 results 根目录，以免覆盖详细审计表。不同反应的组合使用独立的[反应配置](reaction.md)。
