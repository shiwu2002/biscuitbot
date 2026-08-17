from __future__ import annotations

import json
from pathlib import Path

import pytest

from biscuitbot.config.loader import save_config
from biscuitbot.config.schema import Config
from biscuitbot.webui.assets_api import assets_payload, delete_asset
from biscuitbot.webui.settings_api import WebUISettingsError

IMG_ID = "img_1234567890ab"
VID_ID = "vid_1234567890ab"
TTS_ID = "tts_1234567890ab"


def _make_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    """构造 config 环境：media 根 = config 目录/media，workspace 单独指定。"""
    config_path = tmp_path / "config.json"
    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "workspace")
    save_config(config, config_path)
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)
    return config_path.parent / "media", tmp_path / "workspace"


def _write(path: Path, data: bytes = b"x" * 64) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _fake_sign(path: Path) -> str:
    return f"/api/media/test/{path.name}"


def test_assets_payload_lists_images_videos_and_tts(tmp_path, monkeypatch) -> None:
    media_root, workspace = _make_env(tmp_path, monkeypatch)

    img = media_root / "generated" / "2026-08-17" / f"{IMG_ID}.png"
    _write(img)
    (img.with_suffix(".json")).write_text(
        json.dumps(
            {"prompt": "a red fox", "created_at": "2026-08-17T10:00:00+08:00", "model": "x"}
        ),
        encoding="utf-8",
    )

    vid = media_root / "generated_video" / "2026-08-17" / f"{VID_ID}.mp4"
    _write(vid)
    (vid.with_suffix(".json")).write_text(
        json.dumps({"created_at": "2026-08-17T11:00:00+08:00"}),
        encoding="utf-8",
    )

    tts = workspace / "generated" / "tts" / "2026-08-17" / f"{TTS_ID}.mp3"
    _write(tts)

    payload = assets_payload(_fake_sign)
    assets = payload["assets"]
    assert len(assets) == 3
    by_kind = {a["kind"]: a for a in assets}
    assert set(by_kind) == {"image", "video", "audio"}

    image = by_kind["image"]
    assert image["id"] == IMG_ID
    assert image["name"] == f"{IMG_ID}.png"
    assert image["caption"] == "a red fox"
    assert image["created_at"] == "2026-08-17T10:00:00+08:00"
    assert image["media_url"].endswith(f"{IMG_ID}.png")
    assert image["size"] == 64

    video = by_kind["video"]
    assert video["id"] == VID_ID
    assert video["caption"] == ""
    assert video["media_url"].endswith(f"{VID_ID}.mp4")

    audio = by_kind["audio"]
    assert audio["id"] == TTS_ID
    assert "tts-" in audio["media_url"]  # 走稳定 staging
    assert audio["media_url"].endswith(f"{TTS_ID}.mp3")
    assert audio["created_at"]  # 无 sidecar → 文件 mtime ISO
    assert audio["caption"] == ""


def test_tts_staging_is_idempotent(tmp_path, monkeypatch) -> None:
    media_root, workspace = _make_env(tmp_path, monkeypatch)
    tts = workspace / "generated" / "tts" / "2026-08-17" / f"{TTS_ID}.mp3"
    _write(tts)

    assets_payload(_fake_sign)
    assets_payload(_fake_sign)

    staged = list((media_root / "websocket").glob("tts-*"))
    assert len(staged) == 1


def test_delete_asset_removes_file_sidecar_and_staging(tmp_path, monkeypatch) -> None:
    media_root, workspace = _make_env(tmp_path, monkeypatch)

    img = media_root / "generated" / "2026-08-17" / f"{IMG_ID}.png"
    _write(img)
    (img.with_suffix(".json")).write_text(json.dumps({"prompt": "p"}), encoding="utf-8")

    tts = workspace / "generated" / "tts" / "2026-08-17" / f"{TTS_ID}.mp3"
    _write(tts)
    assets_payload(_fake_sign)  # 建立 staging 副本

    assert len(list((media_root / "websocket").glob("tts-*"))) == 1

    payload = delete_asset({"id": [IMG_ID]}, _fake_sign)
    assert not img.exists()
    assert not img.with_suffix(".json").exists()
    assert {a["kind"] for a in payload["assets"]} == {"audio"}

    payload = delete_asset({"id": [TTS_ID]}, _fake_sign)
    assert not tts.exists()
    assert not list((media_root / "websocket").glob("tts-*"))
    assert payload["assets"] == []


@pytest.mark.parametrize(
    "bad",
    ["../" + IMG_ID, IMG_ID + ".png", "img_xyz", "img_", "", "IMAGE_1234567890AB"],
)
def test_delete_asset_rejects_invalid_id(tmp_path, monkeypatch, bad: str) -> None:
    _make_env(tmp_path, monkeypatch)
    with pytest.raises(WebUISettingsError):
        delete_asset({"id": [bad]}, _fake_sign)
