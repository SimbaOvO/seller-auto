"""后台启动完整 Chrome for Testing。去掉自动化标记，不改画布、WebGL 或操作系统。"""

from __future__ import annotations

import json
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

from seller_auto.step_log import step

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

_CHROME_GLOBS = (
    "chromium-*/chrome-mac-*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
    "chromium-*/chrome-win64/chrome.exe",
    "chromium-*/chrome-win/chrome.exe",
)
_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+\.\d+)")
_LOGIN_FILES = frozenset(
    {
        "Local State",
        "Default/Preferences",
        "Default/Cookies",
        "Default/Network/Cookies",
        "Default/Login Data",
    }
)


def default_playwright_browsers_dir() -> Path:
    """Playwright 未设置 PLAYWRIGHT_BROWSERS_PATH 时使用的目录。"""

    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            return Path(local) / "ms-playwright"
        return Path.home() / "AppData" / "Local" / "ms-playwright"
    if sys.platform == "darwin":
        return Path.home() / "Library/Caches/ms-playwright"
    return Path.home() / ".cache" / "ms-playwright"


def playwright_install_command() -> str:
    if sys.platform == "win32":
        return r".venv\Scripts\python.exe -m playwright install chromium"
    return ".venv/bin/python -m playwright install chromium"


def _chromium_revision(path: Path) -> int:
    for part in path.parts:
        if not part.startswith("chromium-"):
            continue
        digits = ""
        for character in part.removeprefix("chromium-"):
            if not character.isdigit():
                break
            digits += character
        if digits:
            return int(digits)
    return 0


def is_sandbox_browsers_path(path: str | Path) -> bool:
    return "cursor-sandbox-cache" in str(path)


def chrome_for_testing_binary(browsers_dir: Path) -> Path | None:
    matches: list[Path] = []
    for pattern in _CHROME_GLOBS:
        matches.extend(browsers_dir.glob(pattern))
    if not matches:
        return None
    return max(matches, key=lambda path: (_chromium_revision(path), str(path)))


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


def desktop_chrome_user_agent(version: str, system: str | None = None) -> str:
    """请求头用本机桌面 Chrome，去掉 HeadlessChrome。"""

    if (system or sys.platform) == "win32":
        return (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{version} Safari/537.36"
        )
    return (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{version} Safari/537.36"
    )


def read_chromium_version(browsers_dir: Path) -> str | None:
    plists = sorted(
        browsers_dir.glob("chromium-*/chrome-mac-*/Google Chrome for Testing.app/Contents/Info.plist"),
        key=_chromium_revision,
    )
    if plists:
        try:
            info = plistlib.loads(plists[-1].read_bytes())
        except (OSError, plistlib.InvalidFileException):
            info = None
        if isinstance(info, dict):
            version = info.get("CFBundleShortVersionString") or info.get("CFBundleVersion")
            if version:
                return str(version)
    binary = chrome_for_testing_binary(browsers_dir)
    if binary is None:
        return None
    return _version_from_binary(binary)


def _version_from_binary(binary: Path) -> str | None:
    kwargs: dict[str, Any] = {
        "capture_output": True,
        "text": True,
        "timeout": 15,
        "check": False,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    try:
        completed = subprocess.run([str(binary), "--version"], **kwargs)
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = _VERSION_RE.search(completed.stdout or completed.stderr or "")
    return match.group(1) if match else None


def profile_lock_pid(profile: Path) -> int | None:
    if sys.platform == "win32":
        return None
    try:
        target = os.readlink(profile / "SingletonLock")
    except OSError:
        return None
    tail = target.rsplit("-", 1)[-1]
    return int(tail) if tail.isdigit() else None


def _windows_profile_locked(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        handle = open(path, "r+b")
    except OSError:
        return True
    try:
        import msvcrt

        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return True
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return False
    finally:
        handle.close()


def profile_is_busy(profile: Path) -> bool:
    if sys.platform == "win32":
        return _windows_profile_locked(profile / "lockfile")
    pid = profile_lock_pid(profile)
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _skip_profile_entry(name: str) -> bool:
    if name.startswith("Singleton") or name == "lockfile":
        return True
    return name in {
        "Cache",
        "Code Cache",
        "GPUCache",
        "GrShaderCache",
        "GraphiteDawnCache",
        "DawnGraphiteCache",
        "DawnWebGPUCache",
        "Service Worker",
        "Crashpad",
        "BrowserMetrics",
        "ShaderCache",
    }


def _remove_tree(path: Path) -> None:
    def _on_remove(func: Any, failed: str, _exc: BaseException) -> None:
        try:
            os.chmod(failed, stat.S_IWRITE)
            func(failed)
        except OSError:
            pass

    shutil.rmtree(path, onexc=_on_remove)


def snapshot_profile(source: Path, destination: Path) -> Path:
    """复制登录态到另一个目录。不带进程锁，第二只 Chrome 就不会关掉正在用的那只。"""

    if source.resolve() == destination.resolve():
        return destination
    if destination.exists() and profile_is_busy(destination):
        step("资料", "回填配置仍在使用，沿用现有目录")
        return destination
    if destination.exists():
        _remove_tree(destination)
    destination.mkdir(parents=True, exist_ok=True)
    if not source.is_dir():
        return destination
    missed: list[str] = []
    for dirpath, dirnames, filenames in os.walk(source):
        relative = Path(dirpath).relative_to(source)
        dirnames[:] = [name for name in dirnames if not _skip_profile_entry(name)]
        target_dir = destination / relative
        target_dir.mkdir(parents=True, exist_ok=True)
        for name in filenames:
            if _skip_profile_entry(name):
                continue
            relative_file = (relative / name).as_posix()
            try:
                shutil.copy2(Path(dirpath) / name, target_dir / name)
            except OSError:
                if relative_file in _LOGIN_FILES:
                    missed.append(relative_file)
                continue
    if missed:
        step("资料", "这些登录文件没有复制过去，回填浏览器需要重新登录：" + "、".join(missed))
    return destination


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
    try:
        temporary.replace(prefs_path)
    except OSError:
        temporary.unlink(missing_ok=True)
        return False
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
