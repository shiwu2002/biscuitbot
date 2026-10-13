"""会话资产 helpers：列出/预览/删除**本会话产出**的图片/视频/音频/文档。

「资产」= 智能体在会话工作区里做出来的图片/视频/音频/文档，与用户主动上传的
知识库文档分开。落盘口径见 ``xianaibot/utils/artifacts.py::_artifact_root``：

- 生成图片 ``<workspace>/generated/YYYY-MM-DD/img_<12hex>.png`` + sidecar ``.json``
- 生成视频 ``<workspace>/generated_video/YYYY-MM-DD/vid_<12hex>.mp4`` + sidecar ``.json``
- TTS 音频 ``<workspace>/generated/tts/<date>/tts_<uuid>.mp3``
- 智能体用文件工具写入的任意报告/表格（如 ``<workspace>/报告.html``）

历史沿革：本模块原先扫 ``get_media_dir()``（media 根）下的 ``generated`` /
``generated_video`` / ``api``，外加 media 根外的 TTS。``0ee7f03``（2026-09-03
「add workspace management」）把产出落盘从 media 根改到工作区，扫描根却没有同步
跟上 —— 生成的图片/视频因此一直不出现在列表里。现在以会话工作区为扫描根；渠道入站
媒体（``media/channels``）与外部 API 上传（``media/api``）不再是资产来源。

工作区文件不在 media 根内，无法被 ``/api/media`` 服务，改由会话级工作区签名路由
``/api/sessions/<key>/workspace-media/<sig>/<payload>`` 直接签原文件：不复制、
不 staging（staging 目录没有清理机制，复制视频会白占磁盘）。

**两种口径，由 ``scan_workspace`` 选**：

1. ``False``（**本会话**，默认）—— 只列会话台账：本会话**交付**过或**写**过的文件。
   文件系统里没有「会话」这个维度（产出落盘在 ``<workspace>/generated/…``，与谁在
   聊无关），所以按会话隔离只能靠记录推导，见
   :mod:`xianaibot.webui.session_deliveries`。
2. ``True``（**整个工作区**）—— 白名单扩展名的「疑似产出」全扫，有遍历/条数上限
   （免得一个项目目录把列表冲爆）。会话没单独设工作区时都指向默认工作区，各会话看到
   的列表因此相同 —— 这是「工作区视图」，不是隔离口径。

台账条目优先于扫描条目，且**交付过的文件不受白名单与上限约束**（工作区外的文件、
``.zip``/``.json`` 这类非产出扩展名，只要交付过就必须看得见），交付即显示是硬保证。
台账成员（交付过或写过）也据此获得删除/预览授权 —— 授权依据是记录，不是路径包含关系。
"""

from __future__ import annotations

import binascii
import heapq
import json
import os
import re
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from xianaibot.webui.http_utils import query_first
from xianaibot.webui.media_api import b64url_decode, b64url_encode
from xianaibot.webui.settings_api import WebUISettingsError

QueryParams = dict[str, list[str]]

# 资产 id = base64url(工作区相对路径)。b64url 字符集不含分隔符与转义歧义字符，
# 中文/空格/`#` 文件名都不会在 URL 编码上出岔子。前端把 id 当不透明字符串用。
_ASSET_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,512}$")

# 工作区**外**的交付文件（``message`` 工具可以交付工作区外的文件）在同一个 id 空间里
# 用 ``@:`` 前缀区分：解出来是「绝对路径」而不是「工作区相对路径」。
_EXTERNAL_ID_TAG = "@:"

_IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".bmp"})
_VIDEO_EXTS = frozenset({".mp4", ".webm", ".mov", ".mkv"})
_AUDIO_EXTS = frozenset({".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac"})
# 含 .html/.htm —— 智能体最常产出的报告就是 HTML（见 FilePreviewPanel 的渲染分支）。
# 不收 .json：生成物的 sidecar 会刷屏。
_DOCUMENT_EXTS = frozenset({
    ".docx", ".pdf", ".md", ".txt", ".xlsx", ".csv", ".pptx", ".html", ".htm",
})

