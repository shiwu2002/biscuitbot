"""SkillHub 技能商店后端（浏览 / 搜索 / 安装 / 升级检查 / 签名校验）。

所属模块与项目作用
===================
本文件位于 ``xianaibot/webui`` 目录，负责把外部技能商店 SkillHub 接入 WebUI：

- 通过**子进程**调用本机已安装的 SkillHub CLI（默认
  ``~/.skillhub/skills_store_cli.py``），复用官方的 install / upgrade / verify 与
  ``.skills_store_lock.json``，而不是自己重写下载、版本对比与签名校验；
- 技能装入 ``<workspace>/skills/``，由 SkillHub 自己维持 ``@<handle>/<slug>/``
  命名空间布局（``SkillsLoader`` 已识别该布局并按扁平名暴露技能）；
- 对外提供 status / catalog / search / installed / install / updates / verify。
  卸载不在此重复实现——沿用 ``webui/skills_api.delete_workspace_skill``（它已支持
  删除商店技能并同步摘除 lockfile 条目）。

安全约束：
- 一律以 argv 列表调用，**绝不拼接 shell 字符串**；
- ``slug`` / ``namespace`` 必须匹配 ``_SAFE_ARG_RE`` 且不得以 ``-`` 开头，否则
  拒绝进入 argv，杜绝参数注入；
- ``cliPath`` / ``pythonPath`` 只从配置读取，**绝不接受 HTTP 传入**（与
  talent-market 对注册表 URL 的策略一致）；
- CLI 缺失时降级为 ``available: false`` 空态，而不是抛错。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from loguru import logger

from xianaibot.config.schema import SkillHubConfig
from xianaibot.utils.helpers import atomic_write_json

# SkillHub CLI 默认安装位置（官方 install.sh 的 INSTALL_BASE / CLI_TARGET）
_DEFAULT_CLI_DIR_NAME = ".skillhub"
_CLI_SCRIPT_NAME = "skills_store_cli.py"
_VERSION_FILE_NAME = "version.json"
# 技能商店在技能根下维护的锁文件（记录已安装技能的 slug / version / installDir）
_LOCKFILE_NAME = ".skills_store_lock.json"

# 允许作为 CLI 参数的值。技能 slug / 命名空间 handle 的实际取值都在此字符集内。
_SAFE_ARG_RE = re.compile(r"^[A-Za-z0-9._-]+$")
# ``skill rankings --type`` 的合法取值
_RANKING_TYPES = frozenset({"all", "hot", "featured", "newest", "recommended", "trending", "paid"})
# 解析 ``upgrade --check-only`` 汇总行：upgrade done: checked=1 upgraded=0 skipped=1 failed=0 dir=...
_UPGRADE_SUMMARY_RE = re.compile(
    r"checked=(\d+)\s+upgraded=(\d+)\s+skipped=(\d+)\s+failed=(\d+)"
)

_MAX_OUTPUT_CHARS = 4_000

# 写操作串行化：install / 摘除 lockfile 都会改动同一技能根与锁文件。
# gateway 为单进程，handler 跑在 asyncio.to_thread 里，故用线程锁即可。
_WRITE_LOCK = threading.Lock()


class SkillHubError(ValueError):
    """SkillHub 操作失败（含建议的 HTTP 状态码）。"""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def _truncate(text: str, limit: int = _MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return text[:limit] + f"\n\n... truncated {omitted} characters ..."


def resolve_cli_path(cfg: SkillHubConfig) -> Path | None:
    """定位 SkillHub CLI 脚本；未安装时返回 None。"""
    configured = (cfg.cli_path or "").strip()
    candidate = (
        Path(configured).expanduser()
        if configured
        else Path.home() / _DEFAULT_CLI_DIR_NAME / _CLI_SCRIPT_NAME
    )
    try:
        return candidate if candidate.is_file() else None
    except OSError:
        return None


def _is_usable_python(candidate: str) -> bool:
    """探针：确认候选解释器真的能跑 Python 3。

    Windows 上 ``shutil.which("python3")`` 可能返回微软商店的占位壳——文件存在、
    可执行，但一运行就报「Python was not found」。必须先探一次再采用。
    """
    try:
        probe = subprocess.run(
            [candidate, "-c", "import sys; print(sys.version_info[0])"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0 and (probe.stdout or "").strip().startswith("3")


def _resolve_python(cfg: SkillHubConfig) -> str:
    """定位用于运行 CLI 的解释器。

    桌面端由 PyInstaller 打包，此时 ``sys.executable`` 是冻结的宿主程序而非
    Python 解释器，直接拿它去跑脚本会失败，因此改为在 PATH 上探找可用的解释器。
    """
    configured = (cfg.python_path or "").strip()
    if configured:
        return configured
    if not getattr(sys, "frozen", False):
        return sys.executable
    for name in ("python3", "python"):
        found = shutil.which(name)
        if found and _is_usable_python(found):
            return found
    raise SkillHubError(
        "找不到可用的 Python 解释器来运行 SkillHub CLI"
        "（可在配置 gateway.skillHub.pythonPath 指定）",
        status=503,
    )


def _read_cli_version(version_file: Path) -> str:
    try:
        data = json.loads(version_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(data.get("version") or "") if isinstance(data, dict) else ""


def skills_root_of(workspace: Path) -> Path:
    """技能商店的安装根目录（与 SkillsLoader 的工作区技能目录一致）。"""
    return Path(workspace) / "skills"


def skillhub_status(cfg: SkillHubConfig, *, workspace: Path) -> dict[str, Any]:
    """返回商店可用性快照，供前端决定渲染商店页还是安装引导空态。"""
    cli = resolve_cli_path(cfg)
    root = skills_root_of(workspace)
    if cli is None:
        return {
            "available": False,
            "enabled": cfg.enable,
            "cli_path": "",
            "version": "",
            "skills_dir": str(root),
            "reason": "未检测到 SkillHub CLI（~/.skillhub/skills_store_cli.py）",
        }
    return {
        "available": True,
        "enabled": cfg.enable,
        "cli_path": str(cli),
        "version": _read_cli_version(cli.parent / _VERSION_FILE_NAME),
        "skills_dir": str(root),
        "reason": "" if cfg.enable else "SkillHub 已在配置中禁用",
    }


def _require_cli(cfg: SkillHubConfig) -> Path:
    if not cfg.enable:
        raise SkillHubError("SkillHub 已在配置中禁用", status=403)
    cli = resolve_cli_path(cfg)
    if cli is None:
        raise SkillHubError(
            "未检测到 SkillHub CLI，请先按官方文档安装（~/.skillhub/skills_store_cli.py）",
            status=503,
        )
    return cli


def _validate_arg(value: str, field: str, *, required: bool = True) -> str:
    """校验将进入 argv 的用户输入，拒绝注入与选项混淆。"""
    text = str(value or "").strip()
    if not text:
        if required:
            raise SkillHubError(f"{field} 不能为空")
        return ""
    # 以 ``-`` 开头的值会被 argparse 当作选项，必须挡住。
    if text.startswith("-") or not _SAFE_ARG_RE.match(text):
        raise SkillHubError(f"非法的 {field}：只允许字母、数字、点、下划线与连字符")
    return text


def _run_cli(
    args: list[str],
    *,
    cfg: SkillHubConfig,
    cli: Path,
    timeout: int | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """以 argv 列表运行 SkillHub CLI（不经过 shell）。"""
    argv = [_resolve_python(cfg), str(cli), "--skip-self-upgrade", *args]
    effective_timeout = timeout or cfg.timeout
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=effective_timeout,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
    except subprocess.TimeoutExpired as exc:
        raise SkillHubError(f"SkillHub CLI 超时（{effective_timeout}s）", status=504) from exc
    except OSError as exc:
        raise SkillHubError(f"无法执行 SkillHub CLI：{exc}", status=500) from exc
    if check and result.returncode != 0:
        raise SkillHubError(_cli_error_message(result), status=502)
    return result


def _cli_error_message(result: subprocess.CompletedProcess[str]) -> str:
    text = (result.stderr or "").strip() or (result.stdout or "").strip()
    return _truncate(text) or f"SkillHub CLI 退出码 {result.returncode}"


def _parse_json_output(raw: str) -> Any:
    """宽容解析 CLI 的 JSON 输出（进度行可能与 JSON 混排）。"""
    text = (raw or "").strip()
    if not text:
        raise SkillHubError("SkillHub CLI 没有返回内容", status=502)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    if start >= 0:
        try:
            data, _ = json.JSONDecoder().raw_decode(text[start:])
            return data
        except json.JSONDecodeError:
            pass
    raise SkillHubError("SkillHub CLI 返回了非 JSON 输出", status=502)


def _run_cli_json(args: list[str], *, cfg: SkillHubConfig, cli: Path) -> Any:
    return _parse_json_output(_run_cli(args, cfg=cfg, cli=cli).stdout or "")


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    """把 ``search`` 与 ``skill rankings`` 两种形状归一成前端条目。"""
    raw_namespace = row.get("namespace")
    namespace: dict[str, Any] = raw_namespace if isinstance(raw_namespace, dict) else {}
    # 排行榜优先用中文简介（商店是中文优先的产品），搜索接口没有该字段。
    description = str(row.get("description_zh") or row.get("description") or "").strip()
    return {
        "slug": str(row.get("slug") or ""),
        "canonical_name": str(
            namespace.get("canonicalName") or row.get("publicSlug") or row.get("slug") or ""
        ),
        "name": str(row.get("name") or row.get("slug") or ""),
        "description": description,
        "version": str(row.get("version") or ""),
        "category": str(row.get("category") or ""),
        "icon_url": str(row.get("iconUrl") or ""),
        "homepage": str(row.get("homepage") or ""),
        "handle": str(namespace.get("handle") or ""),
        "namespace": str(namespace.get("displayName") or ""),
        "downloads": _as_int(row.get("downloads")),
        "stars": _as_int(row.get("stars")),
        "verified": bool(row.get("verified") or False),
        "source": str(row.get("source") or ""),
    }


def search_skills(query: str, *, cfg: SkillHubConfig, limit: int | None = None) -> list[dict[str, Any]]:
    """按关键词检索商店技能。"""
    text = str(query or "").strip()
    if not text:
        raise SkillHubError("搜索关键词不能为空")
    cli = _require_cli(cfg)
    count = limit if isinstance(limit, int) and limit > 0 else cfg.search_limit
    count = max(1, min(count, 50))
    # 选项置于 ``--`` 之前，关键词置于其后，避免以 ``-`` 开头的查询被当作选项。
    data = _run_cli_json(
        ["search", "--json", "--search-limit", str(count), "--", text],
        cfg=cfg,
        cli=cli,
    )
    rows = data.get("results") if isinstance(data, dict) else None
    return [_normalize_row(row) for row in rows or [] if isinstance(row, dict)]


def skill_rankings(*, cfg: SkillHubConfig, ranking_type: str | None = None) -> list[dict[str, Any]]:
    """拉取商店排行榜（首页浏览数据源）。"""
    cli = _require_cli(cfg)
    kind = str(ranking_type or cfg.rankings_type or "hot").strip().lower()
    if kind not in _RANKING_TYPES:
        raise SkillHubError(f"不支持的排行榜类型：{kind}")
    data = _run_cli_json(["skill", "rankings", "--type", kind], cfg=cfg, cli=cli)
    if not isinstance(data, dict):
        return []
    if kind == "all":
        # ``--type all`` 返回的是 {"rankings": {分节: {"skills": [...]}}}，
        # 没有顶层 skills 键，直接取会静默得到空列表。
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        sections = data.get("rankings")
        for section in (sections or {}).values() if isinstance(sections, dict) else []:
            if not isinstance(section, dict):
                continue
            # 分节里装技能的键不都叫 ``skills``：付费分节用的是
            # ``featured_paid_skills``，只认 ``skills`` 会把它整节静默丢掉。
            for field, value in section.items():
                if not isinstance(value, list):
                    continue
                if field != "skills" and not field.endswith("_skills"):
                    continue
                for row in value:
                    if not isinstance(row, dict):
                        continue
                    key = str(row.get("slug") or "")
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    rows.append(_normalize_row(row))
        return rows
    section_rows = data.get("skills")
    return [_normalize_row(item) for item in section_rows or [] if isinstance(item, dict)]


def read_lockfile(skills_root: Path) -> dict[str, Any]:
    """读取技能商店锁文件里的已安装技能表（键为 ``@handle/slug``）。"""
    try:
        data = json.loads((skills_root / _LOCKFILE_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    skills = data.get("skills") if isinstance(data, dict) else None
    return skills if isinstance(skills, dict) else {}


def installed_skills(*, workspace: Path) -> list[dict[str, Any]]:
    """列出由 SkillHub 安装的技能（以锁文件为准，比 ``list`` 输出信息更全）。"""
    skills_root = skills_root_of(workspace)
    rows: list[dict[str, Any]] = []
    for canonical, raw in sorted(read_lockfile(skills_root).items()):
        entry = raw if isinstance(raw, dict) else {}
        raw_namespace = entry.get("namespace")
        namespace: dict[str, Any] = raw_namespace if isinstance(raw_namespace, dict) else {}
        rows.append(
            {
                "canonical_name": str(canonical),
                "slug": str(entry.get("internalSlug") or entry.get("publicSlug") or ""),
                "handle": str(namespace.get("handle") or ""),
                "name": str(entry.get("name") or canonical),
                "version": str(entry.get("version") or ""),
                "source": str(entry.get("source") or ""),
                "install_dir": str(entry.get("installDir") or ""),
            }
        )
    return rows


def drop_lockfile_entry(skills_root: Path, canonical_name: str) -> bool:
    """从锁文件摘除一个技能条目。

    SkillHub CLI 没有 uninstall 子命令，删除技能目录后锁文件不会自行收敛，
    会造成 ``skillhub list/upgrade`` 仍把它当已安装。此处负责补齐一致性。
    """
    if not canonical_name:
        return False
    path = skills_root / _LOCKFILE_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    skills = data.get("skills")
    if not isinstance(skills, dict) or canonical_name not in skills:
        return False
    with _WRITE_LOCK:
        skills.pop(canonical_name, None)
        try:
            atomic_write_json(path, data)
        except OSError:
            logger.warning("skill-hub: 摘除锁文件条目失败：{}", canonical_name, exc_info=True)
            return False
    return True


def install_skill(
    slug: str,
    namespace: str,
    *,
    workspace: Path,
    cfg: SkillHubConfig,
    force: bool = False,
) -> dict[str, Any]:
    """安装一个商店技能到 ``<workspace>/skills/``。"""
    cli = _require_cli(cfg)
    clean_slug = _validate_arg(slug, "slug")
    clean_namespace = _validate_arg(namespace, "namespace")
    skills_root = skills_root_of(workspace)
    skills_root.mkdir(parents=True, exist_ok=True)
    args = [
        "install",
        clean_slug,
        "--namespace",
        clean_namespace,
        "--dir",
        str(skills_root),
        "--json",
    ]
    if force:
        args.append("--force")
    with _WRITE_LOCK:
        try:
            # 安装含下载，给足超时。
            result = _run_cli(args, cfg=cfg, cli=cli, timeout=max(cfg.timeout, 180))
        except SkillHubError as exc:
            # CLI 对「目标已存在」报错退出，映射为 409 供前端提示覆盖安装。
            if "Target exists" in exc.message:
                raise SkillHubError(
                    f"技能已存在：{clean_namespace}/{clean_slug}（可用覆盖安装强制更新）",
                    status=409,
                ) from exc
            raise
    logger.info("skill-hub: 已安装 {}（{}）", f"{clean_namespace}/{clean_slug}", result.returncode)
    return {
        "installed": True,
        "slug": clean_slug,
        "namespace": clean_namespace,
        "output": _truncate((result.stdout or "").strip()),
    }


def check_updates(*, workspace: Path, cfg: SkillHubConfig) -> dict[str, Any]:
    """检查已安装技能是否有可用升级。

    注意：SkillHub 的 ``upgrade`` 依赖每个技能自带的 ``config.json``（含更新
    URL），多数社区技能没有该文件，会被 CLI 直接 ``skip``。因此 ``skipped``
    偏高属正常，不代表出错——UI 不应据此承诺「始终最新」。
    """
    cli = _require_cli(cfg)
    skills_root = skills_root_of(workspace)
    if not read_lockfile(skills_root):
        return {
            "checked": 0,
            "upgradable": 0,
            "skipped": 0,
            "failed": 0,
            "details": [],
            "summary": "尚未安装任何商店技能",
        }
    # 必须持写锁：``--check-only`` 只跳过安装，**不跳过保存**——CLI 在循环结束后
    # 会无条件用旧内容重写整个锁文件（skills_upgrade.py 的 ``save_lockfile``），
    # 且该写入是裸 write_text、非原子。与删除技能并发时，刚被摘掉的条目会被写回，
    # 产生指向已删目录的幽灵条目。
    with _WRITE_LOCK:
        result = _run_cli(
            ["upgrade", "--check-only", "--dir", str(skills_root)],
            cfg=cfg,
            cli=cli,
            check=False,
        )
    lines = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
    summary = next((line for line in reversed(lines) if line.startswith("upgrade done")), "")
    match = _UPGRADE_SUMMARY_RE.search(summary)
    counts = {
        "checked": _as_int(match.group(1)) if match else 0,
        "upgradable": _as_int(match.group(2)) if match else 0,
        "skipped": _as_int(match.group(3)) if match else 0,
        "failed": _as_int(match.group(4)) if match else 0,
    }
    return {
        **counts,
        "details": [_truncate(line, 300) for line in lines if line.startswith("[")],
        "summary": summary or _truncate("\n".join(lines)),
    }


def verify_skill(
    slug: str,
    namespace: str,
    *,
    workspace: Path,
    cfg: SkillHubConfig,
) -> dict[str, Any]:
    """校验已安装技能的签名与平台记录是否一致。

    ``--dir`` 是**全局**选项，必须置于子命令之前，否则 CLI 会去默认的
    ``./skills`` 找目录并报「未找到已安装的 skill 目录」。
    """
    cli = _require_cli(cfg)
    clean_slug = _validate_arg(slug, "slug")
    clean_namespace = _validate_arg(namespace, "namespace", required=False)
    args = ["--dir", str(skills_root_of(workspace)), "verify", clean_slug, "--json"]
    if clean_namespace:
        args = [
            "--dir",
            str(skills_root_of(workspace)),
            "verify",
            clean_slug,
            "--namespace",
            clean_namespace,
            "--json",
        ]
    # verify 用退出码 2 表示「签名校验不通过」，那是业务结果而非 CLI 故障，
    # 因此 check=False：只在拿不到可解析输出时才当作调用失败。
    result = _run_cli(args, cfg=cfg, cli=cli, check=False)
    raw = (result.stdout or "").strip()
    if not raw:
        raise SkillHubError(_cli_error_message(result), status=502)
    data = _parse_json_output(raw)
    if not isinstance(data, dict):
        return {"result": data, "ok": result.returncode == 0, "exit_code": result.returncode}
    data.setdefault("ok", result.returncode == 0)
    data["exit_code"] = result.returncode
    return data
