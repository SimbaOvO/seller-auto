"""从订阅读取一个节点，在本地转成 HTTP 代理。不改本机 Clash 的节点选择。"""

from __future__ import annotations

import atexit
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener

from seller_auto.step_log import step

LOCAL_PORT = 17910
_ENV_LOADED = False
_LOCK = threading.Lock()
_PROCESS: subprocess.Popen[bytes] | None = None
_ATEXIT = False
_NODE: dict[str, object] | None = None


def browser_proxy() -> dict[str, str]:
    return {"server": f"http://127.0.0.1:{LOCAL_PORT}"}


def proxy_label() -> str:
    node = _NODE
    if node is None:
        return f"127.0.0.1:{LOCAL_PORT}"
    return f"{node['name']} {node['server']}:{node['port']} -> 127.0.0.1:{LOCAL_PORT}"


def load_local_env() -> None:
    """读取项目根目录的 .env.local。已经在环境里的值优先。"""

    global _ENV_LOADED
    if _ENV_LOADED:
        return
    _ENV_LOADED = True
    path = Path(__file__).resolve().parent.parent / ".env.local"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def subscription_settings() -> tuple[str, str]:
    load_local_env()
    url = os.environ.get("SELLER_AUTO_SUBSCRIPTION_URL", "").strip()
    match = os.environ.get("SELLER_AUTO_NODE_MATCH", "").strip()
    if not url:
        raise RuntimeError("没有配置 SELLER_AUTO_SUBSCRIPTION_URL，请写到 .env.local")
    if not match:
        raise RuntimeError("没有配置 SELLER_AUTO_NODE_MATCH，请写到 .env.local")
    return url, match


def fetch_subscription(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "clash.meta", "Accept": "text/plain,application/yaml,*/*"})
    with urllib.request.urlopen(request, timeout=25) as response:
        return response.read().decode("utf-8", errors="replace")


def proxies_from_subscription(text: str) -> list[dict[str, object]]:
    section = _proxy_section(text)
    proxies: list[dict[str, object]] = []
    current: dict[str, object] | None = None
    for line in section.splitlines():
        stripped = line.strip()
        if stripped.startswith("- {") and stripped.endswith("}"):
            if current:
                proxies.append(current)
                current = None
            proxies.append(_flow_mapping(stripped[2:].strip()[1:-1]))
            continue
        if re.match(r"^-\s+name\s*:", stripped):
            if current:
                proxies.append(current)
            _, _, value = stripped.partition(":")
            current = {"name": _scalar(value.strip())}
            continue
        if current is not None and re.match(r"^[A-Za-z0-9_-]+\s*:", stripped):
            key, _, value = stripped.partition(":")
            current[key.strip()] = _scalar(value.strip())
    if current:
        proxies.append(current)
    return [item for item in proxies if item.get("name") and item.get("server")]


def select_node(proxies: list[dict[str, object]], match: str) -> dict[str, object]:
    hits = [item for item in proxies if match in str(item.get("name") or "")]
    if not hits:
        raise RuntimeError(f"订阅里没有名称包含「{match}」的节点")
    return hits[0]


def render_proxy_config(node: dict[str, object]) -> str:
    body = ["  - name: " + _yaml_scalar(node["name"])]
    for key, value in node.items():
        if key == "name" or value is None or value == "":
            continue
        body.append(f"    {key}: {_yaml_scalar(value)}")
    proxy = "\n".join(body)
    name = _yaml_scalar(node["name"])
    server = node["server"]
    return f"""mixed-port: {LOCAL_PORT}
bind-address: 127.0.0.1
allow-lan: false
mode: rule
log-level: warning
ipv6: false
external-controller: ""
dns:
  enable: true
  listen: 127.0.0.1:17911
  enhanced-mode: normal
  nameserver:
    - 223.5.5.5
    - 119.29.29.29
proxies:
{proxy}
proxy-groups:
  - name: PROXY
    type: select
    proxies:
      - {name}
rules:
  - DOMAIN,{server},DIRECT
  - MATCH,PROXY
"""


def load_node(*, refresh: bool = False) -> dict[str, object]:
    global _NODE
    with _LOCK:
        if _NODE is not None and not refresh:
            return _NODE
        url, match = subscription_settings()
        text = fetch_subscription(url)
        node = select_node(proxies_from_subscription(text), match)
        _NODE = node
        step("代理", f"订阅节点 {node['name']} {node['server']}:{node['port']}")
        return node