# 顺序敏感：先匹配图片/视频/音频，最后 document。
_KIND_EXTS: tuple[tuple[str, frozenset[str]], ...] = (
    ("image", _IMAGE_EXTS),
    ("video", _VIDEO_EXTS),
    ("audio", _AUDIO_EXTS),
    ("document", _DOCUMENT_EXTS),
)
_WORKSPACE_ASSET_EXTS = frozenset(
    _IMAGE_EXTS | _VIDEO_EXTS | _AUDIO_EXTS | _DOCUMENT_EXTS
)

# 目录名剪枝：不做产出、但可能有几十万文件的依赖/构建目录。首字符为 "." 的项
# 在遍历里已被整体跳过（含 .git/.venv/.cache）。
_PRUNE_DIR_NAMES = frozenset({
    "node_modules", "__pycache__", "venv", "dist", "build", ".next", ".nuxt",
    "target", "coverage", "site-packages", "bower_components", "vendor",
})

# 遍历与返回条数上限：工作区可能指向一个大型代码仓库，必须给遍历兜底，
# 否则一次列资产就会扫爆。触顶时 payload 回传 truncated，让 UI 能提示而不是静默少列。
#
# 两个上限分工不同，别混：
# - ``_WORKSPACE_SCAN_MAX_CANDIDATES`` 只限制**考察**多少个候选文件（限定耗时），不限制内存。
# - ``_MAX_ASSET_ITEMS`` 限制**保留**多少条，保留集在遍历中用 mtime 有界挑选，
#   所以大工作区丢的是更旧的文件，而不是「遍历顺序上更早遇到」的文件 —— 后者会让
#   刚生成的产出随机消失。
_WORKSPACE_SCAN_MAX_CANDIDATES = 200_000
_MAX_ASSET_ITEMS = 2_000

SignedMediaPath = Callable[[Path], str | None]


def _kind_for_suffix(suffix: str) -> str:
    """按扩展名推断资产 kind；调用方已保证后缀在白名单内。"""
    suffix = suffix.lower()
    for kind, exts in _KIND_EXTS:
        if suffix in exts:
            return kind
    return "document"


def _iso_from_mtime(mtime: float) -> str:
    try:
        return datetime.fromtimestamp(mtime).astimezone().isoformat()
    except (OSError, OverflowError, ValueError):
        return ""


def _sort_instant(value: str) -> datetime | None:
    """把 ``created_at`` 解析成**带时区**的时刻；解析不了返回 None。

    排序不能直接比字符串：sidecar 里记的是 ``datetime.now().astimezone()`` 的
    isoformat，混了不同 UTC 偏移（跨时区、夏令时、老数据 +08:00 与新数据 +09:00）
    时 ``"…T09:00+09:00" < "…T08:30+08:00"`` 会得出错误顺序。

    无偏移的（朴素）时间按**本机时区**理解 —— 与写入端 ``.astimezone()`` 一致。
    """
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _load_sidecar(sidecar: Path) -> dict[str, Any]:
    """读取 sidecar JSON；损坏/缺失时返回空 dict。"""
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _asset_item(
    asset_id: str,
    name: str,
    kind: str,
    size: int,
    created_at: str,
    caption: str,
    media_url: str,
    *,
    delivered: bool = False,
    path: str = "",
) -> dict[str, Any]:
    return {
        "id": asset_id,
        "name": name,
        "kind": kind,
        "size": size,
        "created_at": created_at,
        "caption": caption,
        "media_url": media_url,
        # 会话资产只有一个来源（当前会话工作区），不再有渠道维度；保留字段让前端
        # 的类型与筛选逻辑无需改动（值恒为空串，渠道筛选条会自动隐藏）。
        "channel": "",
        # 智能体交付给用户的文件（会话记录留痕）；前端据此给一个「交付」标记。
        "delivered": delivered,
        # 工作区**相对**路径（posix 写法，工作区外的交付件为空串）：前端的文件预览
        # 面板走 ``file-preview``/``file-reveal`` 路由时需要它（相对路径是那两个路由
        # 的入参），没有它就只能退化成「按签名 URL 预览、无法定位原文件」。
        "path": path,
    }


