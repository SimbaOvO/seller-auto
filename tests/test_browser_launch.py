import json
import plistlib
import tempfile
import unittest
from pathlib import Path

from seller_auto.browser_launch import (
    STEALTH_JS,
    desktop_chrome_user_agent,
    mark_clean_shutdown,
    persistent_context_options,
    profile_is_busy,
    read_chromium_version,
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


class BackgroundDefaultTests(unittest.TestCase):
    def test_background_is_the_default(self) -> None:
        self.assertTrue(DelaySettings().headless)
        self.assertTrue(DelaySettings.from_argv([]).headless)
        self.assertFalse(DelaySettings.from_argv(["--foreground"]).headless)
        self.assertTrue(DelaySettings.from_argv(["--foreground", "--headless"]).headless)


if __name__ == "__main__":
    unittest.main()
