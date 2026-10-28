"""常驻后台。整个抓取只保留一个浏览器窗口。"""

from __future__ import annotations

import os
import signal
import socket
import sys
import threading
import traceback

from seller_auto.browser_launch import use_playwright_browsers
from seller_auto.ipc import (
    CODE_REVISION,
    PID_PATH,
    encode_message,
    payload_to_message,
    read_message,
    remove_endpoint_files,
    serve,
)
from seller_auto.pacing import DelaySettings
from seller_auto.service import SellerAutoService
from seller_auto.step_log import step


def main() -> None:
    os.environ["SELLER_AUTO_DAEMON"] = "1"
    use_playwright_browsers()
    PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    PID_PATH.write_text(str(os.getpid()), encoding="utf-8")
    settings = DelaySettings.from_argv()
    service = SellerAutoService(settings)

    def _shutdown(signum: int, _frame: object) -> None:
        raise SystemExit(signum)

    if sys.platform == "win32":
        signal.signal(signal.SIGBREAK, _shutdown)
    else:
        signal.signal(signal.SIGTERM, _shutdown)
    step("后台", f"已启动，日志 {os.environ.get('SELLER_AUTO_LOG', 'logs/seller-auto.log')}")
    server = serve()
    work = threading.Lock()
    try:
        while True:
            connection, _address = server.accept()
            threading.Thread(target=_handle_connection, args=(connection, service, work), daemon=True).start()
    finally:
        server.close()
        remove_endpoint_files()
        service.close()
        step("后台", "已退出")


def _handle_connection(connection: socket.socket, service: SellerAutoService, work: threading.Lock) -> None:
    with connection:
        try:
            request = read_message(connection)
        except Exception:
            return
        method = request.get("method") or ""
        if method == "ping":
            try:
                connection.sendall(encode_message({"ok": True}))
            except OSError:
                return
            return
        if method == "revision":
            try:
                connection.sendall(encode_message({"ok": True, "revision": CODE_REVISION}))
            except OSError:
                return
            return
        if method == "session_status":
            _run_tool(connection, service, method, request.get("params") or {})
            return
        with work:
            _run_tool(connection, service, method, request.get("params") or {})


def _run_tool(connection: socket.socket, service: SellerAutoService, method: str, params: dict) -> None:
    try:
        handler = getattr(service, method, None)
        if handler is None or method.startswith("_"):
            connection.sendall(encode_message({"ok": False, "error": f"没有这个步骤：{method}"}))
            return
        step(method, "开始")
        payload = handler(**params)
        status = payload.data.get("status", "")
        message = payload.data.get("message", "")
        step(method, f"完成 {status} {message}".strip())
        connection.sendall(encode_message(payload_to_message(payload)))
    except Exception as exc:
        step("错误", f"{exc}\n{traceback.format_exc()}")
        try:
            connection.sendall(encode_message({"ok": False, "error": str(exc)}))
        except OSError:
            return


if __name__ == "__main__":
    main()
