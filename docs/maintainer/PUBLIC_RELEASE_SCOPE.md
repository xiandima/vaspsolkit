# 公开发布范围

本仓库仅发布通用的 VASPsolKit 源码、正式测试、模板和示例配置。它不包含任何真实体系、VASP 输出、赝势文件、机构专属 SLURM 脚本、历史计算记录或本地助手状态。

已停用界面的源码、内部实施计划和研发设计资料不进入公开仓库。面向用户的科学约定和
集群配置说明属于正式文档。正式运行包不得依赖 Textual，也不得重新导入已归档模块。

发布前必须确认：

1. `git status --ignored` 中没有 VASP 输出、POTCAR、用户 case 或大体积计算目录进入暂存区。
2. `python -m pytest -q` 和 `python tools/build_release.py --outdir /tmp/vaspsolkit-release` 均通过。
3. README 的固定编号、确认口令、调度器能力和已实现菜单一致；未实现的功能不能写成已支持。
4. `tests/test_public_release.py` 通过，源码和构建产物不含用户路径、站点节点名或模块初始化路径。
5. 核对 `CITATION.cff`、README、包元数据与实际 tag 的版本和仓库地址。未发布版本不填写虚构的 date-released。
6. 现有公开历史不因清理文档而重写；只提交审核过的公开改动，不能把内部计算或私有路径引入新提交。

## 工作树与发布产物

`tools/build_release.py` 使用 `git archive HEAD` 构建，因此未提交修复不会进入产物。先用
`python -m build --wheel --no-isolation`（需安装开发依赖）验证当前工作树；审核、提交后再
使用发布脚本，并检查产物的版本与内容。构建旧 HEAD 成功不能证明新修订已经被测试或发布。

离线部署要携带与目标 Python/平台匹配的依赖 wheelhouse；`pip install --no-deps` 不会安装
缺失依赖。测试必须使用 fixture、临时 Case 或 fake scheduler，不得把真实任务提交作为
公开 CI 的必要步骤。公开文档审查依据和已知边界见[审查记录](../DOCUMENTATION_AUDIT.md)。
