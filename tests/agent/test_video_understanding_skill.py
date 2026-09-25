"""`video-understanding` 内置技能的守卫测试（README 级契约，不是实现细节）。

这个技能是「删掉视频直链入口」之后模型唯一的视频处理入口：界面不再有直链
按钮，`video_attachment_note` 与四处代码注释都点名了这个技能名。因此这里钉住
四件事：

1. 技能真的存在于**真实**内置目录里（悬空引用=模型读不到 SKILL.md）；
2. `tier` 合法、description 含英文 token（摘要行要靠它被扫到）；
3. **没有声明 `requires`**——一旦声明 `requires.bins: [ffmpeg]`，
   `list_skills(filter_unavailable=True)` 会在没装 ffmpeg 的机器上**整个丢掉**
   这个技能，而「没有 ffmpeg 也能转写音频」正是它的核心降级路径；
4. 正文里的锚点（成本阶梯的每一级、`curl.exe` 的 Windows 陷阱）还在。
"""

from __future__ import annotations

import re
from pathlib import Path

from xianaibot.agent.skills import BUILTIN_SKILLS_DIR, SkillsLoader

_SKILL_NAME = "video-understanding"


def _loader(tmp_path: Path) -> SkillsLoader:
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    return SkillsLoader(workspace)


def _skill_md() -> Path:
    return BUILTIN_SKILLS_DIR / _SKILL_NAME / "SKILL.md"


def _body() -> str:
    return _skill_md().read_text(encoding="utf-8")


def test_skill_exists_in_real_builtin_dir():
    # 名字被写死在 websocket.py / helpers.py / 两处前端注释里，改名要同步改它们。
    assert _skill_md().is_file(), f"内置技能缺失：{_skill_md()}"


def test_discovered_with_valid_tier(tmp_path: Path):
    skills = {s["name"]: s for s in _loader(tmp_path).list_skills()}
    assert _SKILL_NAME in skills

    metadata = _loader(tmp_path).get_skill_metadata(_SKILL_NAME)
    assert metadata["tier"] == "user"


def test_description_carries_english_tokens():
    # 摘要行（build_skills_summary）与技能去重检查都按英文词切分，纯中文描述
    # 在英文查询下得 0 分。
    metadata = SkillsLoader(Path(".")).get_skill_metadata(_SKILL_NAME)
    description = metadata["description"]
    lowered = description.lower()
    for token in ("video", "ffprobe", "transcribe", "ffmpeg"):
        assert token in lowered, f"description 缺少英文锚点 {token!r}"

    # 摘要行必须有它——否则技能根本没进系统提示。
    assert re.search(rf"^- \*\*{_SKILL_NAME}\*\*", _loader(Path(".")).build_skills_summary(), re.M)


def test_declares_no_requires(tmp_path: Path):
    """声明 requires.bins 会让本技能在没装 ffmpeg 的机器上彻底消失。

    仓库既有惯例（`media-generation-craft` 等）也是不写 requires——依赖靠
    exec 现场软探测，缺了就走正文里的「诚实降级」。
    """
    capability = _loader(tmp_path).get_skill_capability(_SKILL_NAME)
    requirements = capability["requirements"]
    assert not requirements["bins"], f"不应声明 CLI 依赖：{requirements['bins']}"
    assert not requirements["env"], f"不应声明环境变量依赖：{requirements['env']}"
    assert not requirements["pkgs"], f"不应声明包依赖：{requirements['pkgs']}"


def test_body_keeps_cost_ladder_anchors():
    body = _body()
    for anchor in (
        "ffprobe",  # L0 免费探测
        "transcribe_media",  # L1 语音转写
        "ffmpeg",  # L1b 抽音轨 / L2 抽帧
        "curl.exe",  # L3 下载直链（Windows 上裸 curl 是 Invoke-WebRequest 别名）
        "max_upload_mb",  # 超限的判据来自这个配置项
    ):
        assert anchor in body, f"技能正文缺少锚点 {anchor!r}"


def test_body_forbids_whole_video_frame_scanning():
    """禁止整片逐帧是产品的红线（当初否掉视频理解就是因为这个成本）。"""
    body = _body()
    assert "逐帧" in body
    assert "禁止整片逐帧扫描" in body


def test_body_states_honest_degradation():
    body = _body()
    assert "降级" in body
    # 没有 ffmpeg / 没有 ASR Key / 平台页面链接三种缺失都要明说。
    assert "没有 ffmpeg" in body or "没装 ffmpeg" in body
    assert "yt-dlp" in body
    assert "假装" in body
