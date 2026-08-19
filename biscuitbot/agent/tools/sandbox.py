"""用于 shell 命令执行的沙箱后端。

所属模块与项目作用
===================
本文件位于 biscuitbot/agent/tools 目录，是工具系统中 shell/exec 工具的
沙箱包装层。它将原始 shell 命令包装为在沙箱中执行的命令字符串，限制文件
系统访问范围，保护宿主环境安全。

添加新后端的方法：实现签名如下的函数：
    _wrap_<name>(command: str, workspace: str, cwd: str) -> str
并在下方的 _BACKENDS 中注册。
"""

import shlex  # shell 词法转义，用于安全拼接命令
from pathlib import Path  # 路径处理

from biscuitbot.config.paths import get_media_dir  # 媒体目录路径


def _bwrap(command: str, workspace: str, cwd: str) -> str:
    """将命令包装到 bubblewrap 沙箱中（容器内需有 bwrap）。

    仅工作区以读写方式 bind 挂载；其父目录（含 config.json）被新 tmpfs
    遮挡。媒体目录以只读方式 bind 挂载，使 exec 命令能读取上传的附件。
    """
    ws = Path(workspace).resolve()
    media = get_media_dir().resolve()

    # 将 cwd 解析为相对工作区的路径，越界时回退到工作区根
    try:
        sandbox_cwd = str(ws / Path(cwd).resolve().relative_to(ws))
    except ValueError:
        sandbox_cwd = str(ws)

    # 必需的只读挂载（运行时必需）
    required = ["/usr"]
    # 可选的只读挂载（存在才挂载）
    optional = [
        "/bin",
        "/lib",
        "/lib64",
        "/etc/alternatives",
        "/etc/ssl/certs",
        "/etc/resolv.conf",
        "/etc/ld.so.cache",
    ]

    args = ["bwrap", "--new-session", "--die-with-parent", "--setenv", "HOME", str(ws)]
    # 必需路径强制只读挂载
    for p in required:
        args += ["--ro-bind", p, p]
    # 可选路径尝试只读挂载（不存在不报错）
    for p in optional:
        args += ["--ro-bind-try", p, p]
    args += [
        "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
        "--tmpfs", str(ws.parent),        # 用 tmpfs 遮挡配置目录
        "--dir", str(ws),                 # 重建工作区挂载点
        "--bind", str(ws), str(ws),       # 工作区读写挂载
        "--ro-bind-try", str(media), str(media),  # 媒体目录只读访问
        "--chdir", sandbox_cwd,           # 设置工作目录
        "--", "sh", "-c", command,        # 实际执行的命令
    ]
    return shlex.join(args)


_BACKENDS = {"bwrap": _bwrap}  # 沙箱后端注册表：名称 -> 包装函数


def wrap_command(sandbox: str, command: str, workspace: str, cwd: str) -> str:
    """使用指定沙箱后端包装 *command*。

    参数:
        sandbox: 沙箱后端名称（如 "bwrap"）。
        command: 原始 shell 命令。
        workspace: 工作区路径。
        cwd: 当前工作目录。

    返回:
        包装后的命令字符串。

    抛出:
        ValueError: 未知沙箱后端时抛出。
    """
    if backend := _BACKENDS.get(sandbox):
        return backend(command, workspace, cwd)
    raise ValueError(f"未知沙箱后端 {sandbox!r}，可用：{list(_BACKENDS)}")
