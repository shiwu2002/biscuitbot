"""Tests for GitStore — line_ages() and core git operations."""

import subprocess
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from xianaibot.utils.gitstore import GitStore


@pytest.fixture
def git(tmp_path):
    """Create an initialized GitStore with tracked MEMORY.md."""
    g = GitStore(tmp_path, tracked_files=["MEMORY.md", "SOUL.md"])
    g.init()
    return g


def _tree_paths(workspace) -> set[str]:
    """HEAD 提交里的文件路径集合——用来断言「到底提交了什么」。"""
    from dulwich.repo import Repo

    with Repo(str(workspace)) as repo:
        commit = repo[repo.refs[b"HEAD"]]
        paths: set[str] = set()

        def walk(tree, prefix: str = "") -> None:
            for name, _mode, sha in tree.iteritems():
                obj = repo[sha]
                rel = f"{prefix}{name.decode('utf-8')}"
                if obj.type_name == b"tree":
                    walk(obj, rel + "/")
                else:
                    paths.add(rel)

        walk(repo[commit.tree])
    return paths


@pytest.fixture
def dir_git(tmp_path):
    """跟踪 skills/ + .agent_tools/ 目录、并排除运行时计数的 GitStore。"""
    g = GitStore(
        tmp_path,
        tracked_files=["MEMORY.md"],
        tracked_dirs=["skills", ".agent_tools"],
        excluded_paths=[".agent_tools/usage_stats.json"],
    )
    g.init()
    return g


