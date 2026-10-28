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


if __name__ == "__main__":
    unittest.main()
