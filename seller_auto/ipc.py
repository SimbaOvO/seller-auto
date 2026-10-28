"""本地套接字。短命令连到常驻后台，不各自启动浏览器。"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from seller_auto.browser_launch import use_playwright_browsers
from seller_auto.service import ToolPayload

ROOT = Path(__file__).resolve().parent.parent
SOCKET_PATH = ROOT / "data" / "seller-auto.sock"
PORT_PATH = ROOT / "data" / "seller-auto.port"
PID_PATH = ROOT / "data" / "seller-auto.pid"
def code_revision() -> str:
    """源码一变，摘要就变，后台会重启后再接新代码。"""

    root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for path in sorted(root.glob("*.py")):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


CODE_REVISION = code_revision()
_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


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
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as exc:
        if sys.platform == "win32" and getattr(exc, "winerror", None) == 5:
            return True
        return False
    return True


def _read_pid() -> int:
    if not PID_PATH.exists():
        return 0
    try:
        return int(PID_PATH.read_text(encoding="utf-8").strip())
    except ValueError:
        return 0


def _endpoint_ready() -> bool:
    if sys.platform == "win32":
        return PORT_PATH.is_file()
    return SOCKET_PATH.exists()


def remove_endpoint_files() -> None:
    PID_PATH.unlink(missing_ok=True)
    SOCKET_PATH.unlink(missing_ok=True)
    PORT_PATH.unlink(missing_ok=True)


def serve() -> socket.socket:
    """监听本机命令。macOS 用套接字文件，Windows 用 127.0.0.1 的临时端口。"""

    PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(4)
        PORT_PATH.write_text(str(server.getsockname()[1]), encoding="utf-8")
        return server
    if SOCKET_PATH.exists():
        SOCKET_PATH.unlink()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(SOCKET_PATH))
    server.listen(4)
    return server


def connect(timeout: float | None = None) -> socket.socket:
    if sys.platform == "win32":
        port = int(PORT_PATH.read_text(encoding="utf-8").strip())
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if timeout is not None:
            client.settimeout(timeout)
        client.connect(("127.0.0.1", port))
        return client
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    if timeout is not None:
        client.settimeout(timeout)
    client.connect(str(SOCKET_PATH))
    return client


def _clear_stale() -> None:
    if not PID_PATH.exists():
        return
    if _pid_alive(_read_pid()):
        return
    remove_endpoint_files()


def _ping() -> bool:
    if not _endpoint_ready():
        return False
    try:
        with connect(timeout=1) as client:
            client.sendall(encode_message({"method": "ping", "params": {}}))
            reply = read_message(client)
        return reply.get("ok") is True
    except (OSError, ConnectionError, json.JSONDecodeError, TimeoutError, ValueError):
        return False


def _revision() -> str:
    if not _endpoint_ready():
        return ""
    try:
        with connect(timeout=1) as client:
            client.sendall(encode_message({"method": "revision", "params": {}}))
            reply = read_message(client)
        return str(reply.get("revision") or "")
    except (OSError, ConnectionError, json.JSONDecodeError, TimeoutError, ValueError):
        return ""


def _stop_posix(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return
    for _ in range(50):
        if not _pid_alive(pid):
            return
        time.sleep(0.1)
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def _stop_windows(pid: int) -> None:
    subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        capture_output=True,
        text=True,
        creationflags=_CREATE_NO_WINDOW,
    )
    if _pid_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    for _ in range(50):
        if not _pid_alive(pid):
            return
        time.sleep(0.1)


def _stop_daemon() -> None:
    pid = _read_pid()
    if pid and _pid_alive(pid):
        if sys.platform == "win32":
            _stop_windows(pid)
        else:
            _stop_posix(pid)
    remove_endpoint_files()


def ensure_daemon() -> None:
    SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)
    if _ping():
        if _revision() == CODE_REVISION:
            return
        _stop_daemon()
    _clear_stale()
    log_file = ROOT / "logs" / "seller-auto.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["SELLER_AUTO_DAEMON"] = "1"
    env["SELLER_AUTO_LOG"] = str(log_file)
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(use_playwright_browsers())
    with log_file.open("a", encoding="utf-8") as handle:
        kwargs: dict[str, object] = {
            "cwd": str(ROOT),
            "env": env,
            "stdout": handle,
            "stderr": subprocess.STDOUT,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen([sys.executable, "-m", "seller_auto.daemon"], **kwargs)
    for _ in range(50):
        if _ping():
            return
        time.sleep(0.1)
    raise ConnectionError("后台进程没有在预期时间内启动，请查看 logs/seller-auto.log")


def call(method: str, params: dict | None = None) -> ToolPayload:
    ensure_daemon()
    with connect() as client:
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
