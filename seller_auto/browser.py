"""阿根廷语言、时区和地理位置的浏览器，以及最多两个标签。"""

from __future__ import annotations

import math
import random
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from seller_auto.browser_launch import (
    STEALTH_JS,
    apply_stealth,
    install_recaptcha_cdn_fix,
    mark_clean_shutdown,
    persistent_context_options,
    profile_is_busy,
    read_chromium_version,
    use_playwright_browsers,
)
from seller_auto.images import to_client_jpeg
from seller_auto.pacing import Cooldown, DelaySettings
from seller_auto.urls import looks_like_verification_url, text_looks_blocked

NEEDS_HUMAN = (
    "需要验证。浏览器保持无头，没有打开窗口。"
    "立刻调用一次 session_status；服务会在无头里点击「No soy un robot」、"
    "可点的「Continuar」和「No puedo resolver el desafío」。"
    "若返回 needs_captcha_selection 和格子图，调用 submit_captcha_selection "
    "提交要点的格子索引（从 0 起，按行优先）。不要打开前台窗口，不要用桌面点击过验证码。"
    "不要重复提交。"
)
STILL_NEEDS_HUMAN = (
    "自主验证还没通过，浏览器仍保持无头。"
    "若已返回格子图，调用 submit_captcha_selection；"
    "还没有图时再调一次 session_status。"
    "多次仍过不去再找用户。不要重复提交。"
)
AGENT_VERIFYING = (
    "正在无头自主验证：点击「No soy un robot」、"
    "可点的「Continuar」和「No puedo resolver el desafío」。"
)
CAPTCHA_SELECT_MESSAGE = (
    "已出现 reCAPTCHA 图片题。看题干 prompt 和每张 tile_N 图，"
    "调用 submit_captcha_selection(indices=[...], challenge_id=...) 提交要点的格子；"
    "索引从 0 起按行优先。提交后若仍返回格子图，继续选。不要用桌面点击，不要重开浏览器。"
)
CAPTCHA_FAIL_MESSAGE = (
    "图片题多次未通过。请用户稍后重试或手动处理。不要反复提交同一题，也不要打开前台窗口。"
)
_CAPTCHA_MAX_FAILS = 3
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


