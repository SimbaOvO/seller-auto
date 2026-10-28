import json
import os
import plistlib
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from seller_auto.browser_launch import (
    STEALTH_JS,
    chrome_for_testing_binary,
    default_playwright_browsers_dir,
    desktop_chrome_user_agent,
    is_sandbox_browsers_path,
    mark_clean_shutdown,
    persistent_context_options,
    playwright_browsers_dir,
    playwright_install_command,
    profile_is_busy,
    read_chromium_version,
    snapshot_profile,
    use_playwright_browsers,
)
from seller_auto.pacing import DelaySettings


class BrowserLaunchTests(unittest.TestCase):
    def test_background_uses_full_chrome_without_automation_markers(self) -> None:
        profile = Path("/tmp/seller-auto-data/profile")
        options = persistent_context_options(profile, headless=True, chrome_version="156.0.8078.4")
        self.assertTrue(options["headless"])
        self.assertEqual(options["channel"], "chrome-for-testing")
        self.assertEqual(options["user_agent"], desktop_chrome_user_agent("156.0.8078.4"))
        self.assertNotIn("HeadlessChrome", options["user_agent"])
        if sys.platform == "win32":
            self.assertIn("Windows NT 10.0", options["user_agent"])
        else:
            self.assertIn("Macintosh", options["user_agent"])
        self.assertNotIn("X11", options["user_agent"])
        self.assertIn("--enable-automation", options["ignore_default_args"])
        self.assertIn("--disable-blink-features=AutomationControlled", options["args"])
        self.assertEqual(options["locale"], "es-AR")
        self.assertNotIn("canvas", STEALTH_JS.lower())
        self.assertIn("webdriver", STEALTH_JS)
        self.assertIn("HeadlessChrome", STEALTH_JS)

    def test_foreground_keeps_the_browser_user_agent(self) -> None:
        options = persistent_context_options(
            Path("/tmp/seller-auto-data/profile"),
            headless=False,
            chrome_version="156.0.8078.4",
        )
        self.assertFalse(options["headless"])
        self.assertNotIn("channel", options)
        self.assertNotIn("user_agent", options)

    def test_read_chromium_version_from_plist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plist = (
                root
                / "chromium-1248"
                / "chrome-mac-arm64"
                / "Google Chrome for Testing.app"
                / "Contents"
                / "Info.plist"
            )
            plist.parent.mkdir(parents=True)
            plist.write_bytes(plistlib.dumps({"CFBundleShortVersionString": "156.0.8078.4"}))
            self.assertEqual(read_chromium_version(root), "156.0.8078.4")

    def test_clean_shutdown_mark_is_written_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            prefs = Path(directory) / "Default" / "Preferences"
            prefs.parent.mkdir()
            prefs.write_text(json.dumps({"profile": {"exit_type": "Crashed", "exited_cleanly": False}}), encoding="utf-8")
            self.assertFalse(profile_is_busy(Path(directory)))
            self.assertTrue(mark_clean_shutdown(Path(directory)))
            saved = json.loads(prefs.read_text(encoding="utf-8"))
            self.assertEqual(saved["profile"]["exit_type"], "Normal")
            self.assertFalse(mark_clean_shutdown(Path(directory)))

    def test_user_agent_follows_the_requested_system(self) -> None:
        windows = desktop_chrome_user_agent("156.0.8078.4", system="win32")
        mac = desktop_chrome_user_agent("156.0.8078.4", system="darwin")
        self.assertIn("Windows NT 10.0", windows)
        self.assertNotIn("Macintosh", windows)
        self.assertNotIn("HeadlessChrome", windows)
        self.assertIn("Macintosh", mac)
        self.assertNotIn("Windows NT", mac)
        self.assertNotIn("HeadlessChrome", mac)

    def test_read_chromium_version_from_binary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "chromium-1300" / "chrome-win64" / "chrome.exe"
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"")
            completed = mock.Mock(stdout="Google Chrome for Testing 156.0.8078.4\n", stderr="")
            with mock.patch("seller_auto.browser_launch.subprocess.run", return_value=completed):
                self.assertEqual(read_chromium_version(root), "156.0.8078.4")

    def test_newest_chromium_revision_wins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            older = root / "chromium-999" / "chrome-win64" / "chrome.exe"
            newer = root / "chromium-1148" / "chrome-win64" / "chrome.exe"
            older.parent.mkdir(parents=True)
            newer.parent.mkdir(parents=True)
            older.write_bytes(b"old")
            newer.write_bytes(b"new")
            self.assertEqual(chrome_for_testing_binary(root), newer)

    def test_windows_browser_cache_uses_local_app_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch("seller_auto.browser_launch.sys.platform", "win32"),
                mock.patch.dict(os.environ, {"LOCALAPPDATA": directory}),
            ):
                self.assertEqual(default_playwright_browsers_dir(), Path(directory) / "ms-playwright")

    def test_install_command_matches_platform(self) -> None:
        with mock.patch("seller_auto.browser_launch.sys.platform", "win32"):
            self.assertEqual(
                playwright_install_command(),
                r".venv\Scripts\python.exe -m playwright install chromium",
            )
        with mock.patch("seller_auto.browser_launch.sys.platform", "darwin"):
            self.assertEqual(
                playwright_install_command(),
                ".venv/bin/python -m playwright install chromium",
            )

    @unittest.skipUnless(sys.platform != "win32", "符号链接锁只在 macOS 上使用")
    def test_posix_profile_lock_matches_a_live_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory)
            (profile / "SingletonLock").symlink_to(f"host-{os.getpid()}")
            self.assertTrue(profile_is_busy(profile))
            (profile / "SingletonLock").unlink()
            (profile / "SingletonLock").symlink_to("host-4194304")
            self.assertFalse(profile_is_busy(profile))

    @unittest.skipUnless(sys.platform == "win32", "lockfile 只在 Windows 上使用")
    def test_windows_lockfile_marks_a_live_profile(self) -> None:
        import msvcrt

        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory)
            lock = profile / "lockfile"
            lock.write_bytes(b"\0")
            handle = open(lock, "r+b")
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            try:
                self.assertTrue(profile_is_busy(profile))
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                handle.close()
            self.assertFalse(profile_is_busy(profile))

    def test_snapshot_keeps_login_and_drops_the_process_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "profile-enrich"
            cookies = source / "Default" / "Cookies"
            cache = source / "Default" / "Cache" / "data_0"
            cookies.parent.mkdir(parents=True)
            cache.parent.mkdir()
            cookies.write_bytes(b"cookie")
            cache.write_bytes(b"cache")
            (source / "lockfile").write_bytes(b"lock")
            try:
                (source / "SingletonLock").symlink_to(f"host-{os.getpid()}")
            except OSError:
                (source / "SingletonLock").write_text("locked", encoding="utf-8")
            destination = snapshot_profile(source, Path(directory) / "profile-enrich-backfill")
            self.assertEqual((destination / "Default" / "Cookies").read_bytes(), b"cookie")
            self.assertFalse((destination / "SingletonLock").exists())
            self.assertFalse((destination / "lockfile").exists())
            self.assertTrue((source / "lockfile").is_file())
            self.assertTrue((source / "SingletonLock").is_symlink() or (source / "SingletonLock").is_file())
            self.assertFalse((destination / "Default" / "Cache").exists())

    def test_snapshot_replaces_a_read_only_destination_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            destination = Path(directory) / "dest"
            (source / "Default").mkdir(parents=True)
            (destination / "Default").mkdir(parents=True)
            (source / "Default" / "Cookies").write_bytes(b"new")
            old = destination / "Default" / "Cookies"
            old.write_bytes(b"old")
            old.chmod(stat.S_IREAD)
            snapshot_profile(source, destination)
            self.assertEqual((destination / "Default" / "Cookies").read_bytes(), b"new")

    def test_snapshot_logs_when_login_files_fail_to_copy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "profile-enrich"
            cookies = source / "Default" / "Cookies"
            cookies.parent.mkdir(parents=True)
            cookies.write_bytes(b"cookie")
            (source / "Local State").write_text("{}", encoding="utf-8")
            original_copy = shutil.copy2

            def fail_cookies(src, dst, *args, **kwargs):
                if Path(src).name == "Cookies":
                    raise OSError("locked")
                return original_copy(src, dst, *args, **kwargs)

            with (
                mock.patch("seller_auto.browser_launch.shutil.copy2", side_effect=fail_cookies),
                mock.patch("seller_auto.browser_launch.step") as logged,
            ):
                snapshot_profile(source, Path(directory) / "dest")
            message = " ".join(call.args[1] for call in logged.call_args_list)
            self.assertIn("Default/Cookies", message)
            self.assertIn("重新登录", message)

    @unittest.skipUnless(sys.platform != "win32", "符号链接锁只在 macOS 上使用")
    def test_snapshot_leaves_a_busy_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "profile-enrich"
            destination = Path(directory) / "profile-enrich-backfill"
            (source / "Default").mkdir(parents=True)
            (destination / "Default").mkdir(parents=True)
            (source / "Default" / "Cookies").write_bytes(b"new")
            (destination / "Default" / "Cookies").write_bytes(b"old")
            (destination / "SingletonLock").symlink_to(f"host-{os.getpid()}")
            snapshot_profile(source, destination)
            self.assertEqual((destination / "Default" / "Cookies").read_bytes(), b"old")
            self.assertTrue((destination / "SingletonLock").is_symlink())


