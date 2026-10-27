"""后台启动完整 Chrome for Testing。去掉自动化标记，不改画布、WebGL 或操作系统。"""

from __future__ import annotations

import json
import os
import plistlib
from pathlib import Path
from typing import Any

# 和 mercadolibre-login 一样：隐藏 webdriver、HeadlessChrome，并让后台页面保持可见。
STEALTH_JS = """
try { Object.defineProperty(navigator, 'webdriver', { get: () => undefined }); } catch (e) {}
try {
  const ua = navigator.userAgent.replace(/HeadlessChrome/g, "Chrome");
  if (ua !== navigator.userAgent) Object.defineProperty(navigator, "userAgent", { configurable: true, get: () => ua });
} catch (e) {}
try {
  Object.defineProperty(document, 'hidden', { configurable: true, get: () => false });
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'visible' });
} catch (e) {}
""".strip()

LAUNCH_ARGS = [
    "--no-first-run",
    "--no-default-browser-check",
    "--hide-crash-restore-bubble",
    "--disable-session-crashed-bubble",
    "--disable-blink-features=AutomationControlled",
    "--window-size=1440,900",
    "--lang=es-AR",
]

CHROME_FOR_TESTING = (
    "chromium-*/chrome-mac-*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
)


def default_playwright_browsers_dir() -> Path:
    """Playwright 未设置 PLAYWRIGHT_BROWSERS_PATH 时使用的目录。"""

    return Path.home() / "Library/Caches/ms-playwright"


def is_sandbox_browsers_path(path: str | Path) -> bool:
    return "cursor-sandbox-cache" in str(path)


def chrome_for_testing_binary(browsers_dir: Path) -> Path | None:
    matches = sorted(browsers_dir.glob(CHROME_FOR_TESTING))
    return matches[-1] if matches else None


def playwright_browsers_dir() -> Path:
    """先用 Playwright 自己的浏览器目录，不用 Cursor 沙箱缓存。"""

    default = default_playwright_browsers_dir()
    if chrome_for_testing_binary(default):
        return default
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
    if env and env != "0" and not is_sandbox_browsers_path(env):
        return Path(env)
    return default


def use_playwright_browsers() -> Path:
    """覆盖沙箱里的 PLAYWRIGHT_BROWSERS_PATH，让 Playwright 去自己的目录找浏览器。"""

    browsers = playwright_browsers_dir()
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers)
    return browsers


def desktop_chrome_user_agent(version: str) -> str:
    """本机是 macOS，请求头用普通桌面 Chrome，去掉 HeadlessChrome。"""

    return (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{version} Safari/537.36"
    )


def read_chromium_version(browsers_dir: Path) -> str | None:
    plists = sorted(
        browsers_dir.glob("chromium-*/chrome-mac-*/Google Chrome for Testing.app/Contents/Info.plist")
    )
    if not plists:
        return None
    try:
        info = plistlib.loads(plists[-1].read_bytes())
    except (OSError, plistlib.InvalidFileException):
        return None
    version = info.get("CFBundleShortVersionString") or info.get("CFBundleVersion")
    return str(version) if version else None


def profile_lock_pid(profile: Path) -> int | None:
    try:
        target = os.readlink(profile / "SingletonLock")
    except OSError:
        return None
    tail = target.rsplit("-", 1)[-1]
    return int(tail) if tail.isdigit() else None


def profile_is_busy(profile: Path) -> bool:
    pid = profile_lock_pid(profile)
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def mark_clean_shutdown(profile: Path) -> bool:
    """清掉异常退出标记，避免下次打开时弹出恢复页面。"""

    if profile_is_busy(profile):
        return False
    prefs_path = profile / "Default" / "Preferences"
    try:
        prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    profile_prefs = prefs.get("profile")
    if not isinstance(profile_prefs, dict):
        profile_prefs = {}
    if profile_prefs.get("exit_type") == "Normal" and profile_prefs.get("exited_cleanly") is True:
        return False
    profile_prefs["exit_type"] = "Normal"
    profile_prefs["exited_cleanly"] = True
    prefs["profile"] = profile_prefs
    temporary = prefs_path.with_name(f"{prefs_path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(prefs), encoding="utf-8")
    temporary.replace(prefs_path)
    return True


def persistent_context_options(
    profile_dir: Path,
    *,
    headless: bool,
    chrome_version: str | None = None,
    locale: str = "es-AR",
    accept_language: str = "es-AR,es;q=0.9",
    timezone_id: str = "America/Argentina/Buenos_Aires",
    geolocation: dict[str, float] | None = None,
) -> dict[str, Any]:
    args = [item for item in LAUNCH_ARGS if not item.startswith("--lang=")]
    args.append(f"--lang={locale}")
    options: dict[str, Any] = {
        "user_data_dir": str(profile_dir.resolve()),
        "headless": headless,
        "locale": locale,
        "timezone_id": timezone_id,
        "geolocation": geolocation or {"latitude": -34.6037, "longitude": -58.3816},
        "permissions": ["geolocation"],
        "viewport": {"width": 1440, "height": 900},
        "extra_http_headers": {"Accept-Language": accept_language},
        "ignore_default_args": ["--enable-automation"],
        "args": args,
    }
    if headless:
        # 不传 channel 时，Playwright 会改用精简的 headless shell。
        options["channel"] = "chrome-for-testing"
        if chrome_version:
            options["user_agent"] = desktop_chrome_user_agent(chrome_version)
    return options
