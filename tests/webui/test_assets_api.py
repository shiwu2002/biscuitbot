"""会话资产（assets_api）测试。

资产 = 会话工作区里的产出型文件。扫描根是调用方传入的 workspace（会话作用域
的 ``project_path``），所以这里不需要构造 config —— 直接给一个临时工作区即可。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from xianaibot.webui.assets_api import (
    _encode_asset_id,
    _encode_external_asset_id,
    assets_payload,
    delete_asset,
    document_preview,
    reveal_asset_path,
)
from xianaibot.webui.settings_api import WebUISettingsError

IMG_ID = "img_1234567890ab"
VID_ID = "vid_1234567890ab"
DOC_ID = "e9329aaeb3bc_report"


def _write(path: Path, data: bytes = b"x" * 64) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _fake_sign(path: Path) -> str:
    return f"/api/sessions/test/workspace-media/sig/{path.name}"


def _asset_id(rel: str) -> str:
    """按工作区相对路径构造资产 id（与后端编码器一致）。"""
    return _encode_asset_id(Path(rel))


# ── 列出 ────────────────────────────────────────────────────────────


def test_lists_agent_outputs_from_the_session_workspace(tmp_path: Path) -> None:
    """回归用例：生成的图片/视频落在工作区，必须出现在资产列表里。

    修复前 assets_payload 扫的是 media 根（``~/.xianaibot/media``），而 0ee7f03
    之后产出实际落在 ``<workspace>/generated``、``<workspace>/generated_video``，
    于是图片/视频根本列不出来。
    """
    ws = tmp_path / "workspace"
    img = ws / "generated" / "2026-10-12" / f"{IMG_ID}.png"
    _write(img)
    img.with_suffix(".json").write_text(
        json.dumps(
            {"prompt": "a red fox", "created_at": "2026-10-12T10:00:00+08:00", "model": "x"}
        ),
        encoding="utf-8",
    )

    vid = ws / "generated_video" / "2026-10-12" / f"{VID_ID}.mp4"
    _write(vid)
    vid.with_suffix(".json").write_text(
        json.dumps({"created_at": "2026-10-12T11:00:00+08:00"}), encoding="utf-8"
    )

    doc = ws / "报告.docx"
    _write(doc, b"x" * 128)

    tts = ws / "generated" / "tts" / "2026-10-12" / "tts_1234567890ab.mp3"
    _write(tts)

    payload = assets_payload(workspace=ws, sign_media=_fake_sign)
    assert payload["truncated"] is False
    assets = payload["assets"]
    by_kind = {a["kind"]: a for a in assets}
    assert set(by_kind) == {"image", "video", "audio", "document"}

    image = by_kind["image"]
    assert image["name"] == f"{IMG_ID}.png"
    assert image["caption"] == "a red fox"
    assert image["created_at"] == "2026-10-12T10:00:00+08:00"
    assert image["media_url"] == _fake_sign(img)
    assert image["size"] == 64
    assert image["channel"] == ""

    video = by_kind["video"]
    assert video["caption"] == ""
    assert video["created_at"] == "2026-10-12T11:00:00+08:00"

    # 无 sidecar → 回落文件 mtime
    assert by_kind["document"]["created_at"]
    assert by_kind["document"]["size"] == 128
    assert by_kind["audio"]["created_at"]


def test_sorts_by_created_at_desc(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    for name, created in (("old.png", "2026-01-01T00:00:00+08:00"), ("new.png", "2026-06-01T00:00:00+08:00")):
        path = ws / name
        _write(path)
        path.with_suffix(".json").write_text(
            json.dumps({"created_at": created}), encoding="utf-8"
        )

    names = [a["name"] for a in assets_payload(workspace=ws)["assets"]]
    assert names == ["new.png", "old.png"]


def test_id_is_path_encoded_so_duplicate_names_stay_distinct(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    _write(ws / "a" / "报告.docx")
    _write(ws / "b" / "报告.docx")

    assets = assets_payload(workspace=ws)["assets"]
    assert len(assets) == 2
    # 同名文件必须各有各的 id，否则前端列表 key 冲突、删除会误伤
    assert len({a["id"] for a in assets}) == 2


def test_skips_source_files_sidecars_and_noise_directories(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    _write(ws / "main.py")
    _write(ws / "app.ts")
    _write(ws / "data.json")  # sidecar 类，不收
    _write(ws / "generated" / "2026-10-12" / f"{IMG_ID}.json")  # 纯 sidecar
    _write(ws / ".git" / "secret.png")
    _write(ws / ".hidden.png")
    _write(ws / "node_modules" / "pkg" / "logo.png")
    _write(ws / "__pycache__" / "x.png")
    _write(ws / "dist" / "chart.png")
    _write(ws / "report.html")  # 产出型：收

    names = [a["name"] for a in assets_payload(workspace=ws)["assets"]]
    assert names == ["report.html"]


def test_documents_include_html_reports(tmp_path: Path) -> None:
    """智能体最常产出的就是 HTML 报告，不能当成源码排除掉。"""
    ws = tmp_path / "workspace"
    _write(ws / "报告.html")
    _write(ws / "report.htm")

    assets = assets_payload(workspace=ws)["assets"]
    assert {a["kind"] for a in assets} == {"document"}
    assert len(assets) == 2


def test_scan_cap_keeps_the_newest_and_reports_truncation(tmp_path: Path) -> None:
    """触顶时保留的是**最新**的产出，而不是 DFS 顺序上恰好先遇到的。

    DFS 顺序与时间无关：先收满上限再截断，会让刚生成的产出随机消失（旧的
    ``_WORKSPACE_SCAN_MAX_FILES`` 行为）。所以这里显式给不同 mtime 验证取舍方向。
    """
    ws = tmp_path / "workspace"
    base = 1_700_000_000.0
    for index in range(5):
        path = ws / f"img_{index}.png"
        _write(path)
        os.utime(path, (base + index, base + index))

    payload = assets_payload(workspace=ws, max_items=3)
    assert payload["truncated"] is True
    assert [a["name"] for a in payload["assets"]] == [
        "img_4.png",
        "img_3.png",
        "img_2.png",
    ]


def test_sorts_by_instant_across_differing_utc_offsets(tmp_path: Path) -> None:
    """按**时刻**排序，不按 created_at 字符串。

    ``09:00+09:00`` 是 00:00Z，``08:30+08:00`` 是 00:30Z —— 后者更晚。字符串比较
    只看字符，会把 ``T09`` 判成更大，于是顺序颠倒。
    """
    ws = tmp_path / "workspace"
    for name, created in (
        ("tokyo.png", "2026-10-12T09:00:00+09:00"),
        ("shanghai.png", "2026-10-12T08:30:00+08:00"),
    ):
        path = ws / name
        _write(path)
        path.with_suffix(".json").write_text(
            json.dumps({"created_at": created}), encoding="utf-8"
        )

    names = [a["name"] for a in assets_payload(workspace=ws)["assets"]]
    assert names == ["shanghai.png", "tokyo.png"]


def test_tolerates_unparsable_or_naive_created_at(tmp_path: Path) -> None:
    """老数据可能不带偏移、甚至是个坏串；都要能排下去而不是抛 TypeError。"""
    ws = tmp_path / "workspace"
    for name, created in (
        ("naive.png", "2026-10-12T10:00:00"),
        ("broken.png", "not-a-timestamp"),
        ("aware.png", "2026-10-12T23:00:00+08:00"),
    ):
        path = ws / name
        _write(path)
        path.with_suffix(".json").write_text(
            json.dumps({"created_at": created}), encoding="utf-8"
        )

    assets = assets_payload(workspace=ws)["assets"]
    assert {a["name"] for a in assets} == {"naive.png", "broken.png", "aware.png"}


def test_missing_workspace_is_empty_not_an_error(tmp_path: Path) -> None:
    payload = assets_payload(workspace=tmp_path / "nope")
    assert payload == {"assets": [], "truncated": False}


def test_symlinked_directory_is_not_traversed(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True)
    outside = tmp_path / "outside"
    _write(outside / "leaked.png")
    try:
        os.symlink(outside, ws / "link", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this platform")

    assert assets_payload(workspace=ws)["assets"] == []


# ── 删除 ────────────────────────────────────────────────────────────


def test_delete_removes_only_the_targeted_file(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    _write(ws / "a" / "报告.docx")
    _write(ws / "b" / "报告.docx")
    assets = assets_payload(workspace=ws)["assets"]
    target = next(a for a in assets if a["id"] == _asset_id("a/报告.docx"))

    payload = delete_asset({"id": [target["id"]]}, workspace=ws)

    assert not (ws / "a" / "报告.docx").exists()
    assert (ws / "b" / "报告.docx").exists()  # 同名的另一个不受影响
    assert [a["name"] for a in payload["assets"]] == ["报告.docx"]


def test_delete_keeps_sidecar_untouched(tmp_path: Path) -> None:
    """sidecar 不是资产，也不该被顺手删掉（它记录 prompt/created_at）。"""
    ws = tmp_path / "workspace"
    img = ws / "generated" / f"{IMG_ID}.png"
    _write(img)
    sidecar = img.with_suffix(".json")
    sidecar.write_text(json.dumps({"prompt": "p"}), encoding="utf-8")

    delete_asset({"id": [_asset_id(f"generated/{IMG_ID}.png")]}, workspace=ws)

    assert not img.exists()
    assert sidecar.exists()


def test_delete_is_idempotent_for_a_file_that_is_already_gone(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    _write(ws / "keep.png")

    payload = delete_asset({"id": [_asset_id("vanished.png")]}, workspace=ws)

    assert [a["name"] for a in payload["assets"]] == ["keep.png"]


def test_delete_refuses_hidden_non_asset_and_symlink_paths(tmp_path: Path) -> None:
    """删的是用户的项目目录：白名单扩展名 / 非隐藏 / 非软链 三道闸都要在。"""
    ws = tmp_path / "workspace"
    _write(ws / "src" / "main.py")
    _write(ws / ".env.png")  # 隐藏
    outside = tmp_path / "outside.png"
    _write(outside)

    for rel in ("src/main.py", ".env.png"):
        delete_asset({"id": [_asset_id(rel)]}, workspace=ws)
        assert (ws / rel).exists(), f"{rel} 不该被删除"

    try:
        os.symlink(outside, ws / "link.png")
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this platform")
    delete_asset({"id": [_asset_id("link.png")]}, workspace=ws)
    assert (ws / "link.png").exists()
    assert outside.exists()


@pytest.mark.parametrize("bad", ["", "..", "../x", "not-base64!!", "a b", "a.b@"])
def test_delete_rejects_invalid_id(tmp_path: Path, bad: str) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True)
    with pytest.raises(WebUISettingsError):
        delete_asset({"id": [bad]}, workspace=ws)


def test_decode_rejects_traversal_and_absolute_paths(tmp_path: Path) -> None:
    """构造出来的 id 不能把删除引到工作区之外。"""
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True)
    secret = tmp_path / "outside.png"
    _write(secret)

    outside_ids = [
        _encode_asset_id(Path("../outside.png")),
        _encode_asset_id(Path("sub/../../outside.png")),
        _encode_asset_id(Path("/etc/passwd.png")),
        _encode_asset_id(Path("C:/Windows/x.png")),
    ]
    for asset_id in outside_ids:
        with pytest.raises(WebUISettingsError):
            delete_asset({"id": [asset_id]}, workspace=ws)
    assert secret.exists()


# ── 文档预览 ────────────────────────────────────────────────────────


def test_document_preview_extracts_docx_text(tmp_path: Path) -> None:
    from docx import Document

    ws = tmp_path / "workspace"
    doc = ws / f"{DOC_ID}.docx"
    doc.parent.mkdir(parents=True, exist_ok=True)
    d = Document()
    d.add_paragraph("Total revenue: $5,000,000")
    d.save(str(doc))

    payload = document_preview({"id": [_asset_id(f"{DOC_ID}.docx")]}, workspace=ws)

    assert payload["name"] == f"{DOC_ID}.docx"
    assert payload["size"] > 0
    assert "Total revenue: $5,000,000" in payload["content"]
    assert payload["truncated"] is False


def test_document_preview_rejects_missing_bad_and_non_document_ids(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    _write(ws / f"{IMG_ID}.png")

    with pytest.raises(WebUISettingsError):
        document_preview({"id": [_asset_id(f"{DOC_ID}.docx")]}, workspace=ws)  # 不存在
    with pytest.raises(WebUISettingsError):
        document_preview({"id": ["../x"]}, workspace=ws)  # 非法 id
    with pytest.raises(WebUISettingsError):
        document_preview({"id": [_asset_id(f"{IMG_ID}.png")]}, workspace=ws)  # 非文档


# ── 交付台账：智能体交给用户的文件 ───────────────────────────────────


def test_delivered_files_bypass_the_extension_whitelist(tmp_path: Path) -> None:
    """交付出去了就要看得见：``.zip``/``.json`` 不在产出白名单里，也不能漏。"""
    ws = tmp_path / "workspace"
    archive = ws / "报告打包.zip"
    data = ws / "data.json"  # .json 故意不在白名单（sidecar 会刷屏）
    _write(archive)
    _write(data)

    payload = assets_payload(workspace=ws, delivered=[archive, data])

    by_name = {a["name"]: a for a in payload["assets"]}
    assert set(by_name) == {"报告打包.zip", "data.json"}
    assert all(a["delivered"] is True for a in by_name.values())
    # 工作区内的交付文件仍用工作区相对路径编码
    assert by_name["data.json"]["id"] == _asset_id("data.json")


def test_delivered_file_outside_the_workspace_uses_external_id(tmp_path: Path) -> None:
    """工作区外的交付文件：id 走 ``@:`` 绝对路径编码，地址走 media 根签名。"""
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True)
    outside = tmp_path / "elsewhere" / "交付说明.pdf"
    _write(outside)

    payload = assets_payload(
        workspace=ws,
        delivered=[outside],
        sign_external=lambda p: f"/api/media/sig/{p.name}",
    )

    (asset,) = payload["assets"]
    assert asset["name"] == "交付说明.pdf"
    assert asset["media_url"] == "/api/media/sig/交付说明.pdf"
    assert asset["delivered"] is True
    assert asset["id"] == _encode_external_asset_id(outside)


def test_delivered_scan_hit_is_not_duplicated(tmp_path: Path) -> None:
    """扫描也能看到、又交付过的文件只出现一次，并标记为交付。"""
    ws = tmp_path / "workspace"
    img = ws / "generated" / f"{IMG_ID}.png"
    _write(img)

    payload = assets_payload(workspace=ws, delivered=[img])

    (asset,) = payload["assets"]
    assert asset["name"] == f"{IMG_ID}.png"
    assert asset["delivered"] is True


def test_delivered_path_matching_is_canonical(tmp_path: Path) -> None:
    """记录里的路径可能带 ``..`` 写法：归一后必须认出是同一个文件。"""
    ws = tmp_path / "workspace"
    _write(ws / "generated" / f"{IMG_ID}.png")
    messy = ws / "generated" / ".." / "generated" / f"{IMG_ID}.png"

    payload = assets_payload(workspace=ws, delivered=[messy])

    (asset,) = payload["assets"]
    assert asset["delivered"] is True


def test_scanned_but_not_delivered_is_marked_false(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    _write(ws / "generated" / f"{IMG_ID}.png")

    (asset,) = assets_payload(workspace=ws)["assets"]

    assert asset["delivered"] is False


def test_delivered_items_survive_the_item_cap(tmp_path: Path) -> None:
    """条数触顶时先挤掉扫描条目：交付过的必须留下。"""
    ws = tmp_path / "workspace"
    base = 1_700_000_000.0
    for index in range(5):
        path = ws / f"img_{index}.png"
        _write(path)
        os.utime(path, (base + index, base + index))
    oldest = ws / "delivered.zip"
    _write(oldest)
    os.utime(oldest, (base - 100, base - 100))  # 比所有扫描条目都旧

    payload = assets_payload(workspace=ws, delivered=[oldest], max_items=3)

    names = [a["name"] for a in payload["assets"]]
    assert len(names) == 3
    assert "delivered.zip" in names  # 最旧，但因为交付过所以留下
    assert payload["truncated"] is True


def test_delete_removes_a_delivered_file_outside_the_workspace(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True)
    outside = tmp_path / "elsewhere" / "old.zip"
    _write(outside)

    payload = delete_asset(
        {"id": [_encode_external_asset_id(outside)]},
        workspace=ws,
        delivered=[outside],
    )

    assert not outside.exists()
    assert payload["assets"] == []


def test_delete_refuses_external_paths_not_in_the_ledger(tmp_path: Path) -> None:
    """``@:`` 编码本身不是授权：台账里没有的绝对路径，构造出来也删不掉。"""
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True)
    victim = tmp_path / "victim.png"
    _write(victim)

    with pytest.raises(WebUISettingsError):
        delete_asset({"id": [_encode_external_asset_id(victim)]}, workspace=ws)

    assert victim.exists()


def test_delete_removes_delivered_file_with_non_whitelisted_extension(tmp_path: Path) -> None:
    """工作区里的 ``.zip`` 交付过就能删：授权看交付记录，不看扩展名白名单。"""
    ws = tmp_path / "workspace"
    archive = ws / "bundle.zip"
    _write(archive)

    delete_asset({"id": [_asset_id("bundle.zip")]}, workspace=ws, delivered=[archive])

    assert not archive.exists()


def test_preview_refuses_delivered_non_document(tmp_path: Path) -> None:
    """交付只保证「看得见」，文档预览仍只服务文档类型。"""
    ws = tmp_path / "workspace"
    archive = ws / "bundle.zip"
    _write(archive)

    with pytest.raises(WebUISettingsError):
        document_preview(
            {"id": [_asset_id("bundle.zip")]},
            workspace=ws,
            delivered=[archive],
        )


# ── 本会话口径：只列台账（写过的 ∪ 交付过的） ────────────────────────


def test_session_view_lists_only_ledger_files(tmp_path: Path) -> None:
    """``scan_workspace=False``：同工作区里别人写的文件不出现 —— 这才是会话隔离。"""
    ws = tmp_path / "workspace"
    mine = ws / "generated" / f"{IMG_ID}.png"
    theirs = ws / "generated" / "img_other.png"
    _write(mine)
    _write(theirs)

    payload = assets_payload(
        workspace=ws,
        written=[mine],
        scan_workspace=False,
        sign_media=_fake_sign,
    )

    (asset,) = payload["assets"]
    assert asset["name"] == f"{IMG_ID}.png"
    assert asset["delivered"] is False
    assert asset["media_url"] == _fake_sign(mine)


def test_session_view_deduplicates_a_written_and_delivered_file(tmp_path: Path) -> None:
    """既写过又交付过的文件只出现一次，且以「交付」身份出现。"""
    ws = tmp_path / "workspace"
    doc = ws / "报告.docx"
    _write(doc)

    payload = assets_payload(
        workspace=ws,
        delivered=[doc],
        written=[doc],
        scan_workspace=False,
    )

    (asset,) = payload["assets"]
    assert asset["name"] == "报告.docx"
    assert asset["delivered"] is True


def test_session_view_skips_intermediate_scripts(tmp_path: Path) -> None:
    """写过的文件仍受产出白名单约束：``.py``/``.js`` 中间脚本不进资产面板。"""
    ws = tmp_path / "workspace"
    script = ws / "mem-report" / "parse.py"
    report = ws / "mem-report" / "index.html"
    _write(script)
    _write(report)

    payload = assets_payload(
        workspace=ws,
        written=[script, report],
        scan_workspace=False,
    )

    assert [a["name"] for a in payload["assets"]] == ["index.html"]


def test_session_view_skips_written_files_that_are_gone(tmp_path: Path) -> None:
    """台账里的路径可能已经被删掉/移走：不报错，只是不出现。"""
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True)
    gone = ws / "generated" / f"{IMG_ID}.png"

    payload = assets_payload(workspace=ws, written=[gone], scan_workspace=False)

    assert payload["assets"] == []


def test_session_view_written_file_outside_the_workspace_uses_external_id(
    tmp_path: Path,
) -> None:
    """工作区外写过的文件（如直接写到用户目录）：同样列得出来。"""
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True)
    outside = tmp_path / "elsewhere" / "报告.docx"
    _write(outside)

    payload = assets_payload(
        workspace=ws,
        written=[outside],
        scan_workspace=False,
        sign_external=lambda p: f"/api/media/sig/{p.name}",
    )

    (asset,) = payload["assets"]
    assert asset["id"] == _encode_external_asset_id(outside)
    assert asset["media_url"] == "/api/media/sig/报告.docx"


def test_delete_removes_a_written_file(tmp_path: Path) -> None:
    """写过 = 有资格处置：本会话写的文件可以删（否则面板上会有删不掉的卡片）。"""
    ws = tmp_path / "workspace"
    doc = ws / "报告.docx"
    _write(doc)

    payload = delete_asset(
        {"id": [_asset_id("报告.docx")]},
        workspace=ws,
        written=[doc],
        scan_workspace=False,
    )

    assert not doc.exists()
    assert payload["assets"] == []


def test_delete_removes_a_written_file_outside_the_workspace(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True)
    outside = tmp_path / "elsewhere" / "报告.docx"
    _write(outside)

    delete_asset(
        {"id": [_encode_external_asset_id(outside)]},
        workspace=ws,
        written=[outside],
        scan_workspace=False,
    )

    assert not outside.exists()


def test_preview_serves_a_written_file(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    doc = ws / "报告.txt"
    _write(doc, "<h1>hi</h1>".encode())

    payload = document_preview(
        {"id": [_asset_id("报告.txt")]},
        workspace=ws,
        written=[doc],
    )

    assert payload["name"] == "报告.txt"
    assert "hi" in payload["content"]


# ── 定位原文件（在文件夹中显示） ─────────────────────────────────────


def test_reveal_resolves_a_workspace_output(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    img = ws / "generated" / f"{IMG_ID}.png"
    _write(img)

    resolved = reveal_asset_path({"id": [_asset_id(f"generated/{IMG_ID}.png")]}, workspace=ws)

    assert resolved == img


def test_reveal_resolves_a_ledger_file_outside_the_workspace(tmp_path: Path) -> None:
    """工作区外的交付件是最需要「文件在哪」的一类（预览不了、扫描也扫不到）。"""
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True)
    outside = tmp_path / "elsewhere" / "报告打包.zip"
    _write(outside)

    resolved = reveal_asset_path(
        {"id": [_encode_external_asset_id(outside)]},
        workspace=ws,
        delivered=[outside],
    )

    assert resolved == outside


def test_reveal_refuses_paths_outside_the_ledger(tmp_path: Path) -> None:
    """``@:`` 编码不是授权：台账里没有的绝对路径，构造出来也不给定位。"""
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True)
    victim = tmp_path / "victim.png"
    _write(victim)

    with pytest.raises(WebUISettingsError):
        reveal_asset_path({"id": [_encode_external_asset_id(victim)]}, workspace=ws)


def test_reveal_refuses_non_asset_paths_inside_the_workspace(tmp_path: Path) -> None:
    """工作区里的源码/侧车不是资产，不能借定位入口把它们暴露出去。"""
    ws = tmp_path / "workspace"
    source = ws / "src" / "main.py"
    _write(source)

    with pytest.raises(WebUISettingsError):
        reveal_asset_path({"id": [_asset_id("src/main.py")]}, workspace=ws)


def test_reveal_rejects_invalid_id(tmp_path: Path) -> None:
    with pytest.raises(WebUISettingsError):
        reveal_asset_path({"id": ["!!not-base64!!"]}, workspace=tmp_path)
