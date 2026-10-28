import unittest
from pathlib import Path
from unittest import mock

from seller_auto.browser import ArgentinaBrowser, browser_closed_error, context_is_alive
from seller_auto.pacing import DelaySettings


class DeadContext:
    browser = None
    pages = [object()]

    def cookies(self):
        raise RuntimeError("Target page, context or browser has been closed")

    def close(self) -> None:
        raise RuntimeError("Target page, context or browser has been closed")


class BrowserRecoveryTests(unittest.TestCase):
    def test_context_is_dead_when_pages_raise(self) -> None:
        self.assertFalse(context_is_alive(None))
        self.assertFalse(context_is_alive(DeadContext()))
        alive = mock.Mock()
        alive.browser = None
        alive.pages = []
        alive.cookies.return_value = []
        self.assertTrue(context_is_alive(alive))

    def test_cached_pages_do_not_keep_a_dead_context_alive(self) -> None:
        self.assertTrue(DeadContext.pages)
        self.assertFalse(context_is_alive(DeadContext()))

    def test_closed_error_matches_playwright_message(self) -> None:
        self.assertTrue(
            browser_closed_error(RuntimeError("BrowserContext.add_cookies: Target page, context or browser has been closed"))
        )
        self.assertTrue(
            browser_closed_error(RuntimeError("Failed to create a ProcessSingleton for your profile directory. The profile is already in use"))
        )
        self.assertFalse(browser_closed_error(RuntimeError("登录后仍然提示未登录")))

    def test_start_relaunches_when_context_is_dead(self) -> None:
        browser = ArgentinaBrowser(DelaySettings(), Path("/tmp/seller-auto-dead"))
        browser.context = DeadContext()
        alive = mock.Mock()
        alive.pages = []
        page = mock.Mock()
        page.is_closed.return_value = False
        alive.new_page.return_value = page
        playwright = mock.Mock()
        playwright.chromium.launch_persistent_context.return_value = alive
        with (
            mock.patch.object(browser, "_wait_until_profile_free"),
            mock.patch("seller_auto.browser.mark_clean_shutdown", return_value=False),
            mock.patch("seller_auto.browser.use_playwright_browsers", return_value=Path("/tmp/browsers")),
            mock.patch("seller_auto.browser.read_chromium_version", return_value="156.0.8078.4"),
            mock.patch(
                "seller_auto.browser.persistent_context_options",
                return_value={"user_data_dir": "/tmp/seller-auto-dead"},
            ),
            mock.patch("playwright.sync_api.sync_playwright") as sync_playwright,
        ):
            sync_playwright.return_value.start.return_value = playwright
            browser.start()
        self.assertIs(browser.context, alive)
        playwright.chromium.launch_persistent_context.assert_called_once()
