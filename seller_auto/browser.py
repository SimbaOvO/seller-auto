"""阿根廷语言、时区和地理位置的浏览器，以及最多两个标签。"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any, Callable

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
    import sys

    print(message, file=sys.stderr, flush=True)


class ArgentinaBrowser:
    def __init__(self, settings: DelaySettings, profile_dir: Path, headless: bool = False) -> None:
        self.settings = settings
        self.profile_dir = profile_dir
        self.headless = headless
        self.amazon_cooldown = Cooldown(settings.amazon_min_delay, settings.amazon_max_delay)
        self.ml_cooldown = Cooldown(settings.ml_min_delay, settings.ml_max_delay)
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

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        self.context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            headless=self.headless,
            locale="es-AR",
            timezone_id="America/Argentina/Buenos_Aires",
            geolocation={"latitude": -34.6037, "longitude": -58.3816},
            permissions=["geolocation"],
            viewport={"width": 1440, "height": 900},
            extra_http_headers={"Accept-Language": "es-AR,es;q=0.9"},
        )
        pages = list(self.context.pages)
        self.list_page = pages[0] if pages else self.context.new_page()
        self.detail_page = None
        self.trim_tabs()
        self._configure(self.list_page)

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
        if self.detail_page is not None and self.detail_page is not self.list_page:
            try:
                if not self.detail_page.is_closed():
                    self.detail_page.close()
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
            return self._reward_if_cleared(site)
        self.challenge_pending = True
        self.challenge_site = site
        log("需要手动验证，请在弹出的浏览器里完成")
        if self._wait_until_clear(page, timeout=90):
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

    def goto(self, page: Any, url: str) -> None:
        page.goto(url, wait_until="domcontentloaded")

    def open_detail(self, url: str) -> Any:
        self.start()
        self.close_detail()
        page = self.context.new_page()
        self.detail_page = page
        self._configure(page)
        self.trim_tabs()
        self.goto(page, url)
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
