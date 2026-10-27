import json
import os
import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from seller_auto.browser_launch import (
    STEALTH_JS,
    default_playwright_browsers_dir,
    desktop_chrome_user_agent,
    is_sandbox_browsers_path,
    mark_clean_shutdown,
    persistent_context_options,
    playwright_browsers_dir,
    profile_is_busy,
    read_chromium_version,
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
