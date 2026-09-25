"""Git-backed version control for memory files, using dulwich."""

from __future__ import annotations

import io
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

# 工作区记忆仓库的跟踪范围。模型/Dream 会直接改写这些文件，而它们有两个不同的
# 建仓入口（``MemoryStore`` 与 ``helpers.init_workspace``），跟踪范围必须在两处
# 完全一致，否则「谁先建仓」会决定哪些文件进版本库。
MEMORY_TRACKED_FILES = ("SOUL.md", "USER.md", "memory/MEMORY.md")

# 整目录跟踪：技能与自定义工具的**文件名事先不可知**，只能按目录收集；
# 但下面两个运行时计数文件每次工具调用都会改写，纳入版本库只会制造噪声
# （回滚时还会复活过期的调用统计），因此显式排除。
MEMORY_TRACKED_DIRS = ("skills", ".agent_tools")
MEMORY_EXCLUDED_PATHS = (
    ".agent_tools/usage_stats.json",
    ".agent_tools/cold_storage.json",
)


@dataclass
class CommitInfo:
    sha: str  # Short SHA (8 chars)
    message: str
    timestamp: str  # Formatted datetime

    def format(self, diff: str = "") -> str:
        """Format this commit for display, optionally with a diff."""
        header = f"## {self.message.splitlines()[0]}\n`{self.sha}` — {self.timestamp}\n"
        if diff:
            return f"{header}\n```diff\n{diff}\n```"
        return f"{header}\n(no file changes)"


@dataclass
class LineAge:
    """Age of a single line based on git blame."""

    age_days: int  # days since last modification


def _compute_line_ages(annotated) -> list[LineAge]:
    """Convert annotate results to per-line ages."""
    now = datetime.now(tz=timezone.utc).date()
    ages: list[LineAge] = []
    for (commit, _tree_entry), _line_bytes in annotated:
        dt = datetime.fromtimestamp(commit.commit_time, tz=timezone.utc).date()
        ages.append(LineAge(age_days=(now - dt).days))
    return ages


