# GitHub Pages 发布

- 在线地址：https://xiandima.github.io/vaspsolkit/
- 源码：`xiandima/vaspsolkit` 的 `main`，程序和手册同一提交。
- 发布来源：GitHub Actions，工作流 `.github/workflows/docs.yml`。
- 版本追溯：网站 `versions.html` 和 `version.json` 记录构建时的 `GITHUB_SHA`。

每次 main 更新都会安装依赖、运行完整单元测试、严格构建 Sphinx、检查生成站点的本地
链接和中文搜索索引，再上传 Pages artifact。只有 main 的非 PR 构建允许部署，部署权限
限制在 deploy job；PR 仅生成预览。预览构建方法见项目 CONTRIBUTING.md。
不要将计算目录、POTCAR、VASP 输出或凭据放入文档。发布后核对 Pages workflow 成功，
并检查网站 version.json 中的提交与仓库 main 一致。

仓库 Settings → Pages → Source 应为 **GitHub Actions**；`github-pages` environment
须允许 main 分支部署。此流程使用 GitHub 官方
[自定义 Pages workflow](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages)。

首次预览曾从 gh-pages 分支发布独立静态快照。该分支可作为历史记录保留，不再是当前
发布来源；不要继续手动更新该分支，否则不能保证程序与文档一致。