def _encode_asset_id(rel: Path) -> str:
    """工作区相对路径 → 资产 id。"""
    return b64url_encode(rel.as_posix().encode("utf-8"))


def _encode_external_asset_id(abs_path: Path) -> str:
    """工作区**外**的交付文件路径 → 资产 id。"""
    return b64url_encode((_EXTERNAL_ID_TAG + abs_path.as_posix()).encode("utf-8"))


def _decode_asset_id(asset_id: str) -> tuple[Path, bool] | None:
    """资产 id → (路径, 是否工作区外)；非法（绝对路径/含 .. 或 NUL）返回 None。

    注意这里**只做格式校验，不做授权**：工作区内的相对路径还要过白名单，
    工作区外的绝对路径必须是本会话交付过的文件（见 :func:`_authorized_target`）。
    """
    if not _ASSET_ID_RE.fullmatch(asset_id):
        return None
    try:
        decoded = b64url_decode(asset_id).decode("utf-8")
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None
    if not decoded or "\x00" in decoded:
        return None
    external = decoded.startswith(_EXTERNAL_ID_TAG)
    raw = decoded[len(_EXTERNAL_ID_TAG):] if external else decoded
    path = Path(raw)
    if any(part == ".." for part in path.parts):
        return None
    if external:
        return (path, True) if path.is_absolute() else None
    if path.is_absolute() or ":" in raw:
        return None
    return path, False


def _asset_target(query: QueryParams) -> tuple[Path, bool] | None:
    """取 query 里的资产 id 并解码为 (路径, 是否工作区外)；非法返回 None。"""
    return _decode_asset_id((query_first(query, "id") or "").strip())


def asset_path_key(path: Path) -> str:
    """跨来源比较路径的规范键：Windows 大小写不敏感，``..`` 与软链归一。"""
    try:
        return os.path.normcase(os.path.realpath(path))
    except OSError:
        return os.path.normcase(str(path))


def delivered_path_keys(delivered: Iterable[Path]) -> frozenset[str]:
    return frozenset(asset_path_key(path) for path in delivered)


def session_ledger_keys(
    delivered: Iterable[Path],
    written: Iterable[Path],
) -> frozenset[str]:
    """本会话台账的规范键集合：交付过的 ∪ 写过的。

    这是「本会话可以触及」的授权依据，与展示列表的成员资格是两件事：工作区内的
    白名单产出即使没进台账也能看/删，台账成员则**不看扩展名**。
    """
    return delivered_path_keys(delivered) | delivered_path_keys(written)


def workspace_media_allowed(path: Path, delivered_keys: frozenset[str]) -> bool:
    """工作区媒体的准入判断：白名单产出扩展名，或**本会话交付过**的文件。

    抽成公开函数是给签名路由用的 —— 让「只签有资格当资产的文件」跟着签名一起发生，
    而不是指望每个调用方都先过一遍资产列表。交付过的文件不看扩展名（``.zip``/
    ``.json`` 交付出去了也得能看）。
    """
    return path.suffix.lower() in _WORKSPACE_ASSET_EXTS or asset_path_key(path) in delivered_keys


def _is_ledger_file(path: Path) -> bool:
    """台账里的路径要是常规文件才拿出来展示/处置（目录、断链一律跳过）。"""
    try:
        return path.is_file()
    except OSError:
        return False