class TestTrackedDirs:
    """目录跟踪：技能/自定义工具由模型落盘，文件名事先不可知。"""

    def test_new_file_in_tracked_dir_is_committed(self, dir_git, tmp_path):
        (tmp_path / "skills" / "demo").mkdir(parents=True)
        (tmp_path / "skills" / "demo" / "SKILL.md").write_text("v1", encoding="utf-8")

        sha = dir_git.auto_commit("dream: 新增技能 demo")

        assert sha is not None
        assert "skills/demo/SKILL.md" in _tree_paths(tmp_path)

    def test_dir_change_without_index_change_is_detected(self, dir_git, tmp_path):
        """纯新增文件（其余跟踪项都没动）也必须触发提交。

        dulwich 的 ``status().untracked`` 会剪掉被 ``!dir/`` 重新包含的子目录，
        只靠它判断变更会漏掉这一整类改动。
        """
        (tmp_path / "skills").mkdir()
        (tmp_path / "skills" / "a.md").write_text("a", encoding="utf-8")
        assert dir_git.auto_commit("first") is not None

        (tmp_path / "skills" / "b.md").write_text("b", encoding="utf-8")
        assert dir_git.auto_commit("second") is not None
        assert {"skills/a.md", "skills/b.md"} <= _tree_paths(tmp_path)

    def test_excluded_file_is_never_committed(self, dir_git, tmp_path):
        (tmp_path / ".agent_tools").mkdir()
        (tmp_path / ".agent_tools" / "usage_stats.json").write_text("{}", encoding="utf-8")
        (tmp_path / ".agent_tools" / "manifest.json").write_text("{}", encoding="utf-8")

        assert dir_git.auto_commit("register tool") is not None

        tree = _tree_paths(tmp_path)
        assert ".agent_tools/manifest.json" in tree
        assert ".agent_tools/usage_stats.json" not in tree

    def test_excluded_change_alone_produces_no_commit(self, dir_git, tmp_path):
        """运行时计数每次工具调用都改写——不能因此刷出提交。"""
        (tmp_path / ".agent_tools").mkdir()
        stats = tmp_path / ".agent_tools" / "usage_stats.json"
        stats.write_text('{"a": 1}', encoding="utf-8")
        assert dir_git.auto_commit("noise") is None

        stats.write_text('{"a": 2}', encoding="utf-8")
        assert dir_git.auto_commit("noise again") is None

    def test_deleted_dir_file_is_recorded(self, dir_git, tmp_path):
        (tmp_path / "skills").mkdir()
        target = tmp_path / "skills" / "gone.md"
        target.write_text("x", encoding="utf-8")
        assert dir_git.auto_commit("add") is not None

        target.unlink()
        assert dir_git.auto_commit("remove") is not None
        assert "skills/gone.md" not in _tree_paths(tmp_path)

    def test_pycache_is_skipped(self, dir_git, tmp_path):
        """技能目录里会跑脚本，别把 __pycache__ 一起提交。"""
        (tmp_path / "skills" / "demo" / "__pycache__").mkdir(parents=True)
        (tmp_path / "skills" / "demo" / "__pycache__" / "x.pyc").write_bytes(b"\x00\x01")
        (tmp_path / "skills" / "demo" / "run.py").write_text("print(1)\n", encoding="utf-8")

        assert dir_git.auto_commit("skill with script") is not None

        tree = _tree_paths(tmp_path)
        assert "skills/demo/run.py" in tree
        assert not any("__pycache__" in p or p.endswith(".pyc") for p in tree)

    def test_revert_restores_dir_file(self, dir_git, tmp_path):
        skill = tmp_path / "skills" / "demo" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("v1", encoding="utf-8")
        dir_git.auto_commit("v1")

        skill.write_text("v2-broken", encoding="utf-8")
        broken_sha = dir_git.auto_commit("v2-broken")
        assert broken_sha is not None

        assert dir_git.revert(broken_sha) is not None
        assert skill.read_text(encoding="utf-8") == "v1"

    def test_revert_keeps_binary_dir_files_intact(self, dir_git, tmp_path):
        """技能目录里可能有 zip/图片，回滚必须按字节写回而不是 utf-8 解码。"""
        blob = b"\x89PNG\r\n\x1a\n\xff\xfe\x00\x01binary"
        asset = tmp_path / "skills" / "demo" / "logo.png"
        asset.parent.mkdir(parents=True)
        asset.write_bytes(blob)
        dir_git.auto_commit("add asset")

        asset.write_bytes(b"corrupted")
        bad_sha = dir_git.auto_commit("corrupt asset")
        assert bad_sha is not None

        assert dir_git.revert(bad_sha) is not None
        assert asset.read_bytes() == blob

    def test_revert_drops_dir_files_added_by_that_commit(self, dir_git, tmp_path):
        """回滚「Dream 新建了一个技能」必须真的把它删掉，不能是空操作。"""
        (tmp_path / "skills").mkdir()
        (tmp_path / "skills" / "old.md").write_text("old", encoding="utf-8")
        assert dir_git.auto_commit("only old skill") is not None

        new_skill = tmp_path / "skills" / "demo" / "SKILL.md"
        new_skill.parent.mkdir()
        new_skill.write_text("new", encoding="utf-8")
        added_sha = dir_git.auto_commit("add new skill")
        assert added_sha is not None

        assert dir_git.revert(added_sha) is not None
        assert not new_skill.exists()
        assert not new_skill.parent.exists()  # 变空的子目录一并清掉
        assert (tmp_path / "skills" / "old.md").exists()

    def test_revert_keeps_never_committed_files(self, dir_git, tmp_path):
        """从未提交过的文件不属于任何提交，回滚不该动它。"""
        (tmp_path / "skills").mkdir()
        (tmp_path / "skills" / "tracked.md").write_text("t", encoding="utf-8")
        first_sha = dir_git.auto_commit("tracked skill")
        assert first_sha is not None

        (tmp_path / "skills" / "tracked.md").write_text("t2", encoding="utf-8")
        broken_sha = dir_git.auto_commit("modify tracked")
        assert broken_sha is not None
        (tmp_path / "skills" / "uncommitted.md").write_text("wip", encoding="utf-8")

        assert dir_git.revert(broken_sha) is not None
        assert (tmp_path / "skills" / "tracked.md").read_text(encoding="utf-8") == "t"
        assert (tmp_path / "skills" / "uncommitted.md").exists()

    def test_init_refreshes_gitignore_for_existing_repo(self, tmp_path):
        """旧工作区升级：仓库已存在时 init() 也要补齐新增跟踪项。"""
        GitStore(tmp_path, tracked_files=["MEMORY.md"]).init()

        g = GitStore(
            tmp_path,
            tracked_files=["MEMORY.md"],
            tracked_dirs=["skills"],
            excluded_paths=[".agent_tools/usage_stats.json"],
        )
        assert g.init() is False  # 仓库已存在，不重建

        gitignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
        assert "!skills/" in gitignore
        assert ".agent_tools/usage_stats.json" in gitignore
        # 放行项在排除项之前，排除项才生效（gitignore 以最后一条匹配为准）
        lines = gitignore.splitlines()
        assert lines.index("!skills/") < lines.index(".agent_tools/usage_stats.json")

    def test_gitignore_refresh_is_idempotent(self, tmp_path):
        g = GitStore(tmp_path, tracked_files=["MEMORY.md"], tracked_dirs=["skills"])
        g.init()
        g.init()
        lines = (tmp_path / ".gitignore").read_text(encoding="utf-8").splitlines()
        assert lines.count("!skills/") == 1


