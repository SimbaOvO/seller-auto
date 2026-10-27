"""阿根廷语言、时区和地理位置的浏览器，以及最多两个标签。"""

from __future__ import annotations

import random
import time
from pathlib import Path
from typing import Any, Callable

from seller_auto.browser_launch import (
    STEALTH_JS,
    mark_clean_shutdown,
    persistent_context_options,
    profile_is_busy,
    read_chromium_version,
    use_playwright_browsers,
)
from seller_auto.pacing import Cooldown, DelaySettings
from seller_auto.urls import looks_like_verification_url, text_looks_blocked

NEEDS_HUMAN = "需要手动验证"
VERIFICATION_SELECTORS = (
    "form[action*='validateCaptcha']",
    "#captchacharacters",
    "input#captchacharacters",
)


def pages_to_close(page_ids: list[str], keep_ids: list[str]) -> list[str]:
    keep = set(keep_ids)
    return [page_id for page_id in page_ids if page_id not in keep]


def log(message: str) -> None:
    from seller_auto.step_log import step

    step("浏览器", message)


class ArgentinaBrowser:
    def __init__(
        self,
        settings: DelaySettings,
        profile_dir: Path,
        headless: bool = True,
        locale: str = "es-AR",
        accept_language: str = "es-AR,es;q=0.9",
        timezone_id: str = "America/Argentina/Buenos_Aires",
        geolocation: dict[str, float] | None = None,
    ) -> None:
        self.settings = settings
        self.profile_dir = profile_dir
        self.background = headless
        self.headless = headless
        self.locale = locale
        self.accept_language = accept_language
        self.timezone_id = timezone_id
        self.geolocation = geolocation
        self.amazon_cooldown = Cooldown(settings.amazon_min_delay, settings.amazon_max_delay, label="亚马逊")
        self.ml_cooldown = Cooldown(settings.ml_min_delay, settings.ml_max_delay, label="美客多")
        self._playwright: Any = None
        self.context: Any = None
        self.list_page: Any = None
        self.detail_page: Any = None
        self.challenge_pending = False
        self.challenge_site = ""

    def start(self) -> None:
        if self.context is not None:
            return
        from playwright.sync_api import sync_playwright

        self._wait_until_profile_free()
        if mark_clean_shutdown(self.profile_dir):
            log("已清掉上次异常退出标记")
        browsers = use_playwright_browsers()
        options = persistent_context_options(
            self.profile_dir,
            headless=self.headless,
            chrome_version=read_chromium_version(browsers),
            locale=self.locale,
            accept_language=self.accept_language,
            timezone_id=self.timezone_id,
            geolocation=self.geolocation,
        )
        Path(options["user_data_dir"]).mkdir(parents=True, exist_ok=True)
        playwright = sync_playwright().start()
        try:
            context = playwright.chromium.launch_persistent_context(**options)
        except Exception:
            playwright.stop()
            raise
        self._playwright = playwright
        self.context = context
        context.add_init_script(STEALTH_JS)
        pages = list(self.context.pages)
        self.list_page = pages[0] if pages else self.context.new_page()
        self.detail_page = None
        self.trim_tabs()
        self._configure(self.list_page)
        if self.headless:
            log("已在后台启动完整 Chrome，没有打开窗口。")
        else:
            log("已打开前台窗口。")

    def _wait_until_profile_free(self) -> None:
        deadline = time.monotonic() + 8
        while profile_is_busy(self.profile_dir) and time.monotonic() < deadline:
            time.sleep(0.2)

    def close(self) -> None:
        if self.context is not None:
            self.context.close()
            self.context = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None
        self.list_page = None
        self.detail_page = None

    def _configure(self, page: Any) -> None:
        page.set_default_timeout(15000)
        page.set_default_navigation_timeout(45000)

    def trim_tabs(self) -> None:
        if self.context is None:
            return
        keep: list[Any] = []
        if self.list_page is not None and not self.list_page.is_closed():
            keep.append(self.list_page)
        if (
            self.detail_page is not None
            and self.detail_page is not self.list_page
            and not self.detail_page.is_closed()
        ):
            keep.append(self.detail_page)
        keep_ids = {id(page) for page in keep}
        for page in list(self.context.pages):
            if id(page) not in keep_ids:
                try:
                    page.close()
                except Exception:
                    log("关闭多余标签失败")

    def close_detail(self) -> None:
        closing = self.detail_page is not None and self.detail_page is not self.list_page
        if closing:
            try:
                if not self.detail_page.is_closed():
                    self.detail_page.close()
                    log("已关闭详情标签")
            except Exception:
                log("关闭详情标签失败")
        self.detail_page = None
        self.trim_tabs()

    def pause(self, low: float, high: float) -> None:
        import time

        time.sleep(random.uniform(low, high))

    def pause_amazon_read(self) -> None:
        self.pause(self.settings.amazon_read_min, self.settings.amazon_read_max)

    def pause_ml_read(self) -> None:
        self.pause(self.settings.ml_read_min, self.settings.ml_read_max)

    def pause_ml_scroll(self) -> None:
        self.pause(self.settings.ml_scroll_min, self.settings.ml_scroll_max)

    def needs_verification(self, page: Any) -> bool:
        try:
            url = page.url or ""
        except Exception:
            return False
        if looks_like_verification_url(url):
            return True
        for selector in VERIFICATION_SELECTORS:
            try:
                if page.locator(selector).count() > 0:
                    return True
            except Exception:
                continue
        try:
            snippet = page.evaluate(
                "() => ((document.body && document.body.innerText) || '').slice(0, 1500)"
            )
        except Exception:
            snippet = ""
        return text_looks_blocked(snippet)

    def ensure_clear(self, page: Any, site: str) -> dict[str, Any] | None:
        if not self.needs_verification(page):
            if self.background and not self.headless:
                self._return_to_background(page)
            return self._reward_if_cleared(site)
        self.challenge_pending = True
        self.challenge_site = site
        page = self._show_for_verification(page)
        log("需要手动验证，请在前台窗口里完成")
        if self._wait_until_clear(page, timeout=90):
            self._return_to_background(page)
            return self._reward_if_cleared(site)
        return {
            "ok": False,
            "status": "needs_human_verification",
            "message": NEEDS_HUMAN,
            "url": page.url,
        }

    def _reward_if_cleared(self, site: str) -> None:
        if not self.challenge_pending:
            return None
        self.challenge_pending = False
        cleared_site = self.challenge_site or site
        self.challenge_site = ""
        if cleared_site == "ml":
            extra = random.uniform(self.settings.ml_captcha_min, self.settings.ml_captcha_max)
            self.ml_cooldown.penalize(extra)
            log(f"验证已通过，下一次美客多导航额外等待 {extra:.0f} 秒")
        return None

    def _wait_until_clear(self, page: Any, timeout: float) -> bool:
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.needs_verification(page):
                return True
            time.sleep(2)
        return not self.needs_verification(page)

    def goto(self, page: Any, url: str, timeout: float | None = None) -> None:
        from playwright.sync_api import TimeoutError as PlaywrightTimeout

        options: dict[str, Any] = {"wait_until": "commit"}
        if timeout is not None:
            options["timeout"] = timeout
        log(f"打开页面 {url}")
        page.goto(url, **options)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=timeout or 45000)
        except PlaywrightTimeout:
            log("页面已离开空白页，正文仍在加载")

    def open_detail(
        self,
        url: str,
        timeout: float | None = None,
        viewport: tuple[int, int] | None = None,
    ) -> Any:
        self.start()
        self.close_detail()
        page = self.context.new_page()
        self.detail_page = page
        self._configure(page)
        if viewport is not None:
            page.set_viewport_size({"width": viewport[0], "height": viewport[1]})
        self.trim_tabs()
        try:
            self.goto(page, url, timeout=timeout)
        except Exception:
            self.close_detail()
            raise
        return page

    def click_result(self, locator: Any) -> tuple[Any, bool]:
        """点击列表标题。返回详情所在页面，以及它是不是新标签。"""

        from playwright.sync_api import TimeoutError as PlaywrightTimeout

        self.start()
        self.close_detail()
        pages_before = {page for page in self.context.pages}
        locator.click(timeout=10000)
        self.list_page.wait_for_timeout(1500)
        new_pages = [page for page in self.context.pages if page not in pages_before and not page.is_closed()]
        if new_pages:
            page = new_pages[-1]
            self.detail_page = page
            self._configure(page)
            self.trim_tabs()
            try:
                page.wait_for_load_state("domcontentloaded", timeout=20000)
            except PlaywrightTimeout:
                pass
            return page, True
        try:
            self.list_page.wait_for_load_state("domcontentloaded", timeout=20000)
        except PlaywrightTimeout:
            pass
        self.trim_tabs()
        return self.list_page, False

    def finish_detail(self, opened_new_tab: bool, list_url: str | None = None) -> None:
        if opened_new_tab:
            self.close_detail()
            return
        self.detail_page = None
        if list_url and self.list_page is not None and self.list_page.url != list_url:
            try:
                self.list_page.go_back(wait_until="domcontentloaded", timeout=20000)
            except Exception:
                log("返回列表失败，改为直接打开列表")
            if self.list_page.url != list_url:
                self.goto(self.list_page, list_url)
        self.trim_tabs()

    def live_page(self, page: Any) -> Any:
        try:
            if page is not None and not page.is_closed():
                return page
        except Exception:
            pass
        if self.detail_page is not None:
            try:
                if not self.detail_page.is_closed():
                    return self.detail_page
            except Exception:
                pass
        return self.list_page

    def _page_url(self, page: Any) -> str:
        try:
            return page.url or ""
        except Exception:
            return ""

    def _show_for_verification(self, page: Any) -> Any:
        if not self.headless:
            return page
        detail = page is self.detail_page and page is not self.list_page
        urls = self._urls_to_restore(page, detail)
        log("需要手动验证，暂时打开前台窗口")
        self._reopen(headless=False, urls=urls, restore_detail=detail)
        return self.detail_page if detail and self.detail_page is not None else self.list_page

    def reveal(self, page: Any, reason: str) -> Any:
        """需要登录时把当前页放到前台，返回重新打开后的页面。"""

        if not self.headless:
            return page
        detail = page is self.detail_page and page is not self.list_page
        urls = self._urls_to_restore(page, detail)
        log(reason)
        self._reopen(headless=False, urls=urls, restore_detail=detail)
        shown = self.detail_page if detail and self.detail_page is not None else self.list_page
        return shown or page

    def restore_background(self, page: Any) -> Any:
        if not self.background or self.headless:
            return page
        detail = page is self.detail_page and page is not self.list_page
        self._return_to_background(page, "登录已完成，浏览器回到后台")
        shown = self.detail_page if detail and self.detail_page is not None else self.list_page
        return shown or page

    def _return_to_background(self, page: Any, message: str = "验证已通过，浏览器回到后台") -> None:
        if not self.background or self.headless:
            return
        detail = page is self.detail_page and page is not self.list_page
        urls = self._urls_to_restore(page, detail)
        log(message)
        self._reopen(headless=True, urls=urls, restore_detail=detail)

    def _urls_to_restore(self, page: Any, detail: bool) -> list[str]:
        current = self._page_url(page)
        list_url = self._page_url(self.list_page)
        if detail:
            return [url for url in (list_url, current) if url and not url.startswith("about:")]
        if current and not current.startswith("about:"):
            return [current]
        return []

    def _reopen(self, *, headless: bool, urls: list[str], restore_detail: bool) -> None:
        self.headless = headless
        self.close()
        self.start()
        if not urls:
            return
        self.goto(self.list_page, urls[0])
        if restore_detail and len(urls) > 1:
            self.open_detail(urls[1])

    def download_bytes(self, url: str, referer: str) -> bytes:
        response = self.context.request.get(
            url,
            timeout=30000,
            headers={"Referer": referer, "Accept": "image/avif,image/webp,image/apng,image/*,*/*"},
        )
        if not response.ok:
            raise RuntimeError(f"图片下载失败：HTTP {response.status}")
        return response.body()

    def scroll_down(self, page: Any, steps: int, pause: Callable[[], None]) -> None:
        last = -1
        for _ in range(steps):
            try:
                position = page.evaluate("() => document.scrollingElement ? document.scrollingElement.scrollTop : 0")
            except Exception:
                position = last
            if position == last and last >= 0:
                break
            last = position
            page.mouse.wheel(0, 1100)
            pause()

    def scroll_horizontal(self, locator: Any, pause: Callable[[], None]) -> None:
        last = -1.0
        for _ in range(6):
            try:
                position = locator.evaluate(
                    """el => {
                        const scroller = el.querySelector(
                            '.andes-carousel-snapped__scroll, [class*="scroll"]'
                        ) || el;
                        const before = scroller.scrollLeft || 0;
                        const delta = Math.max((scroller.clientWidth || 480) * 0.9, 280);
                        scroller.scrollLeft = before + delta;
                        return scroller.scrollLeft || 0;
                    }"""
                )
            except Exception:
                break
            if position == last:
                break
            last = float(position)
            pause()