def _chrome_for_testing_running() -> bool:
    """本机是否还有 Google Chrome for Testing 进程。"""

    if sys.platform != "darwin":
        return True
    try:
        completed = subprocess.run(
            [
                "osascript",
                "-e",
                'tell application "System Events" to (exists process "Google Chrome for Testing")',
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0 and completed.stdout.strip().lower() == "true"


def _chrome_for_testing_visible(shown: bool) -> bool:
    """隐藏或显示正在跑的 Chrome for Testing。进程保持打开，显示时不 activate，避免抢前台。"""

    if sys.platform != "darwin":
        return False
    if shown:
        # 只设可见，不 activate，让验证像无头一样不影响用户当前窗口。
        script = 'tell application "System Events" to set visible of process "Google Chrome for Testing" to true'
    else:
        script = 'tell application "System Events" to set visible of process "Google Chrome for Testing" to false'
    try:
        completed = subprocess.run(
            ["osascript", "-e", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def context_is_alive(context: Any) -> bool:
    """持久化上下文的 browser 是空的，pages 又只是本地缓存，所以要打一次协议。"""

    if context is None:
        return False
    try:
        browser = getattr(context, "browser", None)
        if browser is not None and hasattr(browser, "is_connected") and not browser.is_connected():
            return False
        context.cookies()
        return True
    except Exception:
        return False


def browser_closed_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(
        token in text
        for token in (
            "has been closed",
            "target closed",
            "browser has been closed",
            "context has been closed",
            "processsingleton",
            "profile is already in use",
            "profile appears to be in use",
        )
    )


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
        self.pending_notice = ""
        self.captcha_challenge_id = ""
        self.captcha_fail_count = 0
        self._window_shown: bool | None = None

    def start(self) -> None:
        if self.context is not None:
            if context_is_alive(self.context):
                return
            log("浏览器已断开，重新打开")
            self._abandon()
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
        apply_stealth(context)
        install_recaptcha_cdn_fix(context)
        context.add_init_script(STEALTH_JS)
        pages = list(self.context.pages)
        self.list_page = pages[0] if pages else self.context.new_page()
        self.detail_page = None
        self.trim_tabs()
        self._configure(self.list_page)
        self._window_shown = None if self.headless else True
        if self.headless:
            log("已在后台启动完整 Chrome，没有打开窗口。")
        else:
            log("已打开前台窗口。")

    def _wait_until_profile_free(self) -> None:
        deadline = time.monotonic() + 8
        while profile_is_busy(self.profile_dir) and time.monotonic() < deadline:
            time.sleep(0.2)

    def _abandon(self) -> None:
        context = self.context
        playwright = self._playwright
        self.context = None
        self._playwright = None
        self.list_page = None
        self.detail_page = None
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                pass

    def close(self) -> None:
        self._abandon()

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
            if page.locator("img.poly-component__picture, .poly-card__portada img").count() >= 4:
                return False
        except Exception:
            pass
        try:
            url = page.url or ""
        except Exception:
            return False
        if looks_like_verification_url(url):
            log(f"页面地址像验证页：{url}")
            return True
        for selector in VERIFICATION_SELECTORS:
            try:
                if page.locator(selector).count() > 0:
                    log(f"页面里有验证控件：{selector}")
                    return True
            except Exception:
                continue
        try:
            snippet = page.evaluate(
                "() => ((document.body && document.body.innerText) || '').slice(0, 1500)"
            )
        except Exception:
            snippet = ""
        if text_looks_blocked(snippet):
            log("页面文字像验证页")
            return True
        return False

    def ensure_clear(self, page: Any, site: str, *, present: bool = False) -> dict[str, Any] | None:
        if not self.needs_verification(page):
            if self.background and not self.headless:
                self._return_to_background(page)
            return self._reward_if_cleared(site)
        self.challenge_pending = True
        self.challenge_site = site
        if self.background and not present:
            log("需要验证。后台保持无头，不打开窗口")
            return {
                "ok": False,
                "status": "needs_human_verification",
                "message": NEEDS_HUMAN,
                "url": self._page_url(page),
            }
        # 验证全程无头：若之前被切成有头，先改回无头，绝不弹前台。
        page = self._ensure_headless_for_verification(page)
        if self._has_recaptcha_image_challenge(page):
            log("已有 reCAPTCHA 图片题，截格交给模型点选")
            captured = self._image_challenge_payload(page)
            if captured.get("status") == "needs_captcha_selection":
                return captured
            log("图片题 iframe 在，但格子还没截到，继续点勾选框")
        log(AGENT_VERIFYING)
        self._attempt_verification_clicks(page)
        time.sleep(2)
        if not self.needs_verification(page):
            return self._reward_if_cleared(site)
        if self._has_recaptcha_image_challenge(page):
            log("已出现 reCAPTCHA 图片题，截格交给模型点选")
            captured = self._image_challenge_payload(page)
            if captured.get("status") == "needs_captcha_selection":
                return captured
        if self._wait_until_clear(page, timeout=25, attempt=True):
            return self._reward_if_cleared(site)
        if self._has_recaptcha_image_challenge(page):
            captured = self._image_challenge_payload(page)
            if captured.get("status") == "needs_captcha_selection":
                return captured
        log("自主验证还没完成，保持无头")
        return {
            "ok": False,
            "status": "needs_human_verification",
            "message": STILL_NEEDS_HUMAN,
            "url": self._page_url(page),
        }

    def _ensure_headless_for_verification(self, page: Any) -> Any:
        """验证一律在无头里做，若之前被切成有头则改回无头，不弹前台。"""

        if self.headless:
            return page
        detail = page is self.detail_page and page is not self.list_page
        urls = self._urls_to_restore(page, detail)
        log("验证改回无头，关闭前台窗口")
        self._reopen(headless=True, urls=urls, restore_detail=detail)
        self._window_shown = False
        return self.detail_page if detail and self.detail_page is not None else self.list_page

    def _reward_if_cleared(self, site: str) -> None:
        if not self.challenge_pending:
            return None
        self.challenge_pending = False
        cleared_site = self.challenge_site or site
        self.challenge_site = ""
        self.captcha_challenge_id = ""
        self.captcha_fail_count = 0
        if cleared_site == "ml":
            extra = random.uniform(self.settings.ml_captcha_min, self.settings.ml_captcha_max)
            self.ml_cooldown.penalize(extra)
            log(f"验证已通过，下一次美客多导航额外等待 {extra:.0f} 秒")
        return None

    def _wait_until_clear(self, page: Any, timeout: float, *, attempt: bool = False) -> bool:
        deadline = time.monotonic() + timeout
        next_reload = time.monotonic() + 20
        while time.monotonic() < deadline:
            if not self.needs_verification(page):
                return True
            if attempt:
                # 图片题已经出来就停手，不要刷新把题目冲掉；由调用方截格返回。
                if self._has_recaptcha_image_challenge(page):
                    log("已出现 reCAPTCHA 图片题，停止自动点击")
                    return False
                self._attempt_verification_clicks(page)
                if (
                    time.monotonic() >= next_reload
                    and self.needs_verification(page)
                    and not self._has_recaptcha_image_challenge(page)
                ):
                    self._reload_verification(page)
                    next_reload = time.monotonic() + 20
            time.sleep(2)
        return not self.needs_verification(page)

    def _has_recaptcha_image_challenge(self, page: Any) -> bool:
        return self._recaptcha_bframe(page) is not None

    def _recaptcha_bframe(self, page: Any) -> Any | None:
        target = self.live_page(page) or page
        try:
            frames = list(target.frames)
        except Exception:
            return None
        for frame in frames:
            try:
                url = frame.url or ""
            except Exception:
                continue
            if "recaptcha" in url and "bframe" in url:
                return frame
        return None

    def _image_challenge_payload(self, page: Any) -> dict[str, Any]:
        captured = self.capture_recaptcha_challenge(page)
        if not captured or not captured.get("image_bytes"):
            return {
                "ok": False,
                "status": "needs_human_verification",
                "message": STILL_NEEDS_HUMAN,
                "url": self._page_url(page),
            }
        return {
            "ok": False,
            "status": "needs_captcha_selection",
            "message": CAPTCHA_SELECT_MESSAGE,
            "prompt": captured.get("prompt") or "",
            "tile_count": captured.get("tile_count") or 0,
            "grid": captured.get("grid") or "",
            "challenge_id": captured.get("challenge_id") or "",
            "url": self._page_url(page),
            "image_bytes": captured["image_bytes"],
        }

    def capture_recaptcha_challenge(self, page: Any) -> dict[str, Any] | None:
        """截 bframe 题干和格子，供模型点选。返回 JPEG bytes，不写盘。"""

        frame = self._recaptcha_bframe(page)
        if frame is None:
            log("截验证码失败：没有 bframe")
            return None
        try:
            frame_url = frame.url or ""
        except Exception:
            frame_url = ""
        tiles = self._wait_for_recaptcha_tiles(frame, timeout=8)
        if tiles is None:
            self._log_recaptcha_frame_debug(frame, frame_url)
            return None
        try:
            count = tiles.count()
        except Exception as exc:
            log(f"截验证码失败：读格子数量出错：{exc}")
            return None
        if count <= 0:
            self._log_recaptcha_frame_debug(frame, frame_url)
            return None
        prompt = self._recaptcha_prompt(frame)
        image_bytes: list[tuple[str, bytes]] = []
        def _shot(locator: Any, label: str) -> bytes | None:
            try:
                return locator.screenshot(timeout=2500, animations="disabled")
            except TypeError:
                try:
                    return locator.screenshot(timeout=2500)
                except Exception as exc:
                    log(f"截取验证码{label}失败：{exc}")
                    return None
            except Exception as exc:
                log(f"截取验证码{label}失败：{exc}")
                return None

        try:
            grid_node = frame.locator("#rc-imageselect-target, .rc-imageselect-target, table").first
            if grid_node.count() > 0:
                grid_png = _shot(grid_node, "全图")
                if grid_png:
                    image_bytes.append(("grid", to_client_jpeg(grid_png)))
        except Exception as exc:
            log(f"截取验证码全图失败：{exc}")
        for index in range(count):
            png = _shot(tiles.nth(index), f"格子 {index}")
            if png:
                image_bytes.append((f"tile_{index}", to_client_jpeg(png)))
        if not any(caption.startswith("tile_") for caption, _ in image_bytes):
            # 单格截图全失败时，至少交全图给模型，索引仍按 DOM 顺序。
            if not image_bytes:
                png = _shot(frame.locator("body").first, "整页")
                if png is None:
                    log("截验证码失败：整页也截不到")
                    return None
                image_bytes.append(("grid", to_client_jpeg(png)))
                log("单格截图失败，改用 bframe 整页截图")
            else:
                log("单格截图失败，仅有全图，仍返回给模型")
            grid_jpeg = image_bytes[0][1]
            for index in range(count):
                # 占位说明：模型看 grid，提交索引仍有效。
                image_bytes.append((f"tile_{index}", grid_jpeg))
        challenge_id = uuid.uuid4().hex[:12]
        self.captcha_challenge_id = challenge_id
        side = int(math.isqrt(count))
        grid = f"{side}x{side}" if side * side == count else f"1x{count}"
        log(f"已截 reCAPTCHA 图片题 {grid}，challenge_id={challenge_id}，prompt={prompt[:80]!r}")
        return {
            "prompt": prompt,
            "tile_count": count,
            "grid": grid,
            "challenge_id": challenge_id,
            "image_bytes": image_bytes,
        }

    def _wait_for_recaptcha_tiles(self, frame: Any, timeout: float = 8) -> Any | None:
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            tiles = self._recaptcha_tiles(frame)
            if tiles is not None:
                try:
                    if tiles.count() > 0:
                        return tiles
                except Exception as exc:
                    last = exc
            time.sleep(0.4)
        if last is not None:
            log(f"等待验证码格子超时：{last}")
        return None

    def _log_recaptcha_frame_debug(self, frame: Any, frame_url: str) -> None:
        counts: dict[str, int] = {}
        for selector in (
            ".rc-imageselect-tile",
            "#rc-imageselect-target td",
            ".rc-image-tile-wrapper",
            ".rc-imageselect-desc-text",
            "img",
            "table",
            "button",
        ):
            try:
                counts[selector] = int(frame.locator(selector).count())
            except Exception:
                counts[selector] = -1
        snippet = ""
        try:
            snippet = str(frame.evaluate("() => (document.body && document.body.innerText || '').slice(0, 300)") or "")
        except Exception as exc:
            snippet = f"<evaluate failed: {exc}>"
        log(f"验证码格子未就绪 url={frame_url!r} counts={counts} text={snippet!r}")

    def apply_captcha_selection(
        self,
        page: Any,
        indices: list[int],
        challenge_id: str = "",
    ) -> dict[str, Any] | None:
        """按模型给出的格子索引点选并 VERIFICAR。通过返回 None，否则返回 blocked 字典。"""

        page = self._ensure_headless_for_verification(page)
        site = self.challenge_site or "ml"
        if not self.needs_verification(page):
            return self._reward_if_cleared(site)
        if challenge_id and self.captcha_challenge_id and challenge_id != self.captcha_challenge_id:
            log("challenge_id 已过期，重新截图")
            if self._has_recaptcha_image_challenge(page):
                return self._image_challenge_payload(page)
            return {
                "ok": False,
                "status": "needs_human_verification",
                "message": STILL_NEEDS_HUMAN,
                "url": self._page_url(page),
            }
        if not self._has_recaptcha_image_challenge(page):
            self._attempt_verification_clicks(page)
            time.sleep(2)
            if not self.needs_verification(page):
                return self._reward_if_cleared(site)
            if self._has_recaptcha_image_challenge(page):
                return self._image_challenge_payload(page)
            return {
                "ok": False,
                "status": "needs_human_verification",
                "message": STILL_NEEDS_HUMAN,
                "url": self._page_url(page),
            }
        frame = self._recaptcha_bframe(page)
        tiles = self._recaptcha_tiles(frame) if frame is not None else None
        if frame is None or tiles is None:
            return {
                "ok": False,
                "status": "needs_human_verification",
                "message": STILL_NEEDS_HUMAN,
                "url": self._page_url(page),
            }
        try:
            count = tiles.count()
        except Exception:
            count = 0
        chosen = sorted({int(index) for index in indices if isinstance(index, (int, float)) or str(index).isdigit()})
        chosen = [int(index) for index in chosen if 0 <= int(index) < max(count, 1)]
        for index in chosen:
            try:
                tiles.nth(index).click(timeout=2000, force=True)
                log(f"已点验证码格子 tile_{index}")
                time.sleep(0.35)
            except Exception as exc:
                log(f"点击验证码格子 {index} 失败：{exc}")
        if not self._click_recaptcha_verify(frame):
            log("未点到 VERIFICAR")
        time.sleep(2.5)
        if not self.needs_verification(page):
            self.captcha_fail_count = 0
            self.captcha_challenge_id = ""
            return self._reward_if_cleared(site)
        if self._has_recaptcha_image_challenge(page):
            self.captcha_fail_count += 1
            if self.captcha_fail_count >= _CAPTCHA_MAX_FAILS:
                log("图片题连续失败次数过多")
                return {
                    "ok": False,
                    "status": "needs_human_verification",
                    "message": CAPTCHA_FAIL_MESSAGE,
                    "url": self._page_url(page),
                }
            log("图片题未过或换了新题，重新截格")
            return self._image_challenge_payload(page)
        self._attempt_verification_clicks(page)
        time.sleep(2)
        if not self.needs_verification(page):
            self.captcha_fail_count = 0
            self.captcha_challenge_id = ""
            return self._reward_if_cleared(site)
        if self._has_recaptcha_image_challenge(page):
            return self._image_challenge_payload(page)
        return {
            "ok": False,
            "status": "needs_human_verification",
            "message": STILL_NEEDS_HUMAN,
            "url": self._page_url(page),
        }

    def _recaptcha_prompt(self, frame: Any) -> str:
        selectors = (
            ".rc-imageselect-desc-text",
            ".rc-imageselect-desc-wrapper",
            ".rc-imageselect-desc",
            "#rc-imageselect",
        )
        for selector in selectors:
            try:
                node = frame.locator(selector).first
                if node.count() == 0:
                    continue
                text = (node.inner_text(timeout=1000) or "").strip()
                if text:
                    return " ".join(text.split())
            except Exception:
                continue
        return ""

    def _recaptcha_tiles(self, frame: Any) -> Any | None:
        for selector in (
            ".rc-imageselect-tile",
            "#rc-imageselect-target td",
            "table.rc-imageselect-table-33 td",
            "table.rc-imageselect-table-44 td",
            ".rc-image-tile-wrapper",
            ".rc-imageselect-target td",
        ):
            try:
                tiles = frame.locator(selector)
                if tiles.count() > 0:
                    return tiles
            except Exception:
                continue
        return None

    def _click_recaptcha_verify(self, frame: Any) -> bool:
        selectors = (
            "#recaptcha-verify-button",
            "button:has-text('VERIFICAR')",
            "button:has-text('Verify')",
        )
        for selector in selectors:
            try:
                button = frame.locator(selector).first
                if button.count() == 0:
                    continue
                button.click(timeout=2000, force=True)
                log("已点击 VERIFICAR")
                return True
            except Exception:
                continue
        return False

    def _reload_verification(self, page: Any) -> None:
        """挑战控件一直不出时，后台刷新验证页再等，不抢前台。"""

        target = self.live_page(page) or page
        try:
            log("验证挑战仍未就绪，后台刷新验证页")
            target.reload(wait_until="commit", timeout=30000)
            time.sleep(2)
        except Exception:
            pass

    def _attempt_verification_clicks(self, page: Any) -> None:
        """自主验证：先点 reCAPTCHA「No soy un robot」，再点可点的继续/跳过。"""

        target = self.live_page(page) or page
        self._attempt_recaptcha_checkbox(target)
        self._attempt_enabled_button(target, "Continuar")
        self._attempt_enabled_button(target, "No puedo resolver el desafío")

    def _attempt_recaptcha_checkbox(self, page: Any) -> bool:
        """点 anchor iframe 里的「No soy un robot」。跨域 iframe 只能走 frame API。"""

        selectors = (
            "#recaptcha-anchor",
            ".recaptcha-checkbox-border",
            ".recaptcha-checkbox",
            '[role="checkbox"]',
        )
        frames = []
        try:
            frames = list(page.frames)
        except Exception:
            frames = []
        for frame in frames:
            url = ""
            try:
                url = frame.url or ""
            except Exception:
                continue
            if "recaptcha" not in url or "anchor" not in url:
                continue
            for selector in selectors:
                try:
                    box = frame.locator(selector).first
                    if box.count() == 0:
                        continue
                    checked = False
                    try:
                        checked = bool(box.get_attribute("aria-checked") == "true")
                    except Exception:
                        checked = False
                    if checked:
                        log("reCAPTCHA 勾选框已经勾上")
                        return True
                    box.click(timeout=2000, force=True)
                    log("已点击 No soy un robot 勾选框")
                    time.sleep(1.5)
                    return True
                except Exception:
                    continue
        try:
            anchor = page.frame_locator('iframe[src*="recaptcha"][src*="anchor"]')
            box = anchor.locator("#recaptcha-anchor, .recaptcha-checkbox-border, [role='checkbox']").first
            box.click(timeout=2000, force=True)
            log("已通过 frame_locator 点击 No soy un robot")
            time.sleep(1.5)
            return True
        except Exception as exc:
            log(f"点击 No soy un robot 失败：{exc}")
            return False

    def _attempt_enabled_button(self, page: Any, label: str) -> bool:
        try:
            info = page.evaluate(
                """(label) => {
                  const nodes = Array.from(document.querySelectorAll('button, a, [role="button"]'));
                  const el = nodes.find((n) => (n.textContent || '').includes(label));
                  if (!el) return { found: false };
                  const style = window.getComputedStyle(el);
                  return {
                    found: true,
                    tag: el.tagName,
                    disabledAttr: el.hasAttribute('disabled'),
                    ariaDisabled: el.getAttribute('aria-disabled'),
                    className: el.className,
                    pointerEvents: style.pointerEvents,
                    color: style.color,
                    cursor: style.cursor,
                    opacity: style.opacity,
                  };
                }""",
                label,
            )
        except Exception as exc:
            log(f"读取按钮「{label}」状态失败：{exc}")
            return False
        if not info or not info.get("found"):
            return False
        disabled_attr = bool(info.get("disabledAttr"))
        aria_disabled = str(info.get("ariaDisabled") or "").lower() == "true"
        log(
            f"按钮「{label}」状态："
            f"tag={info.get('tag')} disabledAttr={disabled_attr} ariaDisabled={info.get('ariaDisabled')!r} "
            f"color={info.get('color')} cursor={info.get('cursor')} pointerEvents={info.get('pointerEvents')} "
            f"opacity={info.get('opacity')} class={info.get('className')!r}"
        )
        if disabled_attr or aria_disabled:
            return False
        try:
            target = page.locator(
                f"button:has-text('{label}'), a:has-text('{label}'), [role='button']:has-text('{label}')"
            ).first
            target.click(timeout=1500, force=True)
            log(f"已点击 {label}")
            time.sleep(1.5)
            return True
        except Exception as exc:
            log(f"点击「{label}」失败：{exc}")
            return False

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
        detail = page is self.detail_page and page is not self.list_page
        urls = self._urls_to_restore(page, detail)
        alive = context_is_alive(self.context) and _chrome_for_testing_running()
        if not self.headless and alive:
            self._reveal_window(page)
            if _chrome_for_testing_running() and self._activate_chrome():
                return page
            log("前台窗口已经不在，重新打开")
        else:
            log("需要手动验证，暂时打开前台窗口")
        self._reopen(headless=False, urls=urls, restore_detail=detail)
        self._window_shown = True
        self._activate_chrome()
        return self.detail_page if detail and self.detail_page is not None else self.list_page

    def _activate_chrome(self) -> bool:
        """验证需要用户操作时，把 Chrome for Testing 调到前台。"""

        if sys.platform != "darwin":
            return True
        try:
            completed = subprocess.run(
                [
                    "osascript",
                    "-e",
                    'tell application "Google Chrome for Testing" to activate',
                    "-e",
                    'tell application "System Events" to set visible of process "Google Chrome for Testing" to true',
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return completed.returncode == 0

    def reveal(self, page: Any, reason: str) -> Any:
        """需要登录时把当前页放到前台。已经打开的浏览器只把窗口调出来。"""

        if not self.headless:
            if self._window_shown is not True:
                log(reason)
            self._reveal_window(page)
            return page
        detail = page is self.detail_page and page is not self.list_page
        urls = self._urls_to_restore(page, detail)
        log(reason)
        self._reopen(headless=False, urls=urls, restore_detail=detail)
        self._window_shown = True
        shown = self.detail_page if detail and self.detail_page is not None else self.list_page
        return shown or page

    def restore_background(self, page: Any) -> Any:
        if not self.background or self.headless:
            return page
        detail = page is self.detail_page and page is not self.list_page
        self._return_to_background(page, "登录已完成，窗口已隐藏，浏览器没有重启")
        shown = self.detail_page if detail and self.detail_page is not None else self.list_page
        return shown or page

    def _return_to_background(self, page: Any, message: str = "验证已通过，窗口已隐藏，浏览器没有重启") -> None:
        # 放行在这只浏览器里。关掉再开无头进程，后面的商品会再次碰到验证。
        if not self.background or self.headless or self._window_shown is False:
            return
        log(message)
        self._conceal_window(page)

    def _reveal_window(self, page: Any) -> None:
        if self.headless:
            return
        if sys.platform == "win32":
            from seller_auto.browser_launch import show_chrome_windows

            show_chrome_windows(self.profile_dir)
            self._window_shown = True
            return
        # 只保证窗口存在且可见，不 activate / bring_to_front，避免打断用户前台工作。
        shown = _chrome_for_testing_visible(True)
        placed = self._place_bounds(page, shown=True)
        if shown or placed:
            self._window_shown = True
            return
        if self._window_shown is True:
            return
        log("打开验证窗口失败，浏览器没有重启")

    def _conceal_window(self, page: Any) -> None:
        if sys.platform == "win32":
            from seller_auto.browser_launch import hide_chrome_windows

            hide_chrome_windows(self.profile_dir)
            self._window_shown = False
            return
        if _chrome_for_testing_visible(False) or self._place_bounds(page, shown=False):
            self._window_shown = False
            return
        log("隐藏窗口失败，浏览器保持打开，没有重启")

    def _place_bounds(self, page: Any, *, shown: bool) -> bool:
        target = self.live_page(page)
        if target is None:
            return False
        session = None
        try:
            session = target.context.new_cdp_session(target)
            window_id = session.send("Browser.getWindowForTarget")["windowId"]
            if shown:
                bounds = {"left": 40, "top": 40, "width": 1440, "height": 900, "windowState": "normal"}
            else:
                bounds = {"windowState": "minimized"}
            session.send("Browser.setWindowBounds", {"windowId": window_id, "bounds": bounds})
        except Exception:
            return False
        finally:
            if session is not None:
                try:
                    session.detach()
                except Exception:
                    pass
        return True

    def _export_cookies(self) -> list[dict[str, Any]]:
        if self.context is None:
            return []
        try:
            cookies = self.context.cookies()
        except Exception:
            log("读取登录状态失败")
            return []
        return list(cookies or [])

    def _import_cookies(self, cookies: list[dict[str, Any]]) -> None:
        if not cookies or self.context is None:
            return
        try:
            self.context.add_cookies(cookies)
        except Exception:
            log("写回登录状态失败")

    def _urls_to_restore(self, page: Any, detail: bool) -> list[str]:
        current = self._page_url(page)
        list_url = self._page_url(self.list_page)
        if detail:
            return [url for url in (list_url, current) if url and not url.startswith("about:")]
        if current and not current.startswith("about:"):
            return [current]
        return []

    def _reopen(self, *, headless: bool, urls: list[str], restore_detail: bool) -> None:
        cookies = self._export_cookies()
        self.headless = headless
        self.close()
        self.start()
        self._import_cookies(cookies)
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