def ensure_local_proxy() -> None:
    """按订阅里的节点启动本地 mihomo。端口已在听就复用。"""

    global _PROCESS, _ATEXIT
    node = load_node()
    with _LOCK:
        if _port_open(LOCAL_PORT):
            return
        binary = find_mihomo()
        if binary is None:
            raise RuntimeError("没有找到 mihomo。请安装它，或设置 SELLER_AUTO_MIHOMO")
        runtime = _runtime_dir()
        runtime.mkdir(parents=True, exist_ok=True)
        config_path = runtime / "config.yaml"
        config_path.write_text(render_proxy_config(node), encoding="utf-8")
        log_path = runtime / "mihomo.log"
        log_handle = log_path.open("ab")
        try:
            step("代理", f"启动本地出口 {proxy_label()}")
            _PROCESS = subprocess.Popen(
                [str(binary), "-d", str(runtime), "-f", str(config_path)],
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            log_handle.close()
        if not _ATEXIT:
            atexit.register(stop_local_proxy)
            _ATEXIT = True
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if _port_open(LOCAL_PORT):
                return
            if _PROCESS.poll() is not None:
                raise RuntimeError(f"本地代理启动失败，详见 {log_path}")
            time.sleep(0.1)
        raise RuntimeError(f"本地代理没有在预期时间内打开，详见 {log_path}")


def stop_local_proxy() -> None:
    global _PROCESS
    with _LOCK:
        process = _PROCESS
        _PROCESS = None
        if process is None or process.poll() is not None:
            return
        try:
            os.killpg(process.pid, 15)
        except OSError:
            process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, 9)
            except OSError:
                process.kill()


def open_through_proxy(request: Request, timeout: float):
    ensure_local_proxy()
    opener = build_opener(
        ProxyHandler(
            {
                "http": f"http://127.0.0.1:{LOCAL_PORT}",
                "https": f"http://127.0.0.1:{LOCAL_PORT}",
            }
        )
    )
    return opener.open(request, timeout=timeout)


def find_mihomo() -> Path | None:
    load_local_env()
    env = os.environ.get("SELLER_AUTO_MIHOMO", "").strip()
    candidates: list[Path] = []
    if env:
        candidates.append(Path(env))
    found = shutil.which("mihomo")
    if found:
        candidates.append(Path(found))
    candidates.extend(
        (
            Path("/opt/homebrew/bin/mihomo"),
            Path("/usr/local/bin/mihomo"),
            Path("/Applications/Sparkle.app/Contents/Resources/sidecar/mihomo"),
        )
    )
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return path
    return None


def _runtime_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "argentina-proxy"


def _port_open(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _proxy_section(text: str) -> str:
    start = text.find("\nproxies:")
    if text.startswith("proxies:"):
        start = 0
    if start < 0:
        raise RuntimeError("订阅里没有 proxies")
    rest = text[start:]
    end = len(rest)
    for marker in ("\nproxy-groups:", "\nproxy-providers:", "\nrules:"):
        found = rest.find(marker)
        if found >= 0:
            end = min(end, found)
    return rest[:end]


def _flow_mapping(body: str) -> dict[str, object]:
    mapping: dict[str, object] = {}
    for field in _split_fields(body):
        if ":" not in field:
            continue
        key, _, value = field.partition(":")
        mapping[key.strip()] = _scalar(value.strip())
    return mapping


def _split_fields(body: str) -> list[str]:
    fields: list[str] = []
    buf: list[str] = []
    quote = ""
    for char in body:
        if quote:
            buf.append(char)
            if char == quote:
                quote = ""
            continue
        if char in "\"'":
            quote = char
            buf.append(char)
            continue
        if char == ",":
            fields.append("".join(buf).strip())
            buf = []
            continue
        buf.append(char)
    if buf:
        fields.append("".join(buf).strip())
    return [item for item in fields if item]


def _scalar(value: str) -> object:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    if value in {"true", "True"}:
        return True
    if value in {"false", "False"}:
        return False
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    return value


def _yaml_scalar(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    text = str(value)
    if re.fullmatch(r"[A-Za-z0-9._-]+", text):
        return text
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