class GitStore:
    """Git-backed version control for memory files.

    跟踪范围由三部分构成：

    - ``tracked_files``：精确文件（``SOUL.md`` / ``USER.md`` / ``memory/MEMORY.md``）；
    - ``tracked_dirs``：整目录（``skills`` / ``.agent_tools``）——技能与自定义工具由
      模型/Dream 直接落盘，文件名事先不可知，只有整目录跟踪才能让它们的新增、
      修改、删除都进版本库，也才谈得上用 ``revert`` 回滚；
    - ``excluded_paths``：``tracked_dirs`` 下仍然**不跟踪**的运行时状态文件
      （``.agent_tools/usage_stats.json``、``cold_storage.json``）。它们每次工具
      调用都会改写，纳入版本库只会把提交日志淹没，回滚时还会复活过期的调用统计。

    目录跟踪对「已存在的工作区」也生效：``init()`` 在仓库已初始化时会补齐
    ``.gitignore`` 里缺失的条目，所以升级后无需手动迁移。
    """

    # 目录跟踪时始终跳过的构建产物（技能目录里放了可执行脚本时会出现）
    _SKIP_DIR_NAMES = {"__pycache__"}
    _SKIP_FILE_SUFFIXES = (".pyc", ".pyo")

    def __init__(
        self,
        workspace: Path,
        tracked_files: Sequence[str],
        tracked_dirs: Sequence[str] | None = None,
        excluded_paths: Sequence[str] | None = None,
    ):
        self._workspace = workspace
        self._tracked_files = tuple(tracked_files)
        self._tracked_dirs = tuple(
            d.strip("/") for d in (tracked_dirs or ()) if d.strip("/")
        )
        self._excluded_paths = set(excluded_paths or ())

    def is_initialized(self) -> bool:
        """Check if the git repo has been initialized."""
        return (self._workspace / ".git").is_dir()

    # -- init ------------------------------------------------------------------

    def init(self) -> bool:
        """Initialize a git repo if not already initialized.

        Creates .gitignore and makes an initial commit.
        Returns True if a new repo was created, False if already exists.
        """
        if self.is_initialized():
            # 已存在的仓库：只补齐 .gitignore 里缺失的条目。旧工作区升级
            # 到「目录跟踪」后，靠这一句让 skills/ 等目录立刻生效。
            self._write_gitignore()
            return False

        if self._is_inside_git_repo():
            logger.warning(
                "Workspace {} is already inside a git repo; "
                "skipping nested repo initialization",
                self._workspace,
            )
            return False

        try:
            from dulwich import porcelain

            porcelain.init(str(self._workspace))

            self._write_gitignore()

            # Ensure tracked files exist (touch them if missing) so the initial
            # commit has something to track. Tracked dirs are not created here:
            # an empty skills/ that never had content is not worth inventing.
            for rel in self._tracked_files:
                p = self._workspace / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                if not p.exists():
                    p.write_text("", encoding="utf-8")

            # Initial commit
            porcelain.add(
                str(self._workspace),
                paths=[".gitignore"] + self._staging_paths(),
            )
            porcelain.commit(
                str(self._workspace),
                message=b"init: xianaibot memory store",
                author=b"xianaibot <xianaibot@dream>",
                committer=b"xianaibot <xianaibot@dream>",
            )
            logger.info("Git store initialized at {}", self._workspace)
            return True
        except Exception:
            logger.exception("Git store init failed for {}", self._workspace)
            return False

    # -- daily operations ------------------------------------------------------

    def auto_commit(self, message: str) -> str | None:
        """Stage tracked memory files and commit if there are changes.

        Returns the short commit SHA, or None if nothing to commit.
        """
        if not self.is_initialized():
            return None

        try:
            from dulwich import porcelain

            # .gitignore excludes everything except tracked files,
            # so any staged/unstaged change must be in our files.
            st = porcelain.status(str(self._workspace))
            if (
                not st.unstaged
                and not any(st.staged.values())
                and not self._has_dir_changes()
            ):
                return None

            msg_bytes = message.encode("utf-8") if isinstance(message, str) else message
            porcelain.add(str(self._workspace), paths=self._staging_paths())
            sha_bytes = porcelain.commit(
                str(self._workspace),
                message=msg_bytes,
                author=b"xianaibot <xianaibot@dream>",
                committer=b"xianaibot <xianaibot@dream>",
            )
            if sha_bytes is None:
                return None
            sha = sha_bytes.hex()[:8]
            logger.debug("Git auto-commit: {} ({})", sha, message)
            return sha
        except Exception:
            logger.exception("Git auto-commit failed: {}", message)
            return None

    # -- internal helpers ------------------------------------------------------

    def _resolve_sha(self, short_sha: str) -> bytes | None:
        """Resolve a short SHA prefix to the full SHA bytes."""
        try:
            from dulwich.repo import Repo

            with Repo(str(self._workspace)) as repo:
                try:
                    sha = repo.refs[b"HEAD"]
                except KeyError:
                    return None

                while sha:
                    if sha.hex().startswith(short_sha):
                        return sha
                    commit = repo[sha]
                    if commit.type_name != b"commit":
                        break
                    sha = commit.parents[0] if commit.parents else None
            return None
        except Exception:
            logger.debug("gitstore: failed to read commit history", exc_info=True)
            return None

    def _is_inside_git_repo(self) -> bool:
        """Check if self._workspace is already inside a git repository.

        Walks up from self._workspace to the filesystem root, returning True
        if any parent directory contains a .git entry.

        Git worktrees and submodules can use a ``.git`` file instead of a
        directory, so we must treat either form as "already inside a repo".
        """
        current = self._workspace.resolve()
        while current != current.parent:
            if (current / ".git").exists():
                return True
            current = current.parent
        return False

    def _build_gitignore(self) -> str:
        """Generate .gitignore content from tracked files and dirs."""
        dirs: set[str] = set()
        for f in list(self._tracked_files) + list(self._tracked_dirs):
            parent = Path(f).parent
            # 逐级回填祖先目录：被 ``/*`` 排除的目录 git 不会进入，
            # 所以 ``a/b`` 这类嵌套跟踪项必须把 ``a/`` 也放行。
            while str(parent) != ".":
                dirs.add(str(parent).replace("\\", "/"))
                parent = parent.parent
        lines = ["/*"]
        for d in sorted(dirs):
            lines.append(f"!{d}/")
        for d in sorted(self._tracked_dirs):
            lines.append(f"!{d}/")
        for f in self._tracked_files:
            lines.append(f"!{f}")
        # 排除项必须排在放行项之后：gitignore 以最后一条匹配的规则为准。
        for e in sorted(self._excluded_paths):
            lines.append(e)
        lines.append("!.gitignore")
        return "\n".join(lines) + "\n"

    def _write_gitignore(self) -> None:
        """把 ``.gitignore`` 写入/补齐到当前跟踪范围（保留用户已有条目）。"""
        gitignore = self._workspace / ".gitignore"
        entries = self._build_gitignore()
        if not gitignore.exists():
            gitignore.write_text(entries, encoding="utf-8")
            return
        try:
            existing = gitignore.read_text(encoding="utf-8")
        except OSError:
            logger.exception("gitstore: 读取 .gitignore 失败：{}", gitignore)
            return
        existing_lines = set(existing.splitlines())
        new_lines = [line for line in entries.splitlines() if line not in existing_lines]
        if new_lines:
            merged = existing.rstrip("\n") + "\n" + "\n".join(new_lines) + "\n"
            gitignore.write_text(merged, encoding="utf-8")

    def _is_excluded(self, relpath: str) -> bool:
        """判断跟踪目录下的某个文件是否被显式排除。"""
        return relpath in self._excluded_paths

    def _dir_files(self) -> list[str]:
        """列出跟踪目录下现有文件的仓库内相对路径（正斜杠）。"""
        out: list[str] = []
        for d in self._tracked_dirs:
            base = self._workspace / d
            if not base.is_dir():
                continue
            for path in base.rglob("*"):
                if not path.is_file():
                    continue
                if any(part in self._SKIP_DIR_NAMES for part in path.parts):
                    continue
                if path.suffix in self._SKIP_FILE_SUFFIXES:
                    continue
                rel = path.relative_to(self._workspace).as_posix()
                if not self._is_excluded(rel):
                    out.append(rel)
        return out

    def _indexed_dir_paths(self) -> set[str]:
        """索引里属于跟踪目录的路径（含已在磁盘上被删除的文件）。"""
        if not self._tracked_dirs or not self.is_initialized():
            return set()
        try:
            from dulwich.repo import Repo

            with Repo(str(self._workspace)) as repo:
                names = {
                    path.decode("utf-8", errors="replace")
                    for path in repo.open_index()
                }
        except Exception:
            logger.debug("gitstore: 读取索引失败", exc_info=True)
            return set()
        prefixes = tuple(f"{d}/" for d in self._tracked_dirs)
        return {
            n for n in names
            if n.startswith(prefixes) and not self._is_excluded(n)
        }

    def _staging_paths(self) -> list[str]:
        """本次提交要暂存的路径：跟踪文件 + 跟踪目录下现有/已跟踪的文件。

        已跟踪但磁盘上已消失的文件依旧要传进去——dulwich 会把「路径不存在」
        当作一次删除来暂存，这正是删除技能文件时我们希望记录的内容。
        """
        paths = list(self._tracked_files)
        seen = set(paths)
        for rel in sorted(set(self._dir_files()) | self._indexed_dir_paths()):
            if rel not in seen:
                seen.add(rel)
                paths.append(rel)
        return paths

    def _has_dir_changes(self) -> bool:
        """跟踪目录里是否有新文件。

        dulwich 的 ``untracked`` 会剪掉被 ``!dir/`` 重新包含的子目录（只报根级
        条目），所以不能靠它判断「目录里多了文件」，得自己跟索引对一遍。
        """
        if not self._tracked_dirs or not self.is_initialized():
            return False
        known = self._indexed_dir_paths()
        return any(rel not in known for rel in self._dir_files())

    def _drop_dir_files_absent_from(self, target_paths: dict[str, bytes]) -> list[str]:
        """删除目标状态里不存在的已跟踪目录文件（回滚时的「对齐」步骤）。

        只看索引里的文件：从未提交过的文件不动，免得回滚顺手删掉模型刚写好、
        还没来得及提交的东西。
        """
        removed: list[str] = []
        for relpath in sorted(self._indexed_dir_paths()):
            if relpath in target_paths:
                continue
            path = self._workspace / relpath
            try:
                if not path.is_file():
                    continue
                path.unlink()
            except OSError:
                logger.warning("gitstore: 回滚时删除 {} 失败", relpath)
                continue
            removed.append(relpath)
            self._prune_empty_parents(path.parent)
        return removed

    def _prune_empty_parents(self, directory: Path) -> None:
        """自下而上删掉因回滚而变空的目录，止步于工作区根目录。"""
        while directory != self._workspace and self._is_within_tracked_dir(directory):
            try:
                if any(directory.iterdir()):
                    return
                directory.rmdir()
            except OSError:
                return
            directory = directory.parent

    def _is_within_tracked_dir(self, directory: Path) -> bool:
        """判断目录是否位于某个跟踪目录之内（避免误删工作区的其他目录）。"""
        try:
            rel = directory.relative_to(self._workspace).as_posix()
        except ValueError:
            return False
        return any(rel == d or rel.startswith(f"{d}/") for d in self._tracked_dirs)

    # -- query -----------------------------------------------------------------

    def log(self, max_entries: int = 20) -> list[CommitInfo]:
        """Return simplified commit log."""
        if not self.is_initialized():
            return []

        try:
            from dulwich.repo import Repo

            entries: list[CommitInfo] = []
            with Repo(str(self._workspace)) as repo:
                try:
                    head = repo.refs[b"HEAD"]
                except KeyError:
                    return []

                sha = head
                while sha and len(entries) < max_entries:
                    commit = repo[sha]
                    if commit.type_name != b"commit":
                        break
                    ts = time.strftime(
                        "%Y-%m-%d %H:%M",
                        time.localtime(commit.commit_time),
                    )
                    msg = commit.message.decode("utf-8", errors="replace").strip()
                    entries.append(CommitInfo(
                        sha=sha.hex()[:8],
                        message=msg,
                        timestamp=ts,
                    ))
                    sha = commit.parents[0] if commit.parents else None

            return entries
        except Exception:
            logger.exception("Git log failed")
            return []

    def line_ages(self, file_path: str) -> list[LineAge]:
        """Compute the age of each line in a tracked file via git blame.

        Returns one LineAge per line, in order.
        Returns an empty list if the repo is not initialized, the file is
        empty, or annotation fails.
        """

        if not self.is_initialized():
            return []

        target = self._workspace / file_path
        if not target.exists() or target.stat().st_size == 0:
            return []

        try:
            from dulwich import porcelain

            annotated = porcelain.annotate(str(self._workspace), file_path)
        except Exception:
            logger.exception("Git line_ages annotate failed for {}", file_path)
            return []

        if not annotated:
            return []

        return _compute_line_ages(annotated)

    def diff_commits(self, sha1: str, sha2: str) -> str:
        """Show diff between two commits."""
        if not self.is_initialized():
            return ""

        try:
            from dulwich import porcelain

            full1 = self._resolve_sha(sha1)
            full2 = self._resolve_sha(sha2)
            if not full1 or not full2:
                return ""

            out = io.BytesIO()
            porcelain.diff(
                str(self._workspace),
                commit=full1,
                commit2=full2,
                outstream=out,
            )
            return out.getvalue().decode("utf-8", errors="replace")
        except Exception:
            logger.exception("Git diff_commits failed")
            return ""

    def find_commit(self, short_sha: str, max_entries: int = 20) -> CommitInfo | None:
        """Find a commit by short SHA prefix match."""
        for c in self.log(max_entries=max_entries):
            if c.sha.startswith(short_sha):
                return c
        return None

    def show_commit_diff(self, short_sha: str, max_entries: int = 20) -> tuple[CommitInfo, str] | None:
        """Find a commit and return it with its diff vs the parent."""
        commits = self.log(max_entries=max_entries)
        for i, c in enumerate(commits):
            if c.sha.startswith(short_sha):
                if i + 1 < len(commits):
                    diff = self.diff_commits(commits[i + 1].sha, c.sha)
                else:
                    diff = ""
                return c, diff
        return None

    # -- restore ---------------------------------------------------------------

    def revert(self, commit: str) -> str | None:
        """Revert (undo) the changes introduced by the given commit.

        Restores all tracked memory files to the state at the commit's parent,
        then creates a new commit recording the revert.

        跟踪目录（``skills`` 等）同样对齐到该提交的父提交：写回其中的文件
        （按字节，技能目录里可能有 zip/图片），并删除「当时还不存在、后来才被
        跟踪」的文件——不然「回滚掉 Dream 新建的那个技能」会变成空操作。
        未进过索引的文件不动：只回滚已提交的内容。

        Returns the new commit SHA, or None on failure.
        """
        if not self.is_initialized():
            return None

        try:
            from dulwich.repo import Repo

            full_sha = self._resolve_sha(commit)
            if not full_sha:
                logger.warning("Git revert: SHA not found: {}", commit)
                return None

            with Repo(str(self._workspace)) as repo:
                commit_obj = repo[full_sha]
                if commit_obj.type_name != b"commit":
                    return None

                if not commit_obj.parents:
                    logger.warning("Git revert: cannot revert root commit {}", commit)
                    return None

                # Use the parent's tree — this undoes the commit's changes
                parent_obj = repo[commit_obj.parents[0]]
                tree = repo[parent_obj.tree]

                restored: list[str] = []
                for filepath in self._tracked_files:
                    content = self._read_blob_from_tree(repo, tree, filepath)
                    if content is not None:
                        dest = self._workspace / filepath
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.write_text(content, encoding="utf-8")
                        restored.append(filepath)

                target_dir_files = dict(self._iter_dir_blobs(repo, tree))
                for relpath, raw in target_dir_files.items():
                    dest = self._workspace / relpath
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(raw)
                    restored.append(relpath)

            # 那次提交之后才进入跟踪的目录文件，在目标状态里并不存在——
            # 回滚必须把它们删掉，否则「撤销新建技能」是个空操作。
            removed = self._drop_dir_files_absent_from(target_dir_files)

            if not restored and not removed:
                return None

            # Commit the restored state
            msg = f"revert: undo {commit}"
            return self.auto_commit(msg)
        except Exception:
            logger.exception("Git revert failed for {}", commit)
            return None

    @staticmethod
    def _read_blob_from_tree(repo, tree, filepath: str) -> str | None:
        """Read a blob's content from a tree object by walking path parts."""
        parts = Path(filepath).parts
        current = tree
        for part in parts:
            try:
                entry = current[part.encode()]
            except KeyError:
                return None
            obj = repo[entry[1]]
            if obj.type_name == b"blob":
                return obj.data.decode("utf-8", errors="replace")
            if obj.type_name == b"tree":
                current = obj
            else:
                return None
        return None

    def _iter_dir_blobs(self, repo, tree) -> list[tuple[str, bytes]]:
        """列出跟踪目录在目标提交里的全部文件（路径 + 原始字节）。

        返回字节而非文本：技能目录里可能有 zip、图片等二进制资源，用
        ``errors="replace"`` 解码会静默损坏它们。
        """
        out: list[tuple[str, bytes]] = []
        for d in self._tracked_dirs:
            subtree = self._lookup_tree(repo, tree, d)
            if subtree is None:
                continue
            out.extend(
                (rel, raw)
                for rel, raw in self._iter_tree_blobs(repo, subtree, d)
                if not self._is_excluded(rel)
            )
        return out

    @staticmethod
    def _lookup_tree(repo, tree, dirpath: str):
        """按路径逐级下钻，返回子树对象；路径不存在或不是目录时返回 None。"""
        current = tree
        for part in Path(dirpath).parts:
            try:
                entry = current[part.encode()]
            except KeyError:
                return None
            obj = repo[entry[1]]
            if obj.type_name != b"tree":
                return None
            current = obj
        return current

    @staticmethod
    def _iter_tree_blobs(repo, tree, prefix: str) -> list[tuple[str, bytes]]:
        """递归展开子树，产出 ``(仓库内路径, 内容字节)``。"""
        out: list[tuple[str, bytes]] = []
        for name, _mode, sha in tree.iteritems():
            obj = repo[sha]
            rel = f"{prefix}/{name.decode('utf-8', errors='replace')}"
            if obj.type_name == b"tree":
                out.extend(GitStore._iter_tree_blobs(repo, obj, rel))
            elif obj.type_name == b"blob":
                out.append((rel, obj.data))
        return out