def _ledger_listable(path: Path, *, delivered: bool) -> bool:
    """台账里的一条够不够格进列表：交付过的照单全收，写过的按产出白名单。

    写过的文件仍受扩展名白名单约束（``parse.py`` / ``bundle.js`` 这类中间脚本不进
    资产面板 —— 它们在聊天的文件编辑提示里已经可见），否则「资产」会被脚本刷屏。
    交付过的文件不受此限：交付即显示是硬保证。
    """
    return delivered or path.suffix.lower() in _WORKSPACE_ASSET_EXTS


def _locate_target(
    target: tuple[Path, bool],
    workspace: Path,
    ledger_keys: frozenset[str],
) -> Path | None:
    """把资产 id 解析成本会话**允许触及**的文件路径；解析不出来返回 None。

    - 工作区**内**的相对路径：必须仍落在工作区内（父目录软链绕出 → None —— 删的是
      用户的项目目录，不能只看路径字符串）；
    - 工作区**外**的绝对路径：只认本会话台账里的那些（交付过或写过）。``@:`` 编码
      本身**不是**授权，否则一个构造出来的 id 就能指向机器上任意文件。
    """
    raw, external = target
    if external:
        return raw if asset_path_key(raw) in ledger_keys else None
    return _within_workspace(raw, workspace)


def _asset_allowed(
    path: Path,
    target: tuple[Path, bool],
    ledger_keys: frozenset[str],
) -> bool:
    """够不够格当资产（``_locate_target`` 已保证路径在允许触及的范围内）。

    两类：本会话**台账里**的文件（不看扩展名 —— ``.zip``/``.json`` 交付出去了也得能
    看能删），或工作区内的产出型文件。
    """
    raw, external = target
    if asset_path_key(path) in ledger_keys and _is_ledger_file(path):
        return True
    if external:
        return False
    return _is_produced_file(path, raw)


def _within_workspace(rel: Path, workspace: Path) -> Path | None:
    """把相对路径限制在会话工作区内；越界（含父目录软链绕出）返回 None。

    只对**父目录**做 resolve + containment：若把末段一起 resolve，工作区里指向
    外部的一条软链会被判定为「越界」并报错，而它其实只是一个不该被当资产的条目。
    软链本身由 ``_is_produced_file`` 静默拒掉。
    """
    target = workspace / rel
    try:
        parent = target.parent.resolve()
        parent.relative_to(workspace.resolve())
    except (OSError, ValueError):
        return None
    return parent / target.name


def _is_produced_file(target: Path, rel: Path) -> bool:
    """是否是一个可删的产出文件。

    删的是**用户的项目目录**，比删 media 缓存后果重，所以不能只靠「路径在工作区内」
    这一条：还要求扩展名在产出白名单内、非隐藏、且是常规文件而非软链 —— 否则一个
    构造出来的 id 就能删掉 ``src/main.py``。
    """
    if rel.suffix.lower() not in _WORKSPACE_ASSET_EXTS:
        return False
    if any(part.startswith(".") for part in rel.parts):
        return False
    return not target.is_symlink() and target.is_file()


