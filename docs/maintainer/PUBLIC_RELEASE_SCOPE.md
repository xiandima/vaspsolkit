# 公开发布范围

本仓库仅发布通用的 VASPsolKit 源码、正式测试、模板和示例配置。它不包含任何真实体系、VASP 输出、赝势文件、机构专属 PBS 脚本、历史计算记录或本地助手状态。

已停用界面的源码、内部实施计划和研发设计资料不进入公开仓库。正式运行包不得依赖
Textual，也不得重新导入已归档模块。

发布前必须确认：

1. `git status --ignored` 中没有 VASP 输出、POTCAR、用户 case 或大体积计算目录进入暂存区。
2. `python -m pytest -q` 和 `python tools/build_release.py --outdir /tmp/vaspsolkit-release` 均通过。
3. README 的固定编号、确认口令、调度器能力和已实现菜单一致；未实现的功能不能写成已支持。
4. `tests/test_public_release.py` 通过，源码和构建产物不含用户路径、站点节点名或模块初始化路径。
5. GitHub 仓库创建后，在 `CITATION.cff` 与 README 中补充真实仓库地址和维护者信息。
6. 首次公开推送使用经过审查的根提交，不发布包含本机路径的内部开发历史。
