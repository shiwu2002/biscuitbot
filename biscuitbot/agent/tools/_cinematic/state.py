"""AI 导演项目存储：分目录记忆 + 原子写。

职责与项目角色：
- ``ProjectStore`` 负责把项目状态持久化到 ``workspace/cinematic/<project_id>/``
  的分目录结构（bible.json / characters/ / locations/ / props/ / shots/ /
  reviews/），而非单文件 JSON；
- ``load`` 合并各文件为一份内存字典供 workflow 操作，``save`` 再拆回分目录；
- 所有写入走 ``tmp + os.replace`` 原子写，避免半写损坏。
"""

from __future__ import annotations

import json  # JSON 序列化
import os  # 原子写（os.replace）
from datetime import datetime  # 时间戳
from pathlib import Path  # 路径处理
from typing import Any

from biscuitbot.utils.helpers import ensure_dir  # 建目录

from .validators import CinematicDirectorError, validate_project_id

# 资产类型 -> 落盘目录名（文档第七节的分目录记忆）
_ASSET_DIRS = {"CHAR": "characters", "LOC": "locations", "PROP": "props"}


class ProjectStore:
    """AI 导演项目的分目录持久化存储。"""

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace).expanduser()  # 工作区路径，展开 ~

    def root(self, project_id: str | None) -> Path:
        """返回项目根目录；校验项目 ID 为安全 slug。"""
        validate_project_id(project_id)
        return self.workspace / "cinematic" / project_id

    def exists(self, project_id: str | None) -> bool:
        """项目是否已存在。"""
        return (self.root(project_id) / "project.json").exists()

    # ------------------------------------------------------------------
    # 文件读写
    # ------------------------------------------------------------------

    @staticmethod
    def _read_json(path: Path) -> Any:
        """读取 JSON 文件。"""
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _write_json(path: Path, data: Any) -> None:
        """原子写 JSON（tmp + os.replace）。"""
        ensure_dir(path.parent)
        encoded = json.dumps(data, ensure_ascii=False, indent=2)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(encoded, encoding="utf-8")
        os.replace(tmp, path)

    # ------------------------------------------------------------------
    # load / save
    # ------------------------------------------------------------------

    def load(self, project_id: str | None) -> dict[str, Any]:
        """合并分目录文件为一份内存字典（meta + bible + assets + shots + reviews）。"""
        root = self.root(project_id)
        meta_path = root / "project.json"
        if not meta_path.exists():
            raise CinematicDirectorError(
                f"project {project_id!r} not found; create it first with action=create_project"
            )
        try:
            meta = self._read_json(meta_path)
        except json.JSONDecodeError as exc:
            raise CinematicDirectorError(f"project {project_id!r} has corrupt project.json: {exc}") from exc
        if not isinstance(meta, dict):
            raise CinematicDirectorError(f"project {project_id!r} project.json is not an object")

        data: dict[str, Any] = dict(meta)
        data.setdefault("scenes", [])
        data.setdefault("reviews", {})

        # bible
        bible_path = root / "bible.json"
        data["bible"] = self._read_json(bible_path) if bible_path.exists() else {}

        # assets（characters/ + locations/ + props/）
        assets: dict[str, Any] = {}
        for kind, dirname in _ASSET_DIRS.items():
            directory = root / dirname
            if directory.exists():
                for f in sorted(directory.glob("*.json")):
                    asset = self._read_json(f)
                    assets[asset["id"]] = asset
        data["assets"] = assets

        # shots
        shots: list[Any] = []
        shots_dir = root / "shots"
        if shots_dir.exists():
            for f in sorted(shots_dir.glob("*.json")):
                shots.append(self._read_json(f))
        data["shots"] = shots

        # reviews
        reviews: dict[str, Any] = {}
        reviews_dir = root / "reviews"
        if reviews_dir.exists():
            for f in sorted(reviews_dir.glob("*.json")):
                reviews[f.stem] = self._read_json(f)
        data["reviews"] = reviews

        return data

    def save(self, project_id: str | None, data: dict[str, Any]) -> None:
        """把内存字典拆回分目录并原子写。"""
        root = self.root(project_id)
        ensure_dir(root)
        data["updated_at"] = datetime.now().isoformat()

        # 索引：meta + scenes（不包含 bible/assets/shots/reviews）
        meta_keys = (
            "schema_version", "project_id", "name", "stage", "completed",
            "scenes", "created_at", "updated_at",
        )
        meta = {k: data[k] for k in meta_keys if k in data}
        self._write_json(root / "project.json", meta)

        # bible
        self._write_json(root / "bible.json", data.get("bible") or {})

        # assets
        for asset in (data.get("assets") or {}).values():
            dirname = _ASSET_DIRS[asset["kind"]]
            self._write_json(root / dirname / f"{asset['id']}.json", asset)

        # shots
        for shot in data.get("shots") or []:
            self._write_json(root / "shots" / f"{shot['id']}.json", shot)

        # reviews
        for key, review in (data.get("reviews") or {}).items():
            self._write_json(root / "reviews" / f"{key}.json", review)
