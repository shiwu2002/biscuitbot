# 内置 SkillHub CLI（随包分发）

技能商店（SkillHub）官方的命令行工具，**原样内置**，用于在用户没有单独安装 CLI 时
也能走官方安装路径（含签名校验与官方锁文件语义）。

## 来源

| 项 | 值 |
|---|---|
| 下载地址 | `https://skillhub-1388575217.cos.ap-guangzhou.myqcloud.com/install/latest.tar.gz` |
| 该地址的出处 | 官方 bootstrap 脚本 `https://skillhub.cn/install/install.sh` 中的 `CLI_TARBALL_URL` |
| 版本 | `2026.8.5`（见 `version.json`） |
| 工具包大小 | 64145 字节 |
| 工具包 sha256 | `3bbe2ba15ada2eb7a94a2b760fead83be5f4164ab28a6c8b0944dbc539f7e236` |
| 抓取日期 | 2026-09-23 |

## 文件清单（原样，未做任何修改）

| 文件 | 大小 | sha256 |
|---|---|---|
| `skills_store_cli.py` | 226599 | `6c5c89142b32b92d212d7b86821ba1f3cc0d6dc68df6d901e0ef38ece4add797` |
| `skills_upgrade.py` | 7500 | `893374aae5d4941c3ededbd0be96366552ab959ed40a18f648aa30fb65116e05` |
| `version.json` | 28 | `fff513acaf76390d2a9c23c0bdba00054c7db1d3d0473080e14c0f6ebe0c6514` |
| `metadata.json` | 476 | `daccf1c7ae782a3d48183cd32a8bbcb1c0673724f647cbcafb1e4732e7b281fa` |

`skills_upgrade.py` 必须与主脚本**同目录**（主脚本按同目录相对导入它）；
`version.json` / `metadata.json` 是 CLI 读取版本戳与商店端点清单的固定文件名，
缺任一个都会让 CLI 退回内置默认值或报错。

## 有意没有内置的文件

工具包里还有 `cli/install.sh`、`cli/skill/*`（商店自带的一个 workspace 技能）、
`cli/plugin/*`（编辑器插件清单）。这些与本产品无关，内置反而会在用户工作区里
多塞一个技能，故一律不取——兜底下载路径的白名单
（`xianaibot/webui/skill_hub.py` 的 `_CLI_KIT_MEMBERS`）与此保持一致。

## 授权说明

官方工具包**不含 LICENSE 文件**，上游 `skillhub.cn` 亦未在工具包内声明许可条款。
此处按「原样分发 + 记录来源」处理，未做修改，也未附加额外许可声明。

## 升级步骤

内置副本**不会自我升级**（调用时固定带 `--skip-self-upgrade`，并设
`SKILLHUB_SKIP_SELF_UPGRADE=1`），以免改写安装目录或就地上换未审阅的版本。

要跟进上游时手工执行：

```bash
# 1. 取官方工具包，核对 sha256 与版本
curl -fsSL https://skillhub-1388575217.cos.ap-guangzhou.myqcloud.com/install/latest.tar.gz \
  -o /tmp/skillhub-cli.tar.gz
tar -tzf /tmp/skillhub-cli.tar.gz          # 确认成员未变
# 2. 覆盖本目录下四个文件，并同步更新本 README 的版本与 sha256
tar -xzf /tmp/skillhub-cli.tar.gz -C /tmp cli/skills_store_cli.py cli/skills_upgrade.py \
  cli/version.json cli/metadata.json
cp /tmp/cli/*.{py,json} xianaibot/vendor/skillhub/
# 3. 冒烟
uv run python xianaibot/vendor/skillhub/skills_store_cli.py --skip-self-upgrade --version
```

若上游改了 CLI 的调用方式或新增依赖，需同步检查 `xianaibot/webui/skill_hub.py`
的 `_run_cli`（argv 与环境变量）与 `_CLI_KIT_MEMBERS`（白名单）。

## 相关位置

- 解析顺序与调用：`xianaibot/webui/skill_hub.py`（`resolve_cli` / `_run_cli`）
- 兜底下载：同文件 `ensure_cli_available` / `_download_cli_kit`
- 冻结环境跑 Python：`xianaibot/utils/python_shim.py`
- 打包登记：`pyproject.toml` 的 `[tool.hatch.build] include`、`scripts/build-desktop.{ps1,sh}`
  的 `--add-data`
