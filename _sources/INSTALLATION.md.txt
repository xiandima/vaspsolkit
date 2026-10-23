# 安装与升级

[手册首页](index.md) · [兼容性说明](COMPATIBILITY.md)

## 服务器可联网

需要 Python >=3.9，建议 Linux 登录节点上的独立 Python 环境。VASP 和 VASPKIT 不由 pip 安装；
先让本机脚本能在集群中正确加载它们。

```bash
git clone https://github.com/xiandima/vaspsolkit.git
cd vaspsolkit
python -m venv "$HOME/.venvs/vaspsolkit"
source "$HOME/.venvs/vaspsolkit/bin/activate"
python -m pip install .
vaspsolkit --help
```

下载源码压缩包的用户，解压后从 `python -m venv` 开始。已经建立环境时只需激活。
安装到自己的环境，不需要管理员权限，也不修改系统 Python。

如果服务器不提供 venv，可使用已有的独立 Conda 环境（需 Python >=3.9），激活后执行
`python -m pip install .`。不要混用另一个环境的 pip 和 python。

## 服务器不能联网

在**与目标集群的操作系统、CPU 架构和 Python 版本兼容**的联网环境中，进入源码根目录：

```bash
python -m pip install "setuptools>=68" wheel
python -m pip wheel --no-deps --no-build-isolation --wheel-dir /tmp/vaspsolkit-wheelhouse .
python -m pip download --dest /tmp/vaspsolkit-wheelhouse /tmp/vaspsolkit-wheelhouse/vaspsolkit-*.whl
```

将整个 `vaspsolkit-wheelhouse` 目录传到集群，激活自己的 Python 环境后安装：

```bash
python -m pip install --no-index --find-links "/path/to/vaspsolkit-wheelhouse" vaspsolkit
vaspsolkit --help
```

目录里必须包含 matplotlib 等依赖，不只是 vaspsolkit 的 wheel。
若提示找不到可用依赖，检查 Python/平台是否一致及文件是否传齐。
`--no-deps` 不是通用的离线安装替代方案。

## 升级

保留原 Case 输入、输出和状态文件。不要在正在写入 Case 的另一个操作期间升级。

若用 Git 下载且源码没有自己的修改：

```bash
cd "/path/to/vaspsolkit-source"
git pull --ff-only
source "$HOME/.venvs/vaspsolkit/bin/activate"
python -m pip install --upgrade .
```

有本地源码修改时，先保存自己的修改并核对差异，不要用强制重置覆盖。
离线用户使用新准备的 wheelhouse，安装命令加 `--upgrade`。

随后**进入 Case 目录**检查来源并查看状态：

```bash
cd "/path/to/my-case"
python -c 'import vaspsolkit; print(vaspsolkit.__version__, vaspsolkit.__file__)'
vaspsolkit status
```

版本号不能单独证明代码已更新，同一开发版也可能有不同修订。版本变化见[版本说明](versions.md)。
已有 Case 继续使用原状态，不重新 init；旧配置的迁移见[使用手册](USER_GUIDE.md#更新旧配置)。

## 命令或版本不对

**提示 vaspsolkit: command not found**

先激活安装时的环境，然后尝试：

```bash
python -m vaspsolkit --help
```

若模块也找不到，请在该环境中重新安装，不要只改命令名称。

**升级后仍显示 PBS 或旧菜单**

在实际运行目录执行：

```bash
python -c 'import vaspsolkit; print(vaspsolkit.__version__, vaspsolkit.__file__)'
```

如果路径指向旧源码，先离开那个源码目录，改在独立 Case 运行；检查是否设置了指向旧目录的
PYTHONPATH。`python -m vaspsolkit` 同样可能被当前目录的旧包遮蔽，重新安装不能消除目录遮蔽。

**用户目录只读或需要隔离程序记录**

指定自己可写的状态和配置目录，路径应在后续会话中保持一致：

```bash
export VASPSOLKIT_STATE_ROOT="/path/to/writable/vaspsolkit-state"
export XDG_CONFIG_HOME="/path/to/writable/config"
```

Case 本身也必须可写。不要在已有活动任务时切换状态目录以绕过提交记录。
测试过的平台及文件系统要求见[兼容性说明](COMPATIBILITY.md)。