def _iter_workspace_asset_paths(
    workspace: Path,
    *,
    retain: int,
    max_candidates: int = _WORKSPACE_SCAN_MAX_CANDIDATES,
) -> tuple[list[tuple[float, Path]], bool]:
    """DFS 遍历工作区，返回（[(mtime, 产出型文件)] 按 mtime 降序子集, 是否有文件被丢下）。

    手写 ``os.scandir`` 而不是 ``rglob``：rglob 会把整棵子树先物化再过滤，工作区
    可能是个大仓库，那样既慢又占内存；手写还能按目录名剪枝、不跟随软链。

    **保留集在遍历中按 mtime 有界挑选**（大小为 ``retain`` 的小顶堆），不先把命中
    全收下来再截断：DFS 顺序与时间无关，先收后截会把「刚生成的产出」随机丢掉。
    这里用 mtime 而不是 sidecar 的 ``created_at`` 做挑选键，是因为挑选发生在遍历热
    路径上（每个候选都要判），读 sidecar 是 I/O；产物落盘后不再改动，mtime 与
    ``created_at`` 一致。返回的 mtime 顺带给调用方当 created_at 的回落值。
    """
    kept: list[tuple[float, Path]] = []
    seen = 0
    if retain <= 0 or not workspace.is_dir():
        return [], False
    truncated = False
    stack = [workspace]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    name = entry.name
                    if name.startswith("."):
                        continue
                    # follow_symlinks=False：软链既不当目录钻（防环路），也不当文件收。
                    if entry.is_dir(follow_symlinks=False):
                        if name not in _PRUNE_DIR_NAMES:
                            stack.append(Path(entry.path))
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    if Path(name).suffix.lower() not in _WORKSPACE_ASSET_EXTS:
                        continue
                    if seen >= max_candidates:
                        truncated = True
                        break
                    seen += 1
                    try:
                        mtime = entry.stat(follow_symlinks=False).st_mtime
                    except OSError:
                        mtime = 0.0
                    candidate = (mtime, Path(entry.path))
                    if len(kept) < retain:
                        heapq.heappush(kept, candidate)
                    elif mtime > kept[0][0]:
                        heapq.heapreplace(kept, candidate)
        except OSError:
            continue
        if truncated:
            break
    # seen > retain 意味着至少有一个候选被丢下（无论是被堆顶挤掉还是堆已满且更旧）。
    return sorted(kept, reverse=True), truncated or seen > retain


def _scan_entry(
    path: Path,
    rel: Path,
    mtime: float,
    sign_media: SignedMediaPath | None,
    *,
    delivered: bool,
) -> tuple[datetime, str, dict[str, Any]]:
    """工作区扫描到的一条资产（sidecar 提供 caption / created_at）。"""
    sidecar = path.with_suffix(".json")
    meta = _load_sidecar(sidecar) if sidecar.is_file() else {}
    created = meta.get("created_at")
    if not isinstance(created, str) or not created:
        created = _iso_from_mtime(mtime)
    caption = meta.get("prompt")
    if not isinstance(caption, str):
        caption = ""
    media_url = sign_media(path) if sign_media is not None else None
    item = _asset_item(
        _encode_asset_id(rel),
        path.name,
        _kind_for_suffix(path.suffix),
        _file_size(path),
        created,
        caption,
        media_url or "",
        delivered=delivered,
        path=rel.as_posix(),
    )
    # 排序键始终是 aware datetime：解析不出来才回落 mtime。拿 created_at 字符串
    # 直接比较会在混时区时排错（见 ``_sort_instant``）。
    instant = _sort_instant(created) or datetime.fromtimestamp(mtime, tz=timezone.utc)
    return instant, path.name, item


def _ledger_entry(
    path: Path,
    workspace: Path,
    sign_media: SignedMediaPath | None,
    sign_external: SignedMediaPath | None,
    *,
    delivered: bool,
) -> tuple[datetime, str, dict[str, Any]] | None:
    """台账里、工作区扫描**没覆盖到**的一条：工作区外的文件，或扩展名不在白名单。

    id 与展示 URL 都按「在不在工作区内」分派：工作区内走会话工作区签名路由（不复制
    原文件），工作区外走 media 根的签名/稳定 staging（交付那一刻发出去的就是它，
    副本按 mtime 稳定命名，已存在就不会再复制）。
    """
    if not _is_ledger_file(path):
        return None
    try:
        mtime = path.stat().st_mtime
        instant = datetime.fromtimestamp(mtime).astimezone()
    except (OSError, OverflowError, ValueError):
        return None
    try:
        rel: Path | None = path.relative_to(workspace)
    except ValueError:
        rel = None
    if rel is not None:
        asset_id = _encode_asset_id(rel)
        media_url = sign_media(path) if sign_media is not None else None
    else:
        asset_id = _encode_external_asset_id(path)
        media_url = sign_external(path) if sign_external is not None else None
    item = _asset_item(
        asset_id,
        path.name,
        _kind_for_suffix(path.suffix),
        _file_size(path),
        instant.isoformat(),
        "",
        media_url or "",
        delivered=delivered,
        path=rel.as_posix() if rel is not None else "",
    )
    return instant, path.name, item