class TestLineAges:
    def test_returns_empty_when_not_initialized(self, tmp_path):
        """line_ages should return [] if the git repo is not initialized."""
        git = GitStore(tmp_path, tracked_files=["MEMORY.md"])
        assert git.line_ages("MEMORY.md") == []

    def test_returns_empty_for_missing_file(self, git):
        """line_ages should return [] for a file that doesn't exist."""
        assert git.line_ages("SOUL.md") == []

    def test_returns_empty_for_empty_file(self, git, tmp_path):
        """line_ages should return [] for an empty tracked file."""
        (tmp_path / "SOUL.md").write_text("", encoding="utf-8")
        git.auto_commit("empty soul")
        assert git.line_ages("SOUL.md") == []

    def test_one_age_per_line(self, git, tmp_path):
        """line_ages should return one entry per line in the file."""
        content = "# Memory\n\n## Section A\n- item 1\n"
        (tmp_path / "MEMORY.md").write_text(content, encoding="utf-8")
        git.auto_commit("initial")
        ages = git.line_ages("MEMORY.md")
        assert len(ages) == len(content.splitlines())

    def test_fresh_lines_have_age_zero(self, git, tmp_path):
        """Lines committed today should have age_days=0."""
        (tmp_path / "MEMORY.md").write_text("## A\n- x\n", encoding="utf-8")
        git.auto_commit("initial")
        ages = git.line_ages("MEMORY.md")
        assert all(a.age_days == 0 for a in ages)

    def test_age_differentiates_across_days(self, git, tmp_path):
        """Lines committed today should show correct age when 'now' is mocked forward."""
        (tmp_path / "MEMORY.md").write_text("## A\n- x\n", encoding="utf-8")
        git.auto_commit("initial")

        future_now = datetime.now(tz=timezone.utc) + timedelta(days=30)
        with patch("xianaibot.utils.gitstore.datetime") as mock_dt:
            mock_dt.now.return_value = future_now
            mock_dt.fromtimestamp = datetime.fromtimestamp
            ages = git.line_ages("MEMORY.md")

        assert len(ages) == 2
        assert all(a.age_days == 30 for a in ages)

    def test_annotate_failure_returns_empty(self, tmp_path):
        """If annotate fails, line_ages should return [] gracefully."""
        git = GitStore(tmp_path, tracked_files=["MEMORY.md"])
        # Don't init — annotate will fail
        assert git.line_ages("MEMORY.md") == []

    def test_partial_edit_only_updates_changed_lines(self, git, tmp_path):
        """Only modified lines should reflect the new commit's timestamp."""
        now = datetime(2026, 5, 1, tzinfo=timezone.utc)
        old = now - timedelta(days=30)

        (tmp_path / "MEMORY.md").write_text(
            "# Memory\n\n## A\n- old\n\n## B\n- keep\n", encoding="utf-8"
        )
        with patch("dulwich.worktree.time.time", return_value=old.timestamp()):
            git.auto_commit("commit1")

        # Only modify section A
        (tmp_path / "MEMORY.md").write_text(
            "# Memory\n\n## A\n- new\n\n## B\n- keep\n", encoding="utf-8"
        )
        with patch("dulwich.worktree.time.time", return_value=now.timestamp()):
            git.auto_commit("commit2")

        with patch("xianaibot.utils.gitstore.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.fromtimestamp = datetime.fromtimestamp
            ages = git.line_ages("MEMORY.md")

        lines = (tmp_path / "MEMORY.md").read_text(encoding="utf-8").splitlines()
        assert len(ages) == len(lines)
        age_by_line = {line: age.age_days for line, age in zip(lines, ages, strict=True)}
        assert age_by_line["- new"] == 0
        assert age_by_line["- keep"] == 30


class TestNestedRepoProtection:
    """Regression tests for GitHub issue #2980: nested repo protection."""

    def test_init_refuses_inside_git_repo(self, tmp_path):
        """init() should detect it's inside an existing git repo and refuse."""
        project = tmp_path / "project"
        project.mkdir()
        (project / ".git").mkdir()

        workspace = project / "workspace"
        workspace.mkdir()

        g = GitStore(workspace, tracked_files=["MEMORY.md"])
        result = g.init()

        assert result is False
        assert not (workspace / ".git").is_dir()

    def test_init_preserves_existing_gitignore(self, tmp_path):
        """init() should preserve existing .gitignore entries and append new ones."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        existing = "*.pyc\n__pycache__/\n"
        (workspace / ".gitignore").write_text(existing, encoding="utf-8")

        g = GitStore(workspace, tracked_files=["MEMORY.md"])
        result = g.init()

        assert result is True
        gitignore = (workspace / ".gitignore").read_text(encoding="utf-8")
        assert "*.pyc" in gitignore
        assert "__pycache__/" in gitignore
        assert "!MEMORY.md" in gitignore
        assert "!.gitignore" in gitignore

    def test_init_no_gitignore_creates_new(self, tmp_path):
        """init() should create .gitignore with Dream content when none exists."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        g = GitStore(workspace, tracked_files=["MEMORY.md"])
        result = g.init()

        assert result is True
        gitignore = (workspace / ".gitignore").read_text(encoding="utf-8")
        expected = g._build_gitignore()
        assert gitignore == expected

    def test_init_gitignore_merge_idempotent(self, tmp_path):
        """init() should not duplicate Dream entries already in .gitignore."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        # Pre-existing .gitignore that already has some Dream entries
        existing = "*.pyc\n/*\n!MEMORY.md\n"
        (workspace / ".gitignore").write_text(existing, encoding="utf-8")

        g = GitStore(workspace, tracked_files=["MEMORY.md"])
        result = g.init()

        assert result is True
        gitignore = (workspace / ".gitignore").read_text(encoding="utf-8")
        # No duplicate lines
        lines = gitignore.splitlines()
        assert lines.count("/*") == 1
        assert lines.count("!MEMORY.md") == 1
        # Existing entry preserved, new Dream entries appended
        assert "*.pyc" in gitignore
        assert "!.gitignore" in gitignore

    def test_init_outside_git_repo_works_normally(self, tmp_path):
        """init() should succeed and create .git when not inside a git repo."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        g = GitStore(workspace, tracked_files=["MEMORY.md"])
        result = g.init()

        assert result is True
        assert (workspace / ".git").is_dir()

    def test_init_refuses_inside_git_worktree(self, tmp_path):
        """init() should refuse when the parent checkout is a git worktree."""
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "README.md").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "-c",
                "user.name=test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-q",
                "-m",
                "init",
            ],
            check=True,
        )
        subprocess.run(["git", "-C", str(repo), "branch", "wt-branch"], check=True)

        worktree = tmp_path / "worktree"
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "add", "-q", str(worktree), "wt-branch"],
            check=True,
        )
        assert (worktree / ".git").is_file()

        workspace = worktree / "workspace"
        workspace.mkdir()

        g = GitStore(workspace, tracked_files=["MEMORY.md"])
        result = g.init()

        assert result is False
        assert not (workspace / ".git").exists()
