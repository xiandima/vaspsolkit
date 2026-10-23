# Main 与用户手册集成验收计划

**Goal:** 审查并修复待发布程序，以同一 main 提交构建安装包和在线手册。

**Architecture:** 在隔离目录对待发布树审查、执行离线测试并构建真实 wheel/sdist；保留用户原工作树，核对后同步。Pages 改用 main 的 Actions 部署，部署前必须通过程序与文档测试。

**Tech Stack:** Python 3.9 / 3.12、pytest、setuptools/build、Sphinx/MyST、GitHub Actions/Pages。

## 验收步骤

- [x] 记录本地待发布文件与远程 main 基线；独立审查输入与调度流程。
- [x] 运行现有完整测试；对审查发现的问题补充失败回归并修复。
- [x] 扩展 tests/test_documented_workflows.py 的 wheel 安装检查，覆盖独立 Case 的初始化、离线计划、准备、提交预览和五点后处理。
- [x] 修正 .github/workflows/tests.yml 的源码遮蔽问题，并增加独立安装来源断言。
- [x] 更新 .github/workflows/docs.yml：完整测试、严格构建、链接检查、只从 main 部署；更新 tests/test_docs_site.py 的部署约束。
- [x] 更新 README、版本/兼容性页面和发布维护说明，移除程序尚未合入 main 的过时提示。
- [x] 在 Python 3.9 和独立 Python 3.12 环境跑完整测试：交叉审查修复后各 943 项通过。
- [ ] 从待推送提交构建并扫描 wheel/sdist。
- [ ] 核对审查结果、公开文件清单和原工作树变化，提交并非强制推送 main。
- [ ] 检查远程 CI、Pages 部署、线上搜索/图像及文档提交标识；记录真实 HPC 验收边界。