def assets_payload(
    *,
    workspace: Path,
    delivered: Iterable[Path] = (),
    written: Iterable[Path] = (),
    scan_workspace: bool = True,
    sign_media: SignedMediaPath | None = None,
    sign_external: SignedMediaPath | None = None,
    max_items: int = _MAX_ASSET_ITEMS,
) -> dict[str, Any]:
    """列出会话资产，按 created_at 降序。

    两种口径，由 ``scan_workspace`` 选（WebUI 路由默认走「本会话」，见
    ``ws_http._scan_workspace``）：

    - ``True``（**整个工作区**）：工作区扫描 ∪ 台账 —— 看的是「这个工作区里有什么」，
      会话不设独立工作区时各会话共享同一份列表；
    - ``False``（**本会话**）：只列台账（交付过 ∪ 写过）—— 看的是「这个会话产出了
      什么」，与工作区是否共享无关，这才是按会话隔离。

    ``delivered`` / ``written`` 来自会话记录（:mod:`xianaibot.webui.session_deliveries`）。
    交付过的优先于其余条目：条数触顶时**先删非交付条目**，交付过的文件一定留在列表里
    （那是「交付给用户的文件都能看到」的保证）。
    """
    delivered_keys = delivered_path_keys(delivered)
    ranked: list[tuple[datetime, str, dict[str, Any]]] = []
    known_keys: set[str] = set()
    truncated = False
    if scan_workspace:
        pairs, truncated = _iter_workspace_asset_paths(workspace, retain=max_items)
        for mtime, path in pairs:
            try:
                rel = path.relative_to(workspace)
            except ValueError:
                continue
            key = asset_path_key(path)
            known_keys.add(key)
            ranked.append(
                _scan_entry(path, rel, mtime, sign_media, delivered=key in delivered_keys)
            )
    # 补齐扫描没覆盖到（或本轮不扫）的台账条目：工作区外的、扩展名不在白名单里的。
    ledger: list[tuple[Path, bool]] = [(path, True) for path in delivered]
    ledger.extend((path, False) for path in written)
    for path, is_delivered in ledger:
        key = asset_path_key(path)
        if key in known_keys:
            continue
        if not _ledger_listable(path, delivered=is_delivered):
            continue
        entry = _ledger_entry(
            path, workspace, sign_media, sign_external, delivered=is_delivered
        )
        if entry is None:
            continue
        known_keys.add(key)
        ranked.append(entry)
    ranked.sort(key=lambda row: (row[0], row[1]), reverse=True)
    if len(ranked) > max_items:
        kept_delivered = [row for row in ranked if row[2]["delivered"]][:max_items]
        budget = max(max_items - len(kept_delivered), 0)
        kept_rest = [row for row in ranked if not row[2]["delivered"]][:budget]
        ranked = sorted(kept_delivered + kept_rest, key=lambda row: (row[0], row[1]), reverse=True)
        truncated = True
    return {"assets": [row[2] for row in ranked], "truncated": truncated}


def _safe_unlink(path: Path, root: Path) -> bool:
    """删除 ``root`` 内的文件；resolve 后必须仍位于 ``root`` 内（防穿越）。"""
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False


_PREVIEW_MAX_CHARS = 200_000


