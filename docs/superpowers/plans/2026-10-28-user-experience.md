# 用户手册与 CLI 体验实施计划

> **For agentic workers:** 使用 subagent-driven-development，按写入范围分工并独立审查；操作检查使用临时目录和模拟调度器。

**Goal:** 用户能从自身任务找到正确步骤，并判断执行结果和下一步。

**Architecture:** 保留 Sphinx/RTD，局部模板负责导航与页内目录，CSS 负责响应式阅读布局，小型本地 JS 增强目录和键盘可用性。Markdown 是内容来源；CLI 只修复审查已复现的问题。

**Tech Stack:** Python、Sphinx、MyST、RTD、pytest、Playwright。

## 1. 审查与基线

- [x] 检查本地与远程 main 一致，记录已发布提交。
- [x] 两路只读审查：CLI 用户流程；中文内容与信息架构。
- [x] 线上浏览器基线：搜索、复制、图片、移动导航均可用。
- [x] 完整 pytest 基线通过：943 passed。

## 2. 阅读界面（主线程）

文件：`docs/conf.py`、`docs/_templates/layout.html`、`docs/_static/manual.css`、`docs/_static/manual.js`、`MANIFEST.in`、`tests/test_docs_site.py`。

- [x] 先增加模板、静态资源和构建后页内导航的契约测试，运行 `python -m pytest tests/test_docs_site.py -q` 确认新测试失败。
- [x] 增加本地模板和 JS 配置，打包 HTML/JS 资源。桌面三列，窄屏正文内折叠目录；保留原搜索与复制。
- [x] 原生按钮负责移动导航，增加跳转正文入口和清晰焦点。目录无需 JS 仍可跳转，JS 仅增加当前位置。
- [x] 运行上述测试及 `python -m sphinx -n -W --keep-going -b html docs BUILD`。

## 3. 用户语言（文档执行者）

文件：`README.md`、`docs/index.md`、`docs/quickstart.md`、`docs/USER_GUIDE.md`、输入/流程/恢复/结果页面及相应文档测试。

- [x] 首页明确首次使用、继续任务、异常和结果入口；README 作为简短入口。
- [x] 快速入门逐步写出操作、完成信号和下一步；提供一个真实菜单片段与清楚标记的示例输出。
- [x] 精简重复技术说明，恢复页提供症状到处理步骤的表格；科学边界和批次提交限制保留。
- [x] 执行 `python -m pytest tests/test_documentation_examples.py tests/test_documented_workflows.py -q`，保持锚点和命令有效。

## 4. CLI 已复现问题（程序执行者）

- [x] 根据审查证据限定修复文件，先增加失败回归测试，再最小修改。
- [x] 保留所有命令参数和菜单编号、确认提交、UNKNOWN 阻止重提以及五点含中性约束。
- [x] 运行针对性测试和完整 pytest：Python 3.9/3.12 均为 1006 passed。

## 5. 独立审查与发布

- [x] 第二轮分别检查规格符合性与实现质量，修复确认问题。手机侧栏与键盘问题、概览/详细诊断文案均经独立复核关闭。
- [x] 浏览器实测 1440、1024、390、360 宽度：导航、页内目录、搜索、复制、图片、无溢出与无 JS 错误；覆盖本地 file 页面。独立复核手机点击关闭、Tab/Shift+Tab 与断点切换。
- [x] 完整测试、严格构建和本地链接验证；候选 wheel/sdist 通过路径审计，源码目录外安装及五点示例通过。最终提交的产物在推送前重建复核。
- [ ] 更新变更说明，检查没有敏感路径或计算文件后提交并推送 main。
- [ ] 确认 CI/Pages 成功，核对线上 version.json 提交与 main 一致，浏览器复核线上版本。

此清单记录发布准备时的状态。发布后的运行结果以 GitHub Actions、站点 version.json
及外部浏览器验收报告为准，不为回写清单再触发一次无内容变化的部署。

## 设计参考

- https://docs.astral.sh/uv/ ：按任务区分入门、指南与参考。
- https://numpy.org/doc/stable/user/absolute_beginners.html ：区分输入与输出、提供页内导航。
- https://vaspkit.com/tutorials.html ：编号菜单与终端操作实例。

仅借鉴组织方式，页面内容根据本项目实际行为独立编写。
