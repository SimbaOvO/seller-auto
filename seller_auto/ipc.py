"""本地套接字。短命令连到常驻后台，不各自启动浏览器。"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from seller_auto.service import ToolPayload

ROOT = Path(__file__).resolve().parent.parent
SOCKET_PATH = ROOT / "data" / "seller-auto.sock"
PID_PATH = ROOT / "data" / "seller-auto.pid"


def encode_message(payload: dict) -> bytes:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return len(body).to_bytes(4, "big") + body


def read_message(connection: socket.socket) -> dict:
    header = _read_exact(connection, 4)
    length = int.from_bytes(header, "big")
    if length > 64 * 1024 * 1024:
        raise ConnectionError("返回内容过大")
    body = _read_exact(connection, length)
    return json.loads(body.decode("utf-8"))


def _read_exact(connection: socket.socket, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining:
        chunk = connection.recv(remaining)
        if not chunk:
            raise ConnectionError("后台进程已断开")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _clear_stale() -> None:
    if PID_PATH.exists():
        try:
            pid = int(PID_PATH.read_text().strip())
        except ValueError:
            pid = 0
        if not pid or not _pid_alive(pid):
            PID_PATH.unlink(missing_ok=True)
            SOCKET_PATH.unlink(missing_ok=True)


def _ping() -> bool:
    if not SOCKET_PATH.exists():
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(1)
            client.connect(str(SOCKET_PATH))
            client.sendall(encode_message({"method": "ping", "params": {}}))
            reply = read_message(client)
        return reply.get("ok") is True
    except OSError:
        return False


def ensure_daemon() -> None:
    SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)
    if _ping():
        return
    _clear_stale()
    log_file = ROOT / "logs" / "seller-auto.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["SELLER_AUTO_DAEMON"] = "1"
    env["SELLER_AUTO_LOG"] = str(log_file)
    with log_file.open("a", encoding="utf-8") as handle:
        subprocess.Popen(
            [sys.executable, "-m", "seller_auto.daemon"],
            cwd=str(ROOT),
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    for _ in range(50):
        if _ping():
            return
        time.sleep(0.1)
    raise ConnectionError("后台进程没有在预期时间内启动，请查看 logs/seller-auto.log")


def call(method: str, params: dict | None = None) -> ToolPayload:
    ensure_daemon()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(SOCKET_PATH))
        client.sendall(encode_message({"method": method, "params": params or {}}))
        reply = read_message(client)
    if not reply.get("ok", True) and "error" in reply and "data" not in reply:
        return ToolPayload({"ok": False, "status": "error", "message": reply["error"]})
    images = [(item["caption"], _b64decode(item["data"])) for item in reply.get("images") or []]
    return ToolPayload(reply.get("data") or {"ok": False, "status": "error", "message": "后台没有返回结果"}, images)


def payload_to_message(payload: ToolPayload) -> dict:
    import base64

    return {
        "ok": True,
        "data": payload.data,
        "images": [
            {"caption": caption, "data": base64.standard_b64encode(data).decode("ascii")}
            for caption, data in payload.images
        ],
    }


def _b64decode(value: str) -> bytes:
    import base64

    return base64.standard_b64decode(value)
