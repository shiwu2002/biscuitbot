"""数字员工「人才市场」注册表后端逻辑。

所属模块与项目作用
===================
本文件位于 ``biscuitbot/webui`` 目录，负责数字员工人才市场的目录拉取与安装：

- 用户提供一个 http/https 注册表 URL，该 URL 托管一份员工目录 JSON
  （形如 ``{"schema":"talent-market.v1","meta":{"updated":...},"employees":[...]}``）；
- ``_fetch_talent_catalog`` 仿 ``biscuitbot/apps/cli/service.py`` 的 ``_fetch_registry``
  拉取该 JSON，并带 TTL 缓存与失败 stale 回退；
- ``talent_catalog_payload`` 把目录规范化后返回给前端，并为每个条目标注是否已安装；
- ``install_talent_employee`` 把条目落库为正式数字员工（写入 ``workspace/employees.json``，
  即 ``EmployeeStore.create_employee``），重复 id 幂等返回已存在记录。

安全约束：
- 只允许 http/https 地址（拒绝 file://、ftp:// 等）；
- 拉取带 15s 超时与 2MB 大小上限；
- 缓存写到 ``<config dir>/talent-market/registry_cache.json``（与含 API Key 的
  ``config.json`` 同级但**独立文件**），绝不覆盖 config.json；
- 安装只写员工文件，不触碰任何配置写入逻辑。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from loguru import logger

from biscuitbot.agent.employees import EmployeeStore, EmployeeValidationError, _slugify
from biscuitbot.config.paths import get_runtime_subdir

# 注册表目录缓存 TTL（秒）：与前端「每 30 分钟自动刷新」对齐，
# 保证页面 30 分钟一次的刷新拿到的是新鲜目录数据。
_CATALOG_TTL_SECONDS = 1800
# 单次拉取超时（秒）
_FETCH_TIMEOUT_SECONDS = 15.0
# 目录 JSON 大小上限（防御性）
_MAX_CATALOG_BYTES = 2 * 1024 * 1024
# 缓存 URL 条目上限（防无限增长）
_MAX_CACHE_ENTRIES = 50
# 缓存目录名（get_runtime_subdir 用）
_TALENT_CACHE_DIR_NAME = "talent-market"
_TALENT_CACHE_FILE = "registry_cache.json"
# 单次读取分块大小
_CHUNK_BYTES = 64 * 1024

# 安装时允许落库到员工文件的字段白名单（防注入其他键）
_INSTALL_KEYS = ("id", "name", "title", "avatar", "system_prompt", "skills", "enabled")


class TalentMarketError(ValueError):
    """人才市场失败（含 HTTP 状态码）。"""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def _now() -> float:
    return time.time()


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, ensure_ascii=False)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{int(_now() * 1_000_000)}.tmp")
    try:
        tmp_path.write_text(payload, encoding="utf-8")
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _cache_path() -> Path:
    """人才市场缓存文件路径（``<config dir>/talent-market/registry_cache.json``）。"""
    return get_runtime_subdir(_TALENT_CACHE_DIR_NAME) / _TALENT_CACHE_FILE


def _validate_registry_url(raw: str) -> str:
    """校验注册表 URL：仅允许 http/https 且带主机名，否则抛 400。"""
    url = (raw or "").strip()
    if not url:
        raise TalentMarketError("注册表 URL 不能为空", status=400)
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise TalentMarketError(
            f"注册表 URL 必须为 http/https 地址：{url!r}", status=400
        )
    return url


def read_talent_market_registry_url(config_path: Path | None = None) -> str:
    """从配置文件读取人才市场注册表 URL。

    注册表 URL 是后台写死在配置文件（``gateway.talentMarketRegistryUrl`` 或
    ``gateway.talent_market_registry_url``）中的值，只能通过 CLI
    ``biscuitbot talent-market set <url>`` 修改。这里直接读原始 JSON，绝不
    触碰文件里其他键（含 API Key），也不写回任何内容。

    Returns:
        已配置的注册表 URL；未配置或文件不可读时返回空字符串。
    """
    from biscuitbot.config.loader import get_config_path

    path = config_path or get_config_path()
    if not path.exists():
        return ""
    raw = _read_json(path)
    if raw is None:
        return ""
    gateway = raw.get("gateway")
    if not isinstance(gateway, dict):
        return ""
    value = gateway.get("talent_market_registry_url") or gateway.get(
        "talentMarketRegistryUrl"
    )
    return value if isinstance(value, str) else ""


def _http_get_json(url: str) -> dict[str, Any]:
    """带大小上限的 httpx 拉取并解析 JSON。失败抛 ``TalentMarketError``(502)。"""
    try:
        with httpx.Client(timeout=_FETCH_TIMEOUT_SECONDS, follow_redirects=True) as client:
            with client.stream("GET", url) as response:
                response.raise_for_status()
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes(_CHUNK_BYTES):
                    total += len(chunk)
                    if total > _MAX_CATALOG_BYTES:
                        raise TalentMarketError(
                            f"注册表 JSON 超过 {_MAX_CATALOG_BYTES} 字节上限", status=502
                        )
                    chunks.append(chunk)
        body = b"".join(chunks)
    except TalentMarketError:
        raise
    except Exception as e:
        raise TalentMarketError(f"拉取注册表失败：{e}", status=502) from e
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise TalentMarketError(f"注册表 JSON 解析失败：{e}", status=502) from e
    if not isinstance(data, dict):
        raise TalentMarketError("注册表 JSON 必须是对象", status=502)
    return data


def _fetch_talent_catalog(url: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """拉取注册表目录，带 TTL 缓存与失败 stale 回退。"""
    cache_path = _cache_path()
    cache = _read_json(cache_path) or {}
    entries = cache.get("entries")
    if not isinstance(entries, dict):
        entries = {}
    entry = entries.get(url)
    if (
        not force_refresh
        and isinstance(entry, dict)
        and _now() - float(entry.get("_cached_at", 0)) < _CATALOG_TTL_SECONDS
        and isinstance(entry.get("data"), dict)
    ):
        return entry["data"]

    try:
        data = _http_get_json(url)
    except TalentMarketError:
        # 拉取失败但存在旧缓存 → stale 回退
        if isinstance(entry, dict) and isinstance(entry.get("data"), dict):
            logger.warning("talent-market 拉取失败，使用旧缓存：{}", url)
            return entry["data"]
        raise

    entries[url] = {"_cached_at": _now(), "data": data}
    # 剪裁超龄/超量条目：仅保留最近 _MAX_CACHE_ENTRIES 条
    if len(entries) > _MAX_CACHE_ENTRIES:
        ordered = sorted(
            entries.items(), key=lambda kv: float(kv[1].get("_cached_at", 0)), reverse=True
        )
        entries = dict(ordered[:_MAX_CACHE_ENTRIES])
    try:
        _write_json(cache_path, {"_schema": 1, "entries": entries})
    except OSError:
        logger.warning("talent-market 无法写入缓存：{}", cache_path)
    return data


def _normalize_talent_entry(raw: Any) -> dict[str, Any] | None:
    """把注册表条目规范化为前端展示结构；无效条目返回 None（跳过）。"""
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    name = name.strip()

    employee_id = raw.get("id")
    if not isinstance(employee_id, str) or not employee_id.strip():
        employee_id = _slugify(name) or "employee"

    def _str_value(key: str) -> str:
        value = raw.get(key)
        return value if isinstance(value, str) else ""

    skills = _flatten_skill_names(raw.get("skills"))

    return {
        "id": employee_id,
        "name": name,
        "title": _str_value("title"),
        "avatar": _str_value("avatar"),
        "description": _str_value("description"),
        "system_prompt": _str_value("system_prompt"),
        "skills": skills,
        "category": _str_value("category"),
    }


def talent_catalog_payload(
    url: str,
    store: EmployeeStore | None,
    *,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """拉取注册表并返回前端目录 payload（含 installed 标注）。"""
    validated = _validate_registry_url(url)
    registry = _fetch_talent_catalog(validated, force_refresh=force_refresh)

    installed_ids: set[str] = set()
    if store is not None:
        try:
            installed_ids = {e.get("id") for e in store.list_employees() if e.get("id")}
        except Exception:
            logger.exception("failed to list employees for talent catalog")
            installed_ids = set()

    rows: list[dict[str, Any]] = []
    installed_count = 0
    raw_rows = registry.get("employees")
    if not isinstance(raw_rows, list):
        # 兼容 "talent" 键名
        raw_rows = registry.get("talent")
    if isinstance(raw_rows, list):
        for raw in raw_rows:
            row = _normalize_talent_entry(raw)
            if row is None:
                continue
            installed = row["id"] in installed_ids
            if installed:
                installed_count += 1
            row["installed"] = installed
            rows.append(row)

    meta = registry.get("meta")
    updated_at = meta.get("updated") if isinstance(meta, dict) else None
    if not isinstance(updated_at, str):
        updated_at = None

    return {
        "source_url": validated,
        "catalog_updated_at": updated_at,
        "employees": rows,
        "installed_count": installed_count,
    }


def _flatten_skill_names(skills_raw: Any) -> list[str]:
    """把 ``skills`` 字段展平为技能名列表（兼容字符串与 bundle 对象两种形式）。"""
    if not isinstance(skills_raw, list):
        return []
    names: list[str] = []
    for item in skills_raw:
        if isinstance(item, str):
            name = item.strip()
        elif isinstance(item, dict):
            raw_name = item.get("name")
            name = raw_name.strip() if isinstance(raw_name, str) else ""
        else:
            continue
        if name:
            names.append(name)
    return names


def _parse_skill_spec(
    skills_raw: Any,
) -> tuple[list[str], dict[str, dict[str, str]]]:
    """解析 ``skills`` 字段，返回 ``(技能名列表, {技能名: {相对路径: 内容}})``。

    条目可以是：
    - 字符串：仅技能名（引用已存在的内置/工作区技能）；
    - 对象 ``{"name": ..., "files": {相对路径: 文本内容}}``：随员工下载的自带技能，
      ``files`` 中必须含 ``SKILL.md``，其余文件按相对路径落盘到
      ``workspace/skills/<name>/``。
    """
    names: list[str] = []
    bundled: dict[str, dict[str, str]] = {}
    if not isinstance(skills_raw, list):
        return names, bundled
    for item in skills_raw:
        if isinstance(item, str):
            name = item.strip()
            if name:
                names.append(name)
        elif isinstance(item, dict):
            raw_name = item.get("name")
            name = raw_name.strip() if isinstance(raw_name, str) else ""
            files = item.get("files")
            if name and isinstance(files, dict):
                text_files = {
                    str(k): str(v)
                    for k, v in files.items()
                    if isinstance(k, str) and isinstance(v, str)
                }
                if text_files:
                    bundled[name] = text_files
                    names.append(name)
    return names, bundled


def _find_raw_entry(registry: dict[str, Any], employee_id: str) -> dict[str, Any] | None:
    """在注册表原始 JSON 中按 id 定位条目；未找到返回 None。"""
    raw_rows = registry.get("employees")
    if not isinstance(raw_rows, list):
        raw_rows = registry.get("talent")
    if not isinstance(raw_rows, list):
        return None
    for raw in raw_rows:
        if not isinstance(raw, dict):
            continue
        rid = raw.get("id")
        if not isinstance(rid, str) or not rid.strip():
            rid = _slugify(str(raw.get("name") or "")) or "employee"
        if rid == employee_id:
            return raw
    return None


def _install_bundled_skills(
    store: EmployeeStore,
    employee_id: str,
    bundled: dict[str, dict[str, str]],
) -> list[str]:
    """把自带技能文件写入工作区并记录归属，返回落盘的技能名列表。"""
    from biscuitbot.agent.skill_owners import SkillOwnershipStore, write_skill_files

    owners = SkillOwnershipStore(store.workspace)
    installed: list[str] = []
    for name, files in bundled.items():
        try:
            write_skill_files(store.workspace, name, files)
        except Exception as e:  # noqa: BLE001 - 单个技能失败不阻断整个安装
            logger.warning("talent-market 技能 {} 落盘失败，跳过：{}", name, e)
            continue
        owners.set_owner(name, employee_id)
        installed.append(name)
    return installed


# 市场头像下载上限（防御性，内置头像约 0.5MB）
_AVATAR_MAX_BYTES = 4 * 1024 * 1024
# 按 content-type 识别的图片扩展名白名单
_AVATAR_CONTENT_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/avif": ".avif",
}


def _avatar_ext_from_magic(data: bytes) -> str | None:
    """按文件魔数识别常见图片扩展名；无法识别返回 None。"""
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    return None


def _install_market_avatar(employee_id: str, avatar: str) -> str:
    """下载注册表条目的图片头像并落盘到实例头像目录，返回本地文件名。

    - emoji / 已是本地文件名（头像制）→ 原样返回，不下载；
    - http/https 图片 URL → 下载到 ``<config dir>/avatars/<id>.<ext>``（原子写），
      返回裸文件名，供 WebUI ``/api/avatars`` 渲染；
    - 下载失败 / 非识别图片 → 返回原值，前端按文本兜底（不阻断安装）。

    只允许 http/https，带大小上限与魔数/Content-Type 校验，文件名取自
    员工 slug + 扩展名白名单（无路径穿越）。
    """
    if not isinstance(avatar, str) or not avatar.strip():
        return avatar or ""
    name = avatar.strip()
    parsed = urlparse(name)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return avatar  # emoji / 本地文件名 / 其它文本
    try:
        ctype = ""
        chunks: list[bytes] = []
        total = 0
        with httpx.Client(timeout=_FETCH_TIMEOUT_SECONDS, follow_redirects=True) as client:
            with client.stream("GET", name) as response:
                response.raise_for_status()
                ctype = response.headers.get("content-type", "")
                for chunk in response.iter_bytes(_CHUNK_BYTES):
                    total += len(chunk)
                    if total > _AVATAR_MAX_BYTES:
                        logger.warning(
                            "talent-market 头像超过大小上限，跳过：{}", name
                        )
                        return avatar
                    chunks.append(chunk)
        data = b"".join(chunks)
        ext = _avatar_ext_from_magic(data)
        if ext is None:
            ext = _AVATAR_CONTENT_TYPES.get(ctype.split(";")[0].strip().lower())
        if ext is None:
            logger.warning("talent-market 头像非识别图片，跳过：{}", name)
            return avatar
        target_dir = get_runtime_subdir("avatars")
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{employee_id}{ext}"
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(target)
        logger.debug("talent-market 头像已下载：{} -> {}", name, target)
        return target.name
    except Exception as e:  # noqa: BLE001 - 单个头像失败不阻断整个安装
        logger.warning("talent-market 头像下载失败，跳过：{}：{}", name, e)
        return avatar


def install_talent_employee(
    values: dict[str, Any],
    store: EmployeeStore,
    *,
    source_url: str | None = None,
) -> dict[str, Any]:
    """把注册表条目落库为数字员工；重复 id 幂等返回已存在记录。

    若 ``source_url`` 提供且条目携带自带技能（bundle 对象），会先把技能文件写入
    ``workspace/skills/<name>/`` 并记录归属（随员工一起下载），再落库员工。

    返回: ``{"employee": {...}, "already_existed": bool, "installed_skills": [...]}``。
    """
    data = {key: values.get(key) for key in _INSTALL_KEYS if key in values}
    employee_id = str(data.get("id") or "").strip()

    # 以注册表为权威来源解析自带技能（含文件内容）与头像，前端只回传技能名。
    bundled: dict[str, dict[str, str]] = {}
    if source_url and employee_id:
        try:
            registry = _fetch_talent_catalog(source_url)
            raw_entry = _find_raw_entry(registry, employee_id)
            if raw_entry is not None:
                names, bundled = _parse_skill_spec(raw_entry.get("skills"))
                data["skills"] = names
                raw_avatar = raw_entry.get("avatar")
                if isinstance(raw_avatar, str) and raw_avatar.strip():
                    data["avatar"] = raw_avatar
        except TalentMarketError:
            logger.warning(
                "talent-market 安装时无法拉取注册表，跳过技能/头像下载：{}", source_url
            )

    installed_skills = _install_bundled_skills(store, employee_id, bundled) if employee_id else []

    # 头像制：目录条目的图片头像下载到本地再落库（emoji / 本地文件名原样保留）。
    if employee_id and isinstance(data.get("avatar"), str) and data["avatar"].strip():
        data["avatar"] = _install_market_avatar(employee_id, data["avatar"])

    try:
        employee = store.create_employee(data)
    except EmployeeValidationError as e:
        if e.status == 409:
            # 幂等：id 已存在 → 返回现存员工，不当作错误
            existing = store.get_employee(employee_id) if employee_id else None
            if existing is not None:
                logger.info(
                    "talent-market 员工已存在，跳过：{}（来源 {}）",
                    employee_id,
                    source_url or "unknown",
                )
                return {
                    "employee": existing,
                    "already_existed": True,
                    "installed_skills": installed_skills,
                }
        raise
    logger.debug(
        "talent-market 安装员工：{}（来源 {}）",
        employee.get("id"),
        source_url or "unknown",
    )
    return {
        "employee": employee,
        "already_existed": False,
        "installed_skills": installed_skills,
    }
