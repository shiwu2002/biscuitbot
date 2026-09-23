"""SkillHub 技能商店后端（浏览 / 搜索 / 安装 / 升级检查 / 签名校验）。

所属模块与项目作用
===================
本文件位于 ``xianaibot/webui`` 目录，负责把外部技能商店 SkillHub 接入 WebUI：

- **浏览/搜索/安装默认走商店的公开 HTTP 接口**（``api.skillhub.cn``，与官方 CLI
  用的是同一组端点、免认证），因此没有装 CLI 也能看到全部商店技能；
- 本机装了 CLI（``~/.skillhub/skills_store_cli.py``）时，安装/升级/校验优先交给
  CLI，复用官方的签名校验与 ``.skills_store_lock.json``；CLI 缺失时安装退化为
  我们自己下载 zip、按同样的锁文件结构登记；
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
- HTTP 直连的商店主机是**常量**（不取自用户输入），因此无需 SSRF 白名单；
- 下载的 zip 解压前逐条校验成员路径（zip-slip）并设体积/条目上限；
- CLI 缺失时降级为 HTTP 直连，而不是抛错。
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from loguru import logger

from xianaibot.config.schema import SkillHubConfig
from xianaibot.utils.helpers import atomic_write_json

# SkillHub CLI 默认安装位置（官方 install.sh 的 INSTALL_BASE / CLI_TARGET）
_DEFAULT_CLI_DIR_NAME = ".skillhub"
_CLI_SCRIPT_NAME = "skills_store_cli.py"
_VERSION_FILE_NAME = "version.json"
# 技能商店在技能根下维护的锁文件（记录已安装技能的 slug / version / installDir）
_LOCKFILE_NAME = ".skills_store_lock.json"

# 商店公开 HTTP 接口（与官方 CLI 的 metadata.json 指向同一组端点，免认证）。
# 主机是常量、不取自用户输入 —— 想换自建镜像改这里即可。
_SKILLHUB_HTTP_BASE = "https://api.skillhub.cn"
_HTTP_SEARCH_PATH = "/api/v1/search"
_HTTP_SHOWCASE_PATH = "/api/v1/showcase"
_HTTP_DOWNLOAD_PATH = "/api/v1/download"
_HTTP_TIMEOUT = 15.0
# 安装要下整包，与 CLI 侧一致给足时间。
_HTTP_DOWNLOAD_TIMEOUT = 180.0
# 解压护栏：商店技能实测 < 1MB，留足余量同时挡住 zip 炸弹。
_MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
_MAX_ARCHIVE_ENTRIES = 5_000

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
    """返回商店可用性快照。

    CLI 不再是「能否浏览」的前提：没有 CLI 时浏览、搜索、安装都走商店 HTTP
    直连，因此只要没被配置禁用就是可用的。``mode`` 表示安装/校验将走哪条路径
    （``cli`` = 官方 CLI 含签名校验，``http`` = 商店直连下载）。
    """
    cli = resolve_cli_path(cfg)
    root = skills_root_of(workspace)
    return {
        "available": bool(cfg.enable),
        "enabled": cfg.enable,
        "mode": "cli" if cli is not None else "http",
        "cli_available": cli is not None,
        "cli_path": str(cli) if cli is not None else "",
        "version": _read_cli_version(cli.parent / _VERSION_FILE_NAME) if cli is not None else "",
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
    """把商店各接口的行形状（``search`` / ``showcase`` / CLI 输出）归一成前端条目。

    三处的字段名并不统一：``showcase`` 用 ``iconUrl``，``/api/v1/search`` 用
    ``icon_url`` 且把技能名放在 ``displayName`` 上，``namespace`` 也可能是纯字符串。
    """
    raw_namespace = row.get("namespace")
    namespace: dict[str, Any] = raw_namespace if isinstance(raw_namespace, dict) else {}
    handle = str(namespace.get("handle") or "")
    if not handle and isinstance(raw_namespace, str):
        handle = raw_namespace
    # 排行榜优先用中文简介（商店是中文优先的产品），搜索接口没有该字段。
    description = str(row.get("description_zh") or row.get("description") or "").strip()
    return {
        "slug": str(row.get("slug") or ""),
        "canonical_name": str(
            namespace.get("canonicalName") or row.get("publicSlug") or row.get("slug") or ""
        ),
        "name": str(row.get("name") or row.get("displayName") or row.get("slug") or ""),
        "description": description,
        "version": str(row.get("version") or ""),
        "category": str(row.get("category") or ""),
        "icon_url": str(row.get("iconUrl") or row.get("icon_url") or ""),
        "homepage": str(row.get("homepage") or ""),
        "handle": handle,
        "namespace": str(namespace.get("displayName") or row.get("owner_name") or ""),
        "downloads": _as_int(row.get("downloads")),
        "stars": _as_int(row.get("stars")),
        "verified": bool(row.get("verified") or False),
        "source": str(row.get("source") or ""),
    }


# --------------------------------------------------------------------------- #
# 商店公开 HTTP 接口（CLI 缺失时的直连通道；主机为常量，无需 SSRF 校验）
# --------------------------------------------------------------------------- #


def _http_json(path: str, params: dict[str, Any], *, timeout: float = _HTTP_TIMEOUT) -> Any:
    """调用商店公开接口并解析 JSON，把网络/协议错误映射为 SkillHubError。"""
    url = f"{_SKILLHUB_HTTP_BASE}{path}"
    try:
        response = httpx.get(
            url,
            params={key: value for key, value in params.items() if value not in (None, "")},
            timeout=timeout,
            follow_redirects=True,
            headers={"Accept": "application/json"},
        )
    except httpx.TimeoutException as exc:
        raise SkillHubError(f"商店接口超时（{path}）", status=504) from exc
    except httpx.HTTPError as exc:
        raise SkillHubError(f"无法访问商店接口（{path}）：{exc}", status=502) from exc
    if response.status_code >= 400:
        raise SkillHubError(f"商店接口返回 HTTP {response.status_code}（{path}）", status=502)
    try:
        return response.json()
    except ValueError as exc:
        raise SkillHubError("商店接口返回了非 JSON 输出", status=502) from exc


def _showcase_rows(kind: str) -> list[dict[str, Any]]:
    data = _http_json(f"{_HTTP_SHOWCASE_PATH}/{kind}", {})
    rows = data.get("skills") if isinstance(data, dict) else None
    return [_normalize_row(row) for row in rows or [] if isinstance(row, dict)]


def _http_rankings(kind: str) -> list[dict[str, Any]]:
    """按榜单类型拉取商店技能；``all`` 合并各榜并去重（对齐 CLI 的 ``--type all``）。"""
    if kind != "all":
        return _showcase_rows(kind)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    failures: list[SkillHubError] = []
    for one in sorted(_RANKING_TYPES - {"all"}):
        try:
            section = _showcase_rows(one)
        except SkillHubError as exc:
            # 单个榜挂了不该拖垮整页；全都挂了再把错误抛给调用方。
            failures.append(exc)
            continue
        for row in section:
            key = row["canonical_name"] or row["slug"]
            if key and key not in seen:
                seen.add(key)
                rows.append(row)
    if not rows and failures:
        raise failures[0]
    return rows


def _http_search(query: str, *, limit: int) -> list[dict[str, Any]]:
    data = _http_json(_HTTP_SEARCH_PATH, {"q": query, "limit": limit})
    rows = data.get("results") if isinstance(data, dict) else None
    return [_normalize_row(row) for row in rows or [] if isinstance(row, dict)]


def _cli_search(text: str, *, cfg: SkillHubConfig, count: int) -> list[dict[str, Any]]:
    cli = _require_cli(cfg)
    # 选项置于 ``--`` 之前，关键词置于其后，避免以 ``-`` 开头的查询被当作选项。
    data = _run_cli_json(
        ["search", "--json", "--search-limit", str(count), "--", text],
        cfg=cfg,
        cli=cli,
    )
    rows = data.get("results") if isinstance(data, dict) else None
    return [_normalize_row(row) for row in rows or [] if isinstance(row, dict)]


def search_skills(query: str, *, cfg: SkillHubConfig, limit: int | None = None) -> list[dict[str, Any]]:
    """按关键词检索商店技能：HTTP 直连优先，失败且有 CLI 时回落 CLI。"""
    text = str(query or "").strip()
    if not text:
        raise SkillHubError("搜索关键词不能为空")
    if not cfg.enable:
        raise SkillHubError("SkillHub 已在配置中禁用", status=403)
    count = limit if isinstance(limit, int) and limit > 0 else cfg.search_limit
    count = max(1, min(count, 50))
    try:
        return _http_search(text, limit=count)
    except SkillHubError:
        if not cfg.enable or resolve_cli_path(cfg) is None:
            raise
        logger.debug("skill-hub: 商店直连搜索失败，回落到 CLI")
    return _cli_search(text, cfg=cfg, count=count)


def _cli_rankings(kind: str, *, cfg: SkillHubConfig) -> list[dict[str, Any]]:
    cli = _require_cli(cfg)
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


def skill_rankings(*, cfg: SkillHubConfig, ranking_type: str | None = None) -> list[dict[str, Any]]:
    """拉取商店排行榜（首页浏览数据源）：HTTP 直连优先，失败且有 CLI 时回落 CLI。"""
    kind = str(ranking_type or cfg.rankings_type or "hot").strip().lower()
    if kind not in _RANKING_TYPES:
        raise SkillHubError(f"不支持的排行榜类型：{kind}")
    if not cfg.enable:
        raise SkillHubError("SkillHub 已在配置中禁用", status=403)
    try:
        return _http_rankings(kind)
    except SkillHubError:
        if not cfg.enable or resolve_cli_path(cfg) is None:
            raise
        logger.debug("skill-hub: 商店直连榜单失败，回落到 CLI")
    return _cli_rankings(kind, cfg=cfg)


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


def _cli_install(
    clean_slug: str,
    clean_namespace: str,
    *,
    skills_root: Path,
    cfg: SkillHubConfig,
    force: bool,
) -> dict[str, Any]:
    cli = _require_cli(cfg)
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
        "mode": "cli",
        "output": _truncate((result.stdout or "").strip()),
    }


def _download_archive(slug: str, namespace: str) -> bytes:
    """从商店下载技能 zip（与官方 CLI 的 primary download 同一端点）。"""
    url = f"{_SKILLHUB_HTTP_BASE}{_HTTP_DOWNLOAD_PATH}"
    try:
        response = httpx.get(
            url,
            params={"slug": slug, "namespace": namespace},
            timeout=_HTTP_DOWNLOAD_TIMEOUT,
            follow_redirects=True,
        )
    except httpx.TimeoutException as exc:
        raise SkillHubError(f"下载技能超时（{slug}）", status=504) from exc
    except httpx.HTTPError as exc:
        raise SkillHubError(f"下载技能失败（{slug}）：{exc}", status=502) from exc
    if response.status_code >= 400:
        raise SkillHubError(
            f"商店下载返回 HTTP {response.status_code}（{slug}）", status=502
        )
    data = response.content
    if not data:
        raise SkillHubError(f"商店返回了空压缩包（{slug}）", status=502)
    if len(data) > _MAX_ARCHIVE_BYTES:
        raise SkillHubError(f"技能压缩包过大（{len(data)} 字节），已拒绝解压", status=502)
    if not data.startswith(b"PK"):
        # slug 不存在时该端点会回 JSON 错误体而不是 zip。
        raise SkillHubError(
            f"商店没有该技能的下载包：{_truncate(data.decode('utf-8', 'replace'), 300)}",
            status=404,
        )
    return data


def _extract_archive(data: bytes, target: Path) -> int:
    """把技能 zip 解到 ``target``，返回写入的文件数。

    逐条校验成员路径，拒绝绝对路径与 ``..`` 穿越（zip-slip）；解压前后用
    ``_MAX_ARCHIVE_*`` 兜住 zip 炸弹。
    """
    written = 0
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = archive.infolist()
        if len(members) > _MAX_ARCHIVE_ENTRIES:
            raise SkillHubError(f"技能压缩包条目过多（{len(members)}），已拒绝解压")
        total = sum(member.file_size for member in members)
        if total > _MAX_ARCHIVE_BYTES:
            raise SkillHubError(f"技能解压后体积过大（{total} 字节），已拒绝解压")
        for member in members:
            if member.is_dir():
                continue
            rel = member.filename.replace("\\", "/").lstrip("/")
            parts = [part for part in rel.split("/") if part not in ("", ".")]
            if not parts or ".." in parts or ":" in parts[0]:
                logger.warning("skill-hub: 跳过可疑压缩包成员：{}", member.filename)
                continue
            destination = target.joinpath(*parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, destination.open("wb") as sink:
                shutil.copyfileobj(source, sink)
            written += 1
    return written


def _http_metadata(slug: str, namespace: str) -> dict[str, Any]:
    """尽力补齐登记用的 name / version / source；查不到不影响安装。"""
    try:
        data = _http_json(_HTTP_SEARCH_PATH, {"q": slug, "limit": 10})
    except SkillHubError:
        return {}
    rows = data.get("results") if isinstance(data, dict) else None
    for row in rows or []:
        if not isinstance(row, dict) or str(row.get("slug") or "") != slug:
            continue
        found = _normalize_row(row)
        if namespace and found["handle"] and found["handle"] != namespace:
            continue
        return found
    return {}


def _register_installed_entry(
    skills_root: Path,
    install_key: str,
    slug: str,
    namespace: str,
    target: Path,
    meta: dict[str, Any],
) -> None:
    """按 CLI 的锁文件结构登记，使「已安装」列表与删除路径都认得它。"""
    path = skills_root / _LOCKFILE_NAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    skills = raw.get("skills")
    if not isinstance(skills, dict):
        skills = {}
        raw["skills"] = skills
    raw.setdefault("version", 1)
    skills[install_key] = {
        "name": str(meta.get("name") or slug),
        "zip_url": f"{_SKILLHUB_HTTP_BASE}{_HTTP_DOWNLOAD_PATH}?{urlencode({'slug': slug, 'namespace': namespace})}",
        "source": str(meta.get("source") or "community"),
        "version": str(meta.get("version") or ""),
        "namespace": {
            "canonicalName": install_key,
            "displayName": str(meta.get("namespace") or namespace),
            "handle": namespace,
            "publicSlug": slug,
        },
        "internalSlug": slug,
        "publicSlug": slug,
        "installDir": str(target),
    }
    atomic_write_json(path, raw)


def _http_install(
    slug: str,
    namespace: str,
    *,
    workspace: Path,
    force: bool,
) -> dict[str, Any]:
    """没装 CLI 时从商店直连下载 zip 并安装（无签名校验，故 source 标为社区源）。"""
    clean_slug = _validate_arg(slug, "slug")
    clean_namespace = _validate_arg(namespace, "namespace", required=False)
    skills_root = skills_root_of(workspace)
    skills_root.mkdir(parents=True, exist_ok=True)
    install_key = f"@{clean_namespace}/{clean_slug}" if clean_namespace else clean_slug
    target = skills_root / install_key
    if target.exists() and not force:
        raise SkillHubError(
            f"技能已存在：{install_key}（可用覆盖安装强制更新）", status=409
        )
    payload = _download_archive(clean_slug, clean_namespace)
    # 先解到暂存目录再整体落位，避免解压中途失败留下半个技能目录。
    staging = skills_root / f".{clean_slug}.{os.getpid()}.installing"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    try:
        written = _extract_archive(payload, staging)
        if not written:
            raise SkillHubError("商店压缩包里没有文件", status=502)
        # 包没问题了再查元信息，免得压缩包坏掉时白跑一次请求。
        meta = _http_metadata(clean_slug, clean_namespace)
        with _WRITE_LOCK:
            if target.exists():
                shutil.rmtree(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(staging), str(target))
            _register_installed_entry(
                skills_root, install_key, clean_slug, clean_namespace, target, meta
            )
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    logger.info("skill-hub: 已从商店直连安装 {}（{} 个文件）", install_key, written)
    return {
        "installed": True,
        "slug": clean_slug,
        "namespace": clean_namespace,
        "mode": "http",
        "files": written,
        "output": f"已从商店下载并安装 {install_key}（{written} 个文件）",
    }


def install_skill(
    slug: str,
    namespace: str,
    *,
    workspace: Path,
    cfg: SkillHubConfig,
    force: bool = False,
) -> dict[str, Any]:
    """安装一个商店技能到 ``<workspace>/skills/``。

    装了 CLI 就交给 CLI（官方路径，含签名校验与锁文件写入）；没装则走商店
    HTTP 直连下载，并按同样的锁文件结构登记。
    """
    if not cfg.enable:
        raise SkillHubError("SkillHub 已在配置中禁用", status=403)
    clean_slug = _validate_arg(slug, "slug")
    clean_namespace = _validate_arg(namespace, "namespace", required=False)
    skills_root = skills_root_of(workspace)
    if resolve_cli_path(cfg) is not None:
        return _cli_install(
            clean_slug, clean_namespace, skills_root=skills_root, cfg=cfg, force=force
        )
    return _http_install(clean_slug, clean_namespace, workspace=workspace, force=force)


def _empty_updates(summary: str) -> dict[str, Any]:
    """升级检查的空结果（无可用计数、无错误）。"""
    return {
        "checked": 0,
        "upgradable": 0,
        "skipped": 0,
        "failed": 0,
        "details": [],
        "summary": summary,
    }


def check_updates(*, workspace: Path, cfg: SkillHubConfig) -> dict[str, Any]:
    """检查已安装技能是否有可用升级。

    注意：SkillHub 的 ``upgrade`` 依赖每个技能自带的 ``config.json``（含更新
    URL），多数社区技能没有该文件，会被 CLI 直接 ``skip``。因此 ``skipped``
    偏高属正常，不代表出错——UI 不应据此承诺「始终最新」。
    """
    skills_root = skills_root_of(workspace)
    if not read_lockfile(skills_root):
        return _empty_updates("尚未安装任何商店技能")
    # 升级检查只有 CLI 能做（要读每个技能的 config.json 更新清单）。缺 CLI 时
    # 返回空结果而不是报错：商店浏览与安装不受影响。
    if resolve_cli_path(cfg) is None:
        return _empty_updates(
            "未检测到 SkillHub CLI，无法检查升级（技能仍可通过商店直连安装或覆盖重装）"
        )
    cli = _require_cli(cfg)
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
