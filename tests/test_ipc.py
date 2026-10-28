import socket
import sys
import unittest
from pathlib import Path
from unittest import mock

from seller_auto.ipc import (
    CODE_REVISION,
    _CREATE_NEW_PROCESS_GROUP,
    _CREATE_NO_WINDOW,
    _pid_alive,
    _stop_daemon,
    code_revision,
    encode_message,
    ensure_daemon,
    read_message,
)


class IpcTests(unittest.TestCase):
    def test_message_roundtrip(self) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            client.connect(("127.0.0.1", port))
            connection, _address = server.accept()
            try:
                client.sendall(encode_message({"method": "ping", "params": {}}))
                self.assertEqual(read_message(connection)["method"], "ping")
            finally:
                connection.close()
        finally:
            client.close()
            server.close()

    def test_missing_process_is_not_alive(self) -> None:
        with mock.patch("seller_auto.ipc.os.kill", side_effect=OSError("gone")):
            self.assertFalse(_pid_alive(123))

    def test_access_denied_means_the_windows_process_is_alive(self) -> None:
        error = OSError("denied")
        error.winerror = 5
        with (
            mock.patch("seller_auto.ipc.sys.platform", "win32"),
            mock.patch("seller_auto.ipc.os.kill", side_effect=error),
        ):
            self.assertTrue(_pid_alive(123))

    def test_windows_stop_uses_taskkill_tree(self) -> None:
        calls = {"count": 0}

        def alive(_pid: int) -> bool:
            calls["count"] += 1
            return calls["count"] == 1

        with (
            mock.patch("seller_auto.ipc.sys.platform", "win32"),
            mock.patch("seller_auto.ipc._read_pid", return_value=42),
            mock.patch("seller_auto.ipc._pid_alive", side_effect=alive),
            mock.patch("seller_auto.ipc.subprocess.run") as run,
            mock.patch("seller_auto.ipc.remove_endpoint_files") as remove,
        ):
            _stop_daemon()
        args = run.call_args.args[0]
        self.assertEqual(args, ["taskkill", "/PID", "42", "/T", "/F"])
        self.assertEqual(run.call_args.kwargs["creationflags"], _CREATE_NO_WINDOW)
        remove.assert_called_once()

    @unittest.skipUnless(sys.platform != "win32", "进程组只在 macOS 上使用")
    def test_posix_daemon_starts_in_a_new_session(self) -> None:
        with (
            mock.patch("seller_auto.ipc._ping", side_effect=[False, True]),
            mock.patch("seller_auto.ipc._clear_stale"),
            mock.patch("seller_auto.ipc.use_playwright_browsers", return_value=Path("browsers")),
            mock.patch("seller_auto.ipc.subprocess.Popen") as popen,
        ):
            ensure_daemon()
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertNotIn("creationflags", popen.call_args.kwargs)

    def test_windows_daemon_starts_without_a_console(self) -> None:
        with (
            mock.patch("seller_auto.ipc.sys.platform", "win32"),
            mock.patch("seller_auto.ipc._ping", side_effect=[False, True]),
            mock.patch("seller_auto.ipc._clear_stale"),
            mock.patch("seller_auto.ipc.use_playwright_browsers", return_value=Path("browsers")),
            mock.patch("seller_auto.ipc.subprocess.Popen") as popen,
        ):
            ensure_daemon()
        kwargs = popen.call_args.kwargs
        self.assertNotIn("start_new_session", kwargs)
        flags = kwargs["creationflags"]
        self.assertEqual(flags & _CREATE_NEW_PROCESS_GROUP, _CREATE_NEW_PROCESS_GROUP)
        self.assertEqual(flags & _CREATE_NO_WINDOW, _CREATE_NO_WINDOW)

    def test_revision_follows_the_source_files(self) -> None:
        self.assertEqual(code_revision(), CODE_REVISION)
        self.assertEqual(len(CODE_REVISION), 16)
        int(CODE_REVISION, 16)

    def test_same_revision_keeps_the_running_daemon(self) -> None:
        with (
            mock.patch("seller_auto.ipc._ping", return_value=True),
            mock.patch("seller_auto.ipc._revision", return_value=CODE_REVISION),
            mock.patch("seller_auto.ipc._stop_daemon") as stop,
            mock.patch("seller_auto.ipc.subprocess.Popen") as popen,
        ):
            ensure_daemon()
        stop.assert_not_called()
        popen.assert_not_called()

    def test_source_change_restarts_the_daemon(self) -> None:
        with (
            mock.patch("seller_auto.ipc._ping", side_effect=[True, True]),
            mock.patch("seller_auto.ipc._revision", return_value="stale"),
            mock.patch("seller_auto.ipc._stop_daemon") as stop,
            mock.patch("seller_auto.ipc._clear_stale"),
            mock.patch("seller_auto.ipc.use_playwright_browsers", return_value=Path("browsers")),
            mock.patch("seller_auto.ipc.subprocess.Popen"),
        ):
            ensure_daemon()
        stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
