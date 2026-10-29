import ctypes
import importlib.util
import unittest
from pathlib import Path
from unittest import mock

_SPEC = importlib.util.spec_from_file_location(
    "launch_mcp",
    Path(__file__).resolve().parent.parent / "scripts" / "launch_mcp.py",
)
assert _SPEC is not None and _SPEC.loader is not None
launch_mcp = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(launch_mcp)


class LaunchMcpTests(unittest.TestCase):
    def test_windows_keeps_the_spawned_process(self) -> None:
        with (
            mock.patch.object(launch_mcp, "_wait_on_windows", return_value=0) as wait,
            mock.patch.object(launch_mcp.os, "execv") as execv,
        ):
            with self.assertRaises(SystemExit) as caught:
                launch_mcp.handoff(r"E:\seller-auto\.venv\Scripts\python.exe", platform="nt")
        self.assertEqual(caught.exception.code, 0)
        wait.assert_called_once_with(
            [r"E:\seller-auto\.venv\Scripts\python.exe", "-m", "seller_auto.mcp_server"]
        )
        execv.assert_not_called()

    def test_posix_replaces_the_current_process(self) -> None:
        with (
            mock.patch.object(launch_mcp, "_wait_on_windows") as wait,
            mock.patch.object(launch_mcp.os, "execv") as execv,
        ):
            launch_mcp.handoff("/tmp/venv/bin/python", platform="posix")
        execv.assert_called_once_with(
            "/tmp/venv/bin/python",
            ["/tmp/venv/bin/python", "-m", "seller_auto.mcp_server"],
        )
        wait.assert_not_called()

    def test_windows_child_starts_without_a_console(self) -> None:
        process = mock.Mock()
        process.wait.return_value = 0
        process._handle = 42
        kernel = mock.Mock()
        kernel.AssignProcessToJobObject.return_value = 0
        flags = getattr(launch_mcp.subprocess, "CREATE_NO_WINDOW", 0x08000000)
        with (
            mock.patch.object(launch_mcp, "_windows_kill_on_close_job", return_value=7),
            mock.patch.object(launch_mcp.subprocess, "Popen", return_value=process) as opened,
            mock.patch.object(ctypes, "WinDLL", create=True, return_value=kernel),
            mock.patch.object(ctypes, "get_last_error", create=True, return_value=5),
            mock.patch.object(launch_mcp.sys, "stderr") as stderr,
        ):
            code = launch_mcp._wait_on_windows(
                [r"E:\seller-auto\.venv\Scripts\python.exe", "-m", "seller_auto.mcp_server"]
            )
        self.assertEqual(code, 0)
        self.assertEqual(opened.call_args.kwargs["creationflags"], flags)
        self.assertIn("5", stderr.write.call_args.args[0])
        kernel.CloseHandle.assert_called_once_with(7)


if __name__ == "__main__":
    unittest.main()