class PlaywrightBrowsersPathTests(unittest.TestCase):
    def test_sandbox_cache_is_ignored(self) -> None:
        sandbox = (
            "/var/folders/_3/tmp/cursor-sandbox-cache/"
            "5f9dd362485f432c858dcbff1bc0fd02/playwright"
        )
        self.assertTrue(is_sandbox_browsers_path(sandbox))
        with mock.patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": sandbox}):
            path = playwright_browsers_dir()
        self.assertEqual(path, default_playwright_browsers_dir())
        self.assertNotIn("cursor-sandbox-cache", str(path))

    def test_use_playwright_browsers_overrides_sandbox_env(self) -> None:
        sandbox = "/tmp/cursor-sandbox-cache/playwright"
        with mock.patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": sandbox}):
            path = use_playwright_browsers()
            self.assertEqual(Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"]), path)
            self.assertNotIn("cursor-sandbox-cache", os.environ["PLAYWRIGHT_BROWSERS_PATH"])


class BackgroundDefaultTests(unittest.TestCase):
    def test_background_is_the_default(self) -> None:
        self.assertTrue(DelaySettings().headless)
        self.assertTrue(DelaySettings.from_argv([]).headless)
        self.assertFalse(DelaySettings.from_argv(["--foreground"]).headless)
        self.assertTrue(DelaySettings.from_argv(["--foreground", "--headless"]).headless)


if __name__ == "__main__":
    unittest.main()
