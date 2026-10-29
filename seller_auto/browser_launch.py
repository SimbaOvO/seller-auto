"""用 Playwright 启动完整 Chrome for Testing。不使用 headless，去掉 webdriver 标记。"""

from __future__ import annotations

import base64
import json
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, NamedTuple

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
    """Windows 只读 exe 里的版本资源。

    再执行一次 chrome.exe --version 时，已经在跑的 Chrome 会把这次启动接走，
    中文系统又按 GBK 解码输出。
    """

    version = _windows_file_version(binary)
    if version or sys.platform == "win32":
        return version
    try:
        completed = subprocess.run(
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        return None
    match = _VERSION_RE.search(completed.stdout or completed.stderr or "")
    return match.group(1) if match else None


def _windows_file_version(binary: Path) -> str | None:
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return None
    version = ctypes.WinDLL("version", use_last_error=True)
    version.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    version.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    version.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    version.GetFileVersionInfoW.restype = wintypes.BOOL
    version.VerQueryValueW.argtypes = [
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.UINT),
    ]
    version.VerQueryValueW.restype = wintypes.BOOL
    size = version.GetFileVersionInfoSizeW(str(binary), None)
    if not size:
        return None
    buffer = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(str(binary), 0, size, buffer):
        return None
    pointer = ctypes.c_void_p()
    length = wintypes.UINT()
    if not version.VerQueryValueW(buffer, "\\VarFileInfo\\Translation", ctypes.byref(pointer), ctypes.byref(length)):
        return None
    if length.value < 4 or not pointer.value:
        return None
    lang, codepage = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint16 * 2)).contents
    names = ("ProductVersion", "FileVersion")
    text = ""
    for name in names:
        query = f"\\StringFileInfo\\{lang:04x}{codepage:04x}\\{name}"
        if version.VerQueryValueW(buffer, query, ctypes.byref(pointer), ctypes.byref(length)) and pointer.value:
            text = ctypes.wstring_at(pointer)
            break
    match = _VERSION_RE.search(text)
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


_USER_DATA_DIR = re.compile(
    r'--user-data-dir(?:=|\s+)(?:"([^"]*)"|([^\s"]+))',
    re.IGNORECASE,
)
_PROFILE_PIDS: dict[str, frozenset[int]] = {}
_PROFILE_PIDS_LOCK = threading.Lock()
_CHROME_COMMANDS_SCRIPT = """
Get-CimInstance Win32_Process -Filter "Name = 'chrome.exe'" |
  Where-Object { $_.CommandLine -and ($_.CommandLine -notlike '*--type=*') } |
  ForEach-Object {
    $encoded = [Convert]::ToBase64String([System.Text.Encoding]::Unicode.GetBytes($_.CommandLine))
    Write-Output ($_.ProcessId.ToString() + ' ' + $encoded)
  }
""".strip()


def _windows_path_key(value: str | Path) -> str:
    """把 Windows 路径收成同一串，分隔符和大小写都不再影响比较。"""

    text = str(value).strip().strip('"')
    lowered = text.lower()
    if lowered.startswith("\\\\?\\"):
        text = text[4:]
    text = text.replace("\\", "/")
    while len(text) > 3 and text.endswith("/"):
        text = text[:-1]
    if len(text) >= 2 and text[1] == ":":
        text = text[0].upper() + text[1:]
    return text.casefold()


def command_uses_profile(command: str, profile: Path) -> bool:
    """浏览器进程的 user-data-dir 是否就是这个配置目录。"""

    if "--type=" in (command or "").lower():
        return False
    match = _USER_DATA_DIR.search(command or "")
    if match is None:
        return False
    raw = match.group(1) if match.group(1) is not None else match.group(2)
    if not raw:
        return False
    if _windows_path_key(raw) == _windows_path_key(profile):
        return True
    if sys.platform != "win32":
        return False
    try:
        return Path(raw).resolve() == profile.resolve()
    except OSError:
        return False


def _decode_process_row(line: str) -> tuple[int, str] | None:
    pid_text, separator, encoded = line.strip().partition(" ")
    if not separator or not pid_text.isdigit() or not encoded:
        return None
    try:
        command = base64.b64decode(encoded, validate=True).decode("utf-16-le")
    except (ValueError, UnicodeError):
        return None
    if "--type=" in command.lower():
        return None
    return int(pid_text), command


