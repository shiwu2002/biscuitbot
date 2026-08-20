#!/usr/bin/env python3
"""本地群聊消息中枢（纯标准库，单文件，零依赖）。

用途
====
给「多智能体 / 多进程 / 多用户」提供一个本地的群聊消息总线：
用户（agent / 脚本 / 人）先注册，然后通过 HTTP 端口查询有哪些用户、
给某个用户发私聊（DM）、或向所有人广播。每个注册用户有一个独立收件箱，
用拉取方式（GET /inbox/<user>?since=<seq>）增量取消息——不需要挂长连接，
最适合 agent / 脚本轮询。

运行
====
    python3 scripts/groupchat.py                      # 127.0.0.1:18600
    python3 scripts/groupchat.py --port 19000 --token mysecret
    python3 scripts/groupchat.py --state-dir /tmp/gc  # 换持久化目录

curl 示例
=========
    # 1. 注册（幂等：同名重复注册只刷新心跳）
    curl -s -X POST 127.0.0.1:18600/register -d '{"name":"alice"}'

    # 2. 查看已注册用户
    curl -s 127.0.0.1:18600/users

    # 3. 私聊 alice → bob
    curl -s -X POST 127.0.0.1:18600/dm -d '{"from":"alice","to":"bob","content":"hi bob"}'

    # 4. 广播（发给除发送者外的所有用户）
    curl -s -X POST 127.0.0.1:18600/broadcast -d '{"from":"alice","content":"大家好"}'

    # 5. 统一发送（to="all" 广播，否则按名字私聊）
    curl -s -X POST 127.0.0.1:18600/send -d '{"from":"bob","to":"all","content":"..."}'

    # 6. 拉取 bob 的收件箱（since=上次返回的最大 seq，增量）
    curl -s "127.0.0.1:18600/inbox/bob?since=0"

    # 7. 群聊记录（广播 + 加入事件，用于历史回溯）
    curl -s "127.0.0.1:18600/room?since=0"

    # 8. 健康检查
    curl -s 127.0.0.1:18600/health

持久化
======
    ~/.biscuitbot/groupchat/users.jsonl     用户注册（append-only）
    ~/.biscuitbot/groupchat/messages.jsonl  全部消息（append-only，含 DM 审计）
    重启时回放重建收件箱与全局 seq；每个用户收件箱最多保留 500 条。

安全
====
    默认仅绑定 127.0.0.1。如需暴露到内网，用 --host 0.0.0.0 并建议加 --token
    （设置后所有请求需带请求头 Authorization: Bearer <token>）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18600
INBOX_CAP = 500        # 每个用户收件箱最多保留条数
ONLINE_WINDOW_S = 300  # last_seen 在多少秒内视为「在线」
MAX_NAME_LEN = 64
MAX_CONTENT_LEN = 20000


# ---------------------------------------------------------------------------
# 状态仓库（内存镜像 + JSONL 落盘）
# ---------------------------------------------------------------------------

class GroupChatStore:
    """用户注册 + 消息总线的内存镜像，append-only JSONL 持久化，线程安全。"""

    def __init__(self, state_dir: Path) -> None:
        self._dir = state_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._users_path = self._dir / "users.jsonl"
        self._messages_path = self._dir / "messages.jsonl"

        # RLock：register/_emit、inbox/touch 等会在持锁状态下再次进入
        # 内部方法，非可重入的 Lock 会自死锁（同一线程二次获取即挂起）。
        self._lock = threading.RLock()
        self._users: dict[str, dict[str, Any]] = {}   # name -> {registered_at, last_seen}
        self._seq = 0
        self._room: list[dict[str, Any]] = []         # 广播 + 加入事件（时间序）
        self._inboxes: dict[str, list[dict[str, Any]]] = {}  # name -> 消息列表
        self._replay()

    # ---- 落盘 ----

    def _append(self, path: Path, record: dict[str, Any]) -> None:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def _replay(self) -> None:
        """从 JSONL 恢复内存状态（用户注册 + 全部消息 + seq）。"""
        users: dict[str, dict[str, Any]] = {}
        if self._users_path.exists():
            for line in self._users_path.read_text(encoding="utf-8").splitlines():
                try:
                    rec = json.loads(line)
                except (ValueError, TypeError):
                    continue
                name = rec.get("name")
                if isinstance(name, str) and name:
                    users[name] = {
                        "registered_at": rec.get("registered_at", ""),
                        "last_seen": rec.get("last_seen", ""),
                    }
        self._users = users

        seq = 0
        room: list[dict[str, Any]] = []
        inboxes: dict[str, list[dict[str, Any]]] = {}
        if self._messages_path.exists():
            for line in self._messages_path.read_text(encoding="utf-8").splitlines():
                try:
                    msg = json.loads(line)
                except (ValueError, TypeError):
                    continue
                s = msg.get("seq")
                if isinstance(s, int) and s > seq:
                    seq = s
                mtype = msg.get("type")
                if mtype == "broadcast":
                    room.append(msg)
                    for name in list(users):
                        if name != msg.get("from"):
                            self._push_to(inboxes, name, msg)
                elif mtype == "dm":
                    to = msg.get("to")
                    if isinstance(to, str) and to:
                        self._push_to(inboxes, to, msg)
                elif mtype == "join":
                    room.append(msg)
        self._seq = seq
        self._room = room
        self._inboxes = inboxes

    @staticmethod
    def _push_to(inboxes: dict[str, list[dict[str, Any]]], name: str, msg: dict[str, Any]) -> None:
        inboxes.setdefault(name, []).append(msg)
        if len(inboxes[name]) > INBOX_CAP:
            del inboxes[name][: len(inboxes[name]) - INBOX_CAP]

    # ---- 用户 ----

    def register(self, name: str) -> dict[str, Any]:
        now = self._now_iso()
        with self._lock:
            existed = name in self._users
            self._users[name] = {
                "registered_at": self._users.get(name, {}).get("registered_at", now),
                "last_seen": now,
            }
            self._append(self._users_path, {"name": name, "registered_at": now, "last_seen": now})
            if not existed:
                self._emit("join", from_user=name, to=None, content=f"{name} joined", persist=True)
            return {"user": name, "joined": not existed, "users": self._user_list_locked()}

    def _user_list_locked(self) -> list[dict[str, Any]]:
        now = time.time()
        out = []
        for name, info in sorted(self._users.items(), key=lambda kv: kv[1].get("registered_at", "")):
            out.append({
                "name": name,
                "registered_at": info["registered_at"],
                "last_seen": info["last_seen"],
                "online": self._is_recent(info["last_seen"], now),
            })
        return out

    @staticmethod
    def _is_recent(iso: str, now: float) -> bool:
        try:
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            return (now - dt.timestamp()) < ONLINE_WINDOW_S
        except ValueError:
            return False

    def touch(self, name: str) -> None:
        """更新心跳（注册 / 发送 / 拉取收件箱时调用）。"""
        with self._lock:
            if name in self._users:
                self._users[name]["last_seen"] = self._now_iso()

    # ---- 消息 ----

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def _emit(self, mtype: str, *, from_user: str, to: str | None,
              content: str, persist: bool) -> dict[str, Any]:
        """写入一条消息（dm / broadcast / join）。须在持锁状态或自行加锁。"""
        with self._lock:
            self._seq += 1
            msg: dict[str, Any] = {
                "seq": self._seq,
                "type": mtype,
                "from": from_user,
                "to": to,
                "content": content,
                "ts": self._now_iso(),
            }
            if mtype == "dm":
                if to is not None:
                    self._push_to(self._inboxes, to, msg)
            elif mtype == "broadcast":
                self._room.append(msg)
                for name in list(self._users):
                    if name != from_user:
                        self._push_to(self._inboxes, name, msg)
            elif mtype == "join":
                self._room.append(msg)
            if persist:
                self._append(self._messages_path, msg)
            return msg

    def dm(self, from_user: str, to: str, content: str) -> dict[str, Any]:
        with self._lock:
            if to not in self._users:
                raise UnknownUser(to)
            return self._emit("dm", from_user=from_user, to=to, content=content, persist=True)

    def broadcast(self, from_user: str, content: str) -> dict[str, Any]:
        with self._lock:
            return self._emit("broadcast", from_user=from_user, to=None, content=content, persist=True)

    def inbox(self, name: str, since: int, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            if name not in self._users:
                raise UnknownUser(name)
            self.touch(name)
            return [m for m in self._inboxes.get(name, []) if m["seq"] > since][-limit:]

    def room(self, since: int, limit: int = 500) -> list[dict[str, Any]]:
        with self._lock:
            return [m for m in self._room if m["seq"] > since][-limit:]

    def users(self) -> list[dict[str, Any]]:
        with self._lock:
            return self._user_list_locked()

    def latest_seq(self) -> int:
        with self._lock:
            return self._seq


class UnknownUser(Exception):
    def __init__(self, name: str) -> None:
        super().__init__(f"unknown user: {name}")


# ---------------------------------------------------------------------------
# HTTP 层
# ---------------------------------------------------------------------------

def _send_json(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any] | None:
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return None
    raw = handler.rfile.read(length)
    try:
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else None
    except (ValueError, UnicodeDecodeError):
        return None


def _clean_name(raw: Any) -> str:
    name = str(raw or "").strip()
    if not name:
        raise ValueError("name is required")
    if len(name) > MAX_NAME_LEN or any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise ValueError(f"name must be <= {MAX_NAME_LEN} printable chars")
    return name


def _clean_content(raw: Any) -> str:
    content = str(raw if raw is not None else "")
    if not content.strip():
        raise ValueError("content is required")
    if len(content) > MAX_CONTENT_LEN:
        raise ValueError(f"content too long (max {MAX_CONTENT_LEN})")
    return content


class GroupChatHandler(BaseHTTPRequestHandler):
    store: GroupChatStore          # 由 serve() 注入
    token: str | None = None

    # ---- 通用 ----

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("[groupchat] %s - %s\n" % (self.client_address[0], fmt % args))

    def _authorized(self) -> bool:
        if not self.token:
            return True
        return self.headers.get("Authorization") == f"Bearer {self.token}"

    def _route(self, path: str) -> None:
        if not self._authorized():
            return _send_json(self, 401, {"ok": False, "error": "unauthorized"})
        try:
            handler = self._dispatch(path)
            if handler is not None:
                handler()
            else:
                _send_json(self, 404, {"ok": False, "error": f"not found: {path}"})
        except UnknownUser as e:
            _send_json(self, 404, {"ok": False, "error": str(e)})
        except ValueError as e:
            _send_json(self, 400, {"ok": False, "error": str(e)})
        except Exception:  # noqa: BLE001 —— 兜底，避免连接悬挂
            import traceback
            traceback.print_exc()
            _send_json(self, 500, {"ok": False, "error": "internal error"})

    def _dispatch(self, path: str):
        parts = [p for p in path.split("?")[0].split("/") if p]
        method = self.command

        if method == "GET":
            if parts == ["users"]:
                return lambda: _send_json(self, 200, {"ok": True, "users": self.store.users()})
            if parts == ["health"]:
                return lambda: _send_json(self, 200, {
                    "ok": True, "users": len(self.store.users()),
                    "latest_seq": self.store.latest_seq(),
                })
            if parts == ["room"]:
                since = int(self.query_arg("since", "0"))
                return lambda: _send_json(self, 200, {
                    "ok": True, "since": self.store.latest_seq(),
                    "messages": self.store.room(since),
                })
            if parts[:1] == ["inbox"] and len(parts) == 2:
                name = _clean_name(parts[1])
                since = int(self.query_arg("since", "0"))
                msgs = self.store.inbox(name, since)
                return lambda: _send_json(self, 200, {
                    "ok": True, "since": msgs[-1]["seq"] if msgs else self.store.latest_seq(),
                    "messages": msgs,
                })

        if method == "POST":
            body = _read_json(self) or {}
            if parts == ["register"]:
                name = _clean_name(body.get("name"))
                return lambda: _send_json(self, 200, self.store.register(name))
            if parts == ["dm"]:
                from_user = _clean_name(body.get("from"))
                to = _clean_name(body.get("to"))
                content = _clean_content(body.get("content"))
                self.store.touch(from_user)
                msg = self.store.dm(from_user, to, content)
                return lambda: _send_json(self, 200, {"ok": True, **msg})
            if parts == ["broadcast"]:
                from_user = _clean_name(body.get("from"))
                content = _clean_content(body.get("content"))
                self.store.touch(from_user)
                msg = self.store.broadcast(from_user, content)
                return lambda: _send_json(self, 200, {"ok": True, **msg})
            if parts == ["send"]:
                to_raw = body.get("to")
                if str(to_raw or "").strip().lower() in ("all", "*"):
                    from_user = _clean_name(body.get("from"))
                    content = _clean_content(body.get("content"))
                    self.store.touch(from_user)
                    msg = self.store.broadcast(from_user, content)
                    return lambda: _send_json(self, 200, {"ok": True, **msg})
                from_user = _clean_name(body.get("from"))
                to = _clean_name(to_raw)
                content = _clean_content(body.get("content"))
                self.store.touch(from_user)
                msg = self.store.dm(from_user, to, content)
                return lambda: _send_json(self, 200, {"ok": True, **msg})
        return None

    def query_arg(self, key: str, default: str) -> str:
        raw = self.path.split("?", 1)[1] if "?" in self.path else ""
        for pair in raw.split("&"):
            if "=" in pair:
                k, v = pair.split("=", 1)
                if k == key:
                    return v
        return default

    # ---- BaseHTTPRequestHandler 入口 ----

    def do_GET(self) -> None:  # noqa: N802 —— http.server 命名约定
        self._route(self.path)

    def do_POST(self) -> None:  # noqa: N802
        self._route(self.path)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def serve(state_dir: Path, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
          token: str | None = None) -> None:
    store = GroupChatStore(state_dir)
    handler_cls = type("BiscuitGroupChatHandler", (GroupChatHandler,), {
        "store": store,
        "token": token,
    })
    server = ThreadingHTTPServer((host, port), handler_cls)
    print(f"群聊服务已启动: http://{host}:{port}")
    print(f"  状态目录: {state_dir}")
    print(f"  已注册用户: {len(store.users())} | 最近 seq: {store.latest_seq()}")
    print("  按 Ctrl-C 停止")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本地群聊消息中枢（HTTP，纯标准库）")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"绑定地址（默认 {DEFAULT_HOST}）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"监听端口（默认 {DEFAULT_PORT}）")
    parser.add_argument("--state-dir", type=Path,
                        default=Path.home() / ".biscuitbot" / "groupchat",
                        help="持久化目录（默认 ~/.biscuitbot/groupchat）")
    parser.add_argument("--token", default=None,
                        help="可选访问令牌；设置后需 Authorization: Bearer <token>")
    args = parser.parse_args(argv)
    serve(args.state_dir, args.host, args.port, args.token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
