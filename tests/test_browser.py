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
            mock.patch("seller_auto.browser.read_chromium_version", return_value="156.0.8078.4") as version,
            mock.patch(
                "seller_auto.browser.persistent_context_options",
                return_value={"user_data_dir": "/tmp/seller-auto-dead"},
            ) as options,
            mock.patch("playwright.sync_api.sync_playwright") as sync_playwright,
            mock.patch("seller_auto.browser.apply_stealth") as stealth,
        ):
            sync_playwright.return_value.start.return_value = playwright
            browser.start()
        self.assertIs(browser.context, alive)
        playwright.chromium.launch_persistent_context.assert_called_once()
        stealth.assert_called_once_with(alive)
        alive.add_init_script.assert_called()
        version.assert_called_once_with(Path("/tmp/browsers"))
        self.assertEqual(options.call_args.kwargs["chrome_version"], "156.0.8078.4")
        self.assertTrue(options.call_args.kwargs["headless"])

    def test_search_grid_is_not_a_verification_page(self) -> None:
        browser = ArgentinaBrowser(DelaySettings(), Path("/tmp/seller-auto-dead"))
        page = mock.Mock()
        page.locator.return_value.count.return_value = 8
        page.url = "https://listado.mercadolibre.com.ar/panel-perforado-para-pared"
        self.assertFalse(browser.needs_verification(page))

    def test_verification_reopens_in_the_foreground(self) -> None:
        browser = ArgentinaBrowser(DelaySettings(), Path("/tmp/seller-auto-dead"))
        page = mock.Mock()
        page.url = "https://www.mercadolibre.com.ar/gz/captcha"
        browser.list_page = page
        browser.detail_page = None
        browser.headless = True
        with mock.patch.object(browser, "_reopen") as reopen:
            shown = browser._show_for_verification(page)
        self.assertIs(shown, page)
        reopen.assert_called_once_with(
            headless=False,
            urls=["https://www.mercadolibre.com.ar/gz/captcha"],
            restore_detail=False,
        )

    def test_return_to_background_hides_without_restarting(self) -> None:
        browser = ArgentinaBrowser(DelaySettings(), Path("/tmp/seller-auto-dead"))
        page = mock.Mock()
        page.url = "https://www.mercadolibre.com.ar/item"
        browser.list_page = page
        browser.detail_page = None
        browser.headless = False
        browser.background = True
        browser._window_shown = True
        with (
            mock.patch.object(browser, "_reopen") as reopen,
            mock.patch.object(browser, "_conceal_window") as conceal,
        ):
            browser._return_to_background(page)
        reopen.assert_not_called()
        conceal.assert_called_once_with(page)
        self.assertFalse(browser.headless)

    def test_headed_verification_reuses_the_same_window(self) -> None:
        browser = ArgentinaBrowser(DelaySettings(), Path("/tmp/seller-auto-dead"))
        page = mock.Mock()
        page.url = "https://www.mercadolibre.com.ar/gz/captcha"
        browser.list_page = page
        browser.context = mock.Mock()
        browser.headless = False
        browser._window_shown = False
        with (
            mock.patch("seller_auto.browser.context_is_alive", return_value=True),
            mock.patch("seller_auto.browser._chrome_for_testing_running", return_value=True),
            mock.patch.object(browser, "_reopen") as reopen,
            mock.patch.object(browser, "_reveal_window") as reveal,
            mock.patch.object(browser, "_activate_chrome", return_value=True) as activate,
        ):
            shown = browser._show_for_verification(page)
        self.assertIs(shown, page)
        reopen.assert_not_called()
        reveal.assert_called_once_with(page)
        activate.assert_called_once()

    def test_headed_verification_reopens_when_window_was_closed(self) -> None:
        browser = ArgentinaBrowser(DelaySettings(), Path("/tmp/seller-auto-dead"))
        page = mock.Mock()
        page.url = "https://www.mercadolibre.com.ar/gz/captcha"
        browser.list_page = page
        browser.context = mock.Mock()
        browser.headless = False
        browser._window_shown = True
        reopened = mock.Mock()
        reopened.url = page.url
        with (
            mock.patch("seller_auto.browser.context_is_alive", return_value=False),
            mock.patch("seller_auto.browser._chrome_for_testing_running", return_value=False),
            mock.patch.object(browser, "_reopen") as reopen,
            mock.patch.object(browser, "_activate_chrome", return_value=True),
            mock.patch.object(browser, "_urls_to_restore", return_value=[page.url]),
        ):
            browser.list_page = reopened
            shown = browser._show_for_verification(page)
        reopen.assert_called_once_with(headless=False, urls=[page.url], restore_detail=False)
        self.assertIs(shown, reopened)

    def test_headless_challenge_stays_without_a_window(self) -> None:
        browser = ArgentinaBrowser(DelaySettings(), Path("/tmp/seller-auto-dead"))
        page = mock.Mock()
        page.url = "https://www.mercadolibre.com.mx/captcha/wall/logged"
        page.locator.return_value.count.return_value = 0
        browser.headless = True
        browser.background = True
        with mock.patch.object(browser, "_show_for_verification") as show:
            result = browser.ensure_clear(page, "ml")
        show.assert_not_called()
        self.assertTrue(browser.headless)
        self.assertEqual(result["status"], "needs_human_verification")
        self.assertIn("没有打开窗口", result["message"])

    def test_presenting_a_challenge_stays_headless(self) -> None:
        browser = ArgentinaBrowser(DelaySettings(), Path("/tmp/seller-auto-dead"))
        page = mock.Mock()
        page.url = "https://www.mercadolibre.com.mx/captcha/wall/logged"
        page.locator.return_value.count.return_value = 0
        browser.list_page = page
        browser.headless = True
        browser.background = True
        with (
            mock.patch.object(browser, "_show_for_verification") as show,
            mock.patch.object(browser, "_ensure_headless_for_verification", return_value=page) as ensure,
            mock.patch.object(browser, "_attempt_verification_clicks") as clicks,
            mock.patch.object(browser, "needs_verification", side_effect=[True, True, True]),
            mock.patch.object(browser, "_has_recaptcha_image_challenge", return_value=False),
            mock.patch.object(browser, "_wait_until_clear", return_value=False) as wait,
            mock.patch("seller_auto.browser.time.sleep"),
        ):
            result = browser.ensure_clear(page, "ml", present=True)
        show.assert_not_called()
        ensure.assert_called_once_with(page)
        clicks.assert_called_once()
        wait.assert_called_once()
        self.assertEqual(result["status"], "needs_human_verification")
        self.assertIn("自主验证还没通过", result["message"])
        self.assertTrue(browser.challenge_pending)
        self.assertTrue(browser.headless)

    def test_presenting_returns_captcha_tiles_for_model(self) -> None:
        browser = ArgentinaBrowser(DelaySettings(), Path("/tmp/seller-auto-dead"))
        page = mock.Mock()
        page.url = "https://www.mercadolibre.com.mx/captcha/wall/logged"
        page.locator.return_value.count.return_value = 0
        browser.list_page = page
        browser.headless = True
        browser.background = True
        tiles = {
            "ok": False,
            "status": "needs_captcha_selection",
            "message": "已出现 reCAPTCHA 图片题",
            "challenge_id": "abc123",
            "prompt": "Selecciona todas las imágenes con una boca de incendios",
            "tile_count": 9,
            "grid": "3x3",
            "image_bytes": [("tile_0", b"jpeg")],
        }
        with (
            mock.patch.object(browser, "_show_for_verification") as show,
            mock.patch.object(browser, "_ensure_headless_for_verification", return_value=page),
            mock.patch.object(browser, "needs_verification", return_value=True),
            mock.patch.object(browser, "_has_recaptcha_image_challenge", return_value=True),
            mock.patch.object(browser, "_image_challenge_payload", return_value=tiles) as capture,
            mock.patch.object(browser, "_wait_until_clear") as wait,
            mock.patch.object(browser, "_attempt_verification_clicks") as clicks,
        ):
            result = browser.ensure_clear(page, "ml", present=True)
        show.assert_not_called()
        wait.assert_not_called()
        clicks.assert_not_called()
        capture.assert_called_once_with(page)
        self.assertEqual(result["status"], "needs_captcha_selection")
        self.assertEqual(result["challenge_id"], "abc123")
        self.assertIn("图片题", result["message"])

    def test_image_challenge_stops_auto_clicks(self) -> None:
        browser = ArgentinaBrowser(DelaySettings(), Path("/tmp/seller-auto-dead"))
        page = mock.Mock()
        browser.list_page = page
        with (
            mock.patch.object(browser, "needs_verification", return_value=True),
            mock.patch.object(browser, "_has_recaptcha_image_challenge", return_value=True),
            mock.patch.object(browser, "_attempt_verification_clicks") as clicks,
            mock.patch.object(browser, "_reload_verification") as reload,
            mock.patch("seller_auto.browser.time.sleep"),
            mock.patch("seller_auto.browser.time.monotonic", side_effect=[0, 1, 2, 3, 4, 5]),
        ):
            cleared = browser._wait_until_clear(page, timeout=10, attempt=True)
        self.assertFalse(cleared)
        clicks.assert_not_called()
        reload.assert_not_called()

    def test_presenting_a_challenge_clears_when_autonomous_verify_succeeds(self) -> None:
        browser = ArgentinaBrowser(DelaySettings(), Path("/tmp/seller-auto-dead"))
        page = mock.Mock()
        page.url = "https://www.mercadolibre.com.mx/captcha/wall/logged"
        page.locator.return_value.count.return_value = 0
        browser.list_page = page
        browser.headless = True
        browser.background = True
        browser.challenge_pending = True
        with (
            mock.patch.object(browser, "_show_for_verification") as show,
            mock.patch.object(browser, "_ensure_headless_for_verification", return_value=page),
            mock.patch.object(browser, "_attempt_verification_clicks"),
            mock.patch.object(browser, "needs_verification", side_effect=[True, False]),
            mock.patch.object(browser, "_has_recaptcha_image_challenge", return_value=False),
            mock.patch.object(browser, "_reward_if_cleared", return_value=None) as reward,
            mock.patch("seller_auto.browser.time.sleep"),
        ):
            result = browser.ensure_clear(page, "ml", present=True)
        show.assert_not_called()
        self.assertIsNone(result)
        reward.assert_called_once_with("ml")

    def test_apply_captcha_selection_clicks_tiles_and_clears(self) -> None:
        browser = ArgentinaBrowser(DelaySettings(), Path("/tmp/seller-auto-dead"))
        page = mock.Mock()
        browser.list_page = page
        browser.headless = True
        browser.challenge_pending = True
        browser.challenge_site = "ml"
        browser.captcha_challenge_id = "cid1"
        frame = mock.Mock()
        tiles = mock.Mock()
        tiles.count.return_value = 9
        tile = mock.Mock()
        tiles.nth.return_value = tile
        with (
            mock.patch.object(browser, "_ensure_headless_for_verification", return_value=page),
            mock.patch.object(browser, "needs_verification", side_effect=[True, False]),
            mock.patch.object(browser, "_has_recaptcha_image_challenge", return_value=True),
            mock.patch.object(browser, "_recaptcha_bframe", return_value=frame),
            mock.patch.object(browser, "_recaptcha_tiles", return_value=tiles),
            mock.patch.object(browser, "_click_recaptcha_verify", return_value=True),
            mock.patch.object(browser, "_reward_if_cleared", return_value=None) as reward,
            mock.patch("seller_auto.browser.time.sleep"),
        ):
            result = browser.apply_captcha_selection(page, [1, 4], "cid1")
        self.assertIsNone(result)
        self.assertEqual(tiles.nth.call_count, 2)
        reward.assert_called_once_with("ml")

    def test_apply_captcha_selection_returns_new_tiles_when_still_blocked(self) -> None:
        browser = ArgentinaBrowser(DelaySettings(), Path("/tmp/seller-auto-dead"))
        page = mock.Mock()
        browser.list_page = page
        browser.headless = True
        browser.challenge_pending = True
        browser.captcha_challenge_id = "cid1"
        frame = mock.Mock()
        tiles = mock.Mock()
        tiles.count.return_value = 9
        tiles.nth.return_value = mock.Mock()
        again = {
            "ok": False,
            "status": "needs_captcha_selection",
            "challenge_id": "cid2",
            "image_bytes": [("tile_0", b"x")],
            "message": "图片题",
        }
        with (
            mock.patch.object(browser, "_ensure_headless_for_verification", return_value=page),
            mock.patch.object(browser, "needs_verification", return_value=True),
            mock.patch.object(browser, "_has_recaptcha_image_challenge", return_value=True),
            mock.patch.object(browser, "_recaptcha_bframe", return_value=frame),
            mock.patch.object(browser, "_recaptcha_tiles", return_value=tiles),
            mock.patch.object(browser, "_click_recaptcha_verify", return_value=True),
            mock.patch.object(browser, "_image_challenge_payload", return_value=again),
            mock.patch("seller_auto.browser.time.sleep"),
        ):
            result = browser.apply_captcha_selection(page, [0], "cid1")
        self.assertEqual(result["status"], "needs_captcha_selection")
        self.assertEqual(browser.captcha_fail_count, 1)

    def test_reveal_window_does_not_activate(self) -> None:
        browser = ArgentinaBrowser(DelaySettings(), Path("/tmp/seller-auto-dead"))
        page = mock.Mock()
        browser.headless = False
        browser._window_shown = False
        with (
            mock.patch("seller_auto.browser._chrome_for_testing_visible", return_value=True) as visible,
            mock.patch.object(browser, "_place_bounds", return_value=False),
            mock.patch.object(page, "bring_to_front") as front,
        ):
            browser._reveal_window(page)
        visible.assert_called_once_with(True)
        front.assert_not_called()
        self.assertTrue(browser._window_shown)

    def test_reopen_writes_cookies_into_the_new_browser(self) -> None:
        browser = ArgentinaBrowser(DelaySettings(), Path("/tmp/seller-auto-dead"))
        browser.list_page = mock.Mock()
        old_context = mock.Mock()
        old_context.cookies.return_value = [{"name": "ssid", "value": "ok", "domain": ".mercadolibre.com.mx", "path": "/"}]
        browser.context = old_context

        def restart() -> None:
            browser.context = mock.Mock()

        with (
            mock.patch.object(browser, "close"),
            mock.patch.object(browser, "start", side_effect=restart),
            mock.patch.object(browser, "goto"),
        ):
            browser._reopen(headless=True, urls=["https://www.mercadolibre.com.ar/item"], restore_detail=False)
        browser.context.add_cookies.assert_called_once_with(
            [{"name": "ssid", "value": "ok", "domain": ".mercadolibre.com.mx", "path": "/"}]
        )
        self.assertTrue(browser.headless)