def chrome_browser_commands() -> list[tuple[int, str]]:
    """当前 Chrome 主进程和它的命令行，不含渲染进程。

    命令行在 PowerShell 里是 UTF-16，打成 Base64 再输出。这样中文路径不会经过系统代码页。
    """

    if sys.platform != "win32":
        return []
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _CHROME_COMMANDS_SCRIPT],
            capture_output=True,
            text=True,
            encoding="utf-8-sig",
            errors="replace",
            timeout=15,
            check=False,
            creationflags=flags,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    rows: list[tuple[int, str]] = []
    for line in (completed.stdout or "").splitlines():
        row = _decode_process_row(line)
        if row is not None:
            rows.append(row)
    return rows


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return ctypes.get_last_error() == 5
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _pids_for_profile(profile_dir: Path) -> set[int]:
    key = _windows_path_key(profile_dir)
    with _PROFILE_PIDS_LOCK:
        cached = _PROFILE_PIDS.get(key)
        if cached and all(_process_alive(pid) for pid in cached):
            return set(cached)
    found = {pid for pid, command in chrome_browser_commands() if command_uses_profile(command, profile_dir)}
    with _PROFILE_PIDS_LOCK:
        if found:
            _PROFILE_PIDS[key] = frozenset(found)
        else:
            _PROFILE_PIDS.pop(key, None)
    return found


class ChromeWindowPlacement(NamedTuple):
    left: int
    top: int
    width: int
    height: int
    insert_after: int
    flags: int
    restore_command: int


def chrome_window_placement(*, shown: bool) -> ChromeWindowPlacement:
    """后台停在屏幕外并保持可见。前台再恢复到原来的大小。"""

    if shown:
        return ChromeWindowPlacement(40, 40, 1440, 900, 0, 0x0040, 9)
    return ChromeWindowPlacement(-32000, -32000, 1440, 900, 1, 0x0010 | 0x0040, 0)


def hide_chrome_windows(profile_dir: Path) -> None:
    """把这个配置目录的窗口挪到屏幕外，并从任务栏拿掉。窗口仍然可见，页面继续绘制。"""

    _set_chrome_windows(profile_dir, shown=False)


def show_chrome_windows(profile_dir: Path) -> None:
    _set_chrome_windows(profile_dir, shown=True)


def _set_chrome_windows(profile_dir: Path, *, shown: bool) -> None:
    if sys.platform != "win32":
        return
    pids = _pids_for_profile(profile_dir)
    if not pids:
        return
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    user32.SetWindowPos.restype = wintypes.BOOL
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL

    def visit(hwnd: int, _lparam: int) -> bool:
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in pids:
            return True
        name = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, name, 256)
        if name.value != "Chrome_WidgetWin_1":
            return True
        _place_chrome_window(user32, hwnd, shown=shown)
        return True

    user32.EnumWindows(callback_type(visit), 0)


def _place_chrome_window(user32: Any, hwnd: int, *, shown: bool) -> None:
    placement = chrome_window_placement(shown=shown)
    _set_taskbar_style(user32, hwnd, visible=shown)
    # 先藏一下，任务栏才会按新样式刷新；SetWindowPos 随即在目标位置把它显示出来。
    user32.ShowWindow(hwnd, 0)
    user32.SetWindowPos(
        hwnd,
        placement.insert_after,
        placement.left,
        placement.top,
        placement.width,
        placement.height,
        placement.flags,
    )
    if placement.restore_command:
        user32.ShowWindow(hwnd, placement.restore_command)


def _set_taskbar_style(user32: Any, hwnd: int, *, visible: bool) -> None:
    """工具窗口不进任务栏，和 macOS 上没有程序坞图标一样。"""

    import ctypes
    from ctypes import wintypes

    getter = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
    setter = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
    getter.argtypes = [wintypes.HWND, ctypes.c_int]
    getter.restype = ctypes.c_ssize_t
    setter.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    setter.restype = ctypes.c_ssize_t
    toolwindow = 0x00000080
    appwindow = 0x00040000
    style = int(getter(hwnd, -20))
    if visible:
        style = (style | appwindow) & ~toolwindow
    else:
        style = (style | toolwindow) & ~appwindow
    setter(hwnd, -20, style)
    user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0002 | 0x0001 | 0x0004 | 0x0010 | 0x0020)


def persistent_context_options(
    profile_dir: Path,
    *,
    headless: bool,
    locale: str = "es-AR",
    accept_language: str = "es-AR,es;q=0.9",
    timezone_id: str = "America/Argentina/Buenos_Aires",
    geolocation: dict[str, float] | None = None,
) -> dict[str, Any]:
    """交给 Playwright 启动有界面的 Chrome。headless 只表示窗口先放在屏幕外。"""

    args = [item for item in LAUNCH_ARGS if not item.startswith("--lang=")]
    args.append(f"--lang={locale}")
    if headless:
        args.append("--window-position=-32000,-32000")
    return {
        "user_data_dir": str(profile_dir.resolve()),
        "headless": False,
        "channel": "chrome-for-testing",
        "locale": locale,
        "timezone_id": timezone_id,
        "geolocation": geolocation or {"latitude": -34.6037, "longitude": -58.3816},
        "permissions": ["geolocation"],
        "viewport": {"width": 1440, "height": 900},
        "extra_http_headers": {"Accept-Language": accept_language},
        "ignore_default_args": ["--enable-automation"],
        "args": args,
    }
