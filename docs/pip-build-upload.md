# biscuitbot 打包与上传 PyPI 操作文档

## 1. 更新版本号

修改以下两处，版本号必须一致：

| 文件                    | 位置                                     | 示例                     |
| --------------------- | -------------------------------------- | ---------------------- |
| `pyproject.toml`      | `version = "0.2.2"`                    | 改为 `version = "0.2.3"` |
| `biscuitbot/__init__.py` | `_read_pyproject_version() or "0.2.2"` | 改为 `"0.2.3"`           |

## 2. 清理旧包和编译缓存

```bash
rm -f dist/*
find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null
find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null
find . -type d -name "build" -maxdepth 1 -exec rm -rf {} + 2>/dev/null
```

## 3. 编译打包

```bash
python -m build
```

生成文件：

- `dist/biscuitbot-{版本}-py3-none-any.whl` — Wheel 包
- `dist/biscuitbot-{版本}.tar.gz` — 源码包

## 4. 上传到 PyPI

```bash
twine upload dist/*
```

交互式输入：

- **Username**: `__token__`
- **Password**: `pypi-xxxx...`（从 [pypi.org](https://pypi.org/) 账户设置 → API Token 获取）

## 5. 验证

```bash
pip install --upgrade biscuitbot
biscuitbot -v
```

## 附：API Token 配置（免交互上传）

创建 `~/.pypirc`：

```ini
[pypi]
username = __token__
password = pypi-xxxx...
```

之后 `twine upload dist/*` 即可免输入直接上传。
