# 版本说明

本手册对应 GitHub `main` 上的 0.4.0 开发版，在线地址为[用户手册](https://xiandima.github.io/vaspsolkit/)。
程序和手册从同一提交构建，通过软件测试后自动发布；这不是单独的正式版本标签或 PyPI 发布。

**本次构建的源码提交：** {{ source_revision }}

安装时可在克隆目录运行 `git rev-parse HEAD`，与上述提交核对，再按[安装与升级](INSTALLATION.md)安装。
不能仅凭相同的 0.4.0 版本字符串判断功能一致；还应在计算目录检查实际导入的包来源。
离线测试通过不等同于真实集群或科学方法验收。

[已验证范围](COMPATIBILITY.md) · [GitHub 仓库](https://github.com/xiandima/vaspsolkit)

```{include} ../CHANGELOG.md
:start-after: '# Changelog'
```