def document_preview(
    query: QueryParams,
    *,
    workspace: Path,
    delivered: Iterable[Path] = (),
    written: Iterable[Path] = (),
) -> dict[str, Any]:
    """返回文档资产的文本预览。

    docx/pdf/xlsx/pptx/txt/md/html 经 ``xianaibot.utils.document.extract_text``
    提取为纯文本（浏览器无法原生渲染 docx，这里给一个可读的文本预览）。
    """
    target = _asset_target(query)
    if target is None:
        raise WebUISettingsError("invalid asset id")
    ledger_keys = session_ledger_keys(delivered, written)
    path = _locate_target(target, workspace, ledger_keys)
    if path is None or not _asset_allowed(path, target, ledger_keys):
        raise WebUISettingsError("asset not found")
    if path.suffix.lower() not in _DOCUMENT_EXTS:
        raise WebUISettingsError("asset is not a document")

    from xianaibot.utils.document import extract_text

    try:
        text = extract_text(path) or ""
    except Exception:
        text = ""
    truncated = len(text) > _PREVIEW_MAX_CHARS
    if truncated:
        text = text[:_PREVIEW_MAX_CHARS]
    return {
        "id": (query_first(query, "id") or "").strip(),
        "name": path.name,
        "kind": "document",
        "size": _file_size(path),
        "content": text,
        "truncated": truncated,
    }


def reveal_asset_path(
    query: QueryParams,
    *,
    workspace: Path,
    delivered: Iterable[Path] = (),
    written: Iterable[Path] = (),
) -> Path:
    """按 id 解析出要「在文件夹中显示」的原文件；不够格就报错。

    授权与删除同源（工作区内的产出型文件，或本会话台账里交付过/写过的文件）：能删的
    文件才谈得上定位。``@:`` 绝对路径本身不是授权 —— 构造出来的 id 指向机器上任意
    文件都会被拒（见 ``_locate_target``）。

    这里只解析路径，真正的系统调用（explorer /select、open -R）由调用方执行。
    """
    target = _asset_target(query)
    if target is None:
        raise WebUISettingsError("invalid asset id")
    ledger_keys = session_ledger_keys(delivered, written)
    path = _locate_target(target, workspace, ledger_keys)
    if path is None:
        raise WebUISettingsError("asset is outside the workspace")
    if not _asset_allowed(path, target, ledger_keys):
        raise WebUISettingsError("asset not found")
    return path


def _remove(path: Path, workspace: Path) -> bool:
    """删一个已授权的资产文件。

    工作区内的走 ``_safe_unlink``（resolve 后仍须在工作区内）；工作区外的交付文件直接
    ``unlink`` —— 它的授权来自交付台账（本会话确实把这条路径发给了用户），不是路径
    包含关系，所以没有「工作区内」这层可依赖的边界。
    """
    try:
        inside = path.is_relative_to(workspace)
    except (OSError, ValueError):
        inside = False
    if inside:
        return _safe_unlink(path, workspace)
    try:
        path.unlink()
        return True
    except OSError:
        return False


def delete_asset(
    query: QueryParams,
    *,
    workspace: Path,
    delivered: Iterable[Path] = (),
    written: Iterable[Path] = (),
    scan_workspace: bool = True,
    sign_media: SignedMediaPath | None = None,
    sign_external: SignedMediaPath | None = None,
) -> dict[str, Any]:
    """按 id 删除一个会话资产，返回更新后的资产列表。

    id 是路径编码（不按文件名 stem 反查）：工作区里两个 ``报告.docx`` 各对应各自的
    id，删除只命中一个，也不会在整棵工作区上做全树遍历。

    幂等语义与 knowledge delete 契约一致：格式非法 → 报错；格式合法但没有授权或目标
    已经不在了（已被删/被移走）→ 直接返回当前列表，不报错。
    """
    target = _asset_target(query)
    if target is None:
        raise WebUISettingsError("invalid asset id")
    ledger_keys = session_ledger_keys(delivered, written)
    path = _locate_target(target, workspace, ledger_keys)
    if path is None:
        raise WebUISettingsError("asset is outside the workspace")
    if _asset_allowed(path, target, ledger_keys):
        if not _remove(path, workspace):
            raise WebUISettingsError("failed to delete asset")
    return assets_payload(
        workspace=workspace,
        delivered=delivered,
        written=written,
        scan_workspace=scan_workspace,
        sign_media=sign_media,
        sign_external=sign_external,
    )
