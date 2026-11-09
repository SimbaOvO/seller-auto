"""按当前出口 IP 对齐浏览器语言、时区和定位。不配置应用层代理，流量交给系统/TUN。"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from seller_auto.step_log import step

_LOOKUP_URLS = (
    "https://ipinfo.io/json",
    "https://ipapi.co/json/",
)


@dataclass(frozen=True)
class ExitIdentity:
    ip: str
    country: str
    city: str
    locale: str
    accept_language: str
    timezone_id: str
    geolocation: dict[str, float]
    source: str

    def summary(self) -> str:
        lat = self.geolocation["latitude"]
        lon = self.geolocation["longitude"]
        return (
            f"{self.ip or '未知'} / {self.country or '??'} {self.city or ''} / "
            f"{self.locale} / {self.timezone_id} / ({lat:.4f},{lon:.4f})"
        ).strip()


# 出口国 → 浏览器身份。美客多阿根廷仍打开 .com.ar，只是不再假装人在布宜诺斯艾利斯。
_COUNTRY_PROFILES: dict[str, dict[str, Any]] = {
    "AR": {
        "locale": "es-AR",
        "accept_language": "es-AR,es;q=0.9",
        "timezone_id": "America/Argentina/Buenos_Aires",
        "geolocation": {"latitude": -34.6037, "longitude": -58.3816},
    },
    "MX": {
        "locale": "es-MX",
        "accept_language": "es-MX,es;q=0.9,en;q=0.8",
        "timezone_id": "America/Mexico_City",
        "geolocation": {"latitude": 19.4326, "longitude": -99.1332},
    },
    "SG": {
        "locale": "en-SG",
        "accept_language": "en-SG,en;q=0.9,es;q=0.8",
        "timezone_id": "Asia/Singapore",
        "geolocation": {"latitude": 1.2897, "longitude": 103.8501},
    },
    "US": {
        "locale": "en-US",
        "accept_language": "en-US,en;q=0.9,es;q=0.8",
        "timezone_id": "America/New_York",
        "geolocation": {"latitude": 40.7128, "longitude": -74.006},
    },
    "CN": {
        "locale": "zh-CN",
        "accept_language": "zh-CN,zh;q=0.9,en;q=0.8",
        "timezone_id": "Asia/Shanghai",
        "geolocation": {"latitude": 31.2304, "longitude": 121.4737},
    },
    "HK": {
        "locale": "zh-HK",
        "accept_language": "zh-HK,zh;q=0.9,en;q=0.8",
        "timezone_id": "Asia/Hong_Kong",
        "geolocation": {"latitude": 22.3193, "longitude": 114.1694},
    },
    "JP": {
        "locale": "ja-JP",
        "accept_language": "ja-JP,ja;q=0.9,en;q=0.8",
        "timezone_id": "Asia/Tokyo",
        "geolocation": {"latitude": 35.6762, "longitude": 139.6503},
    },
}

_DEFAULT_PROFILE = {
    "locale": "en-US",
    "accept_language": "en-US,en;q=0.9,es;q=0.8",
    "timezone_id": "UTC",
    "geolocation": {"latitude": 0.0, "longitude": 0.0},
}

_cached: ExitIdentity | None = None


def clear_exit_identity_cache() -> None:
    global _cached
    _cached = None


def resolve_exit_identity(*, force: bool = False) -> ExitIdentity:
    """查一次出口 IP，映射成浏览器身份。查不到就退回阿根廷默认。"""

    global _cached
    if _cached is not None and not force:
        return _cached
    payload, source = _lookup_exit()
    identity = _from_payload(payload, source)
    _cached = identity
    step("出口", f"浏览器身份已对齐：{identity.summary()}")
    return identity


def _lookup_exit() -> tuple[dict[str, Any], str]:
    last_error = ""
    for url in _LOOKUP_URLS:
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "seller-auto/1.0", "Accept": "application/json"})
            with urllib.request.urlopen(request, timeout=8) as response:
                body = response.read().decode("utf-8", errors="replace")
            data = json.loads(body)
            if isinstance(data, dict) and (data.get("ip") or data.get("country") or data.get("country_code")):
                return data, url
        except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, UnicodeError) as exc:
            last_error = str(exc)
            continue
    step("出口", f"查出口 IP 失败，沿用阿根廷默认身份：{last_error or '无响应'}")
    return {}, "fallback"


def _from_payload(payload: dict[str, Any], source: str) -> ExitIdentity:
    if source == "fallback" or not payload:
        profile = dict(_COUNTRY_PROFILES["AR"])
        return ExitIdentity(
            ip="",
            country="AR",
            city="Buenos Aires",
            locale=str(profile["locale"]),
            accept_language=str(profile["accept_language"]),
            timezone_id=str(profile["timezone_id"]),
            geolocation=dict(profile["geolocation"]),
            source=source,
        )
    country = str(payload.get("country") or payload.get("country_code") or "").strip().upper()
    if len(country) > 2:
        country = country[:2]
    city = str(payload.get("city") or "").strip()
    ip = str(payload.get("ip") or payload.get("query") or "").strip()
    known = _COUNTRY_PROFILES.get(country)
    geolocation = _parse_location(payload.get("loc") or payload.get("latitude"), payload.get("longitude"))
    timezone = str(payload.get("timezone") or "").strip()
    if known is not None:
        profile = dict(known)
        # 已知国家用出口坐标；时区仍用该国常用值，避免 IP 库偶发错时区。
        if geolocation is not None:
            profile["geolocation"] = geolocation
    else:
        profile = dict(_DEFAULT_PROFILE)
        if timezone:
            profile["timezone_id"] = timezone
        if geolocation is not None:
            profile["geolocation"] = geolocation
    return ExitIdentity(
        ip=ip,
        country=country or "??",
        city=city,
        locale=str(profile["locale"]),
        accept_language=str(profile["accept_language"]),
        timezone_id=str(profile["timezone_id"]),
        geolocation=dict(profile["geolocation"]),
        source=source,
    )


def _parse_location(lat_or_loc: Any, lon: Any = None) -> dict[str, float] | None:
    if isinstance(lat_or_loc, str) and "," in lat_or_loc and lon is None:
        left, _, right = lat_or_loc.partition(",")
        try:
            return {"latitude": float(left.strip()), "longitude": float(right.strip())}
        except ValueError:
            return None
    try:
        if lat_or_loc is None or lon is None:
            return None
        return {"latitude": float(lat_or_loc), "longitude": float(lon)}
    except (TypeError, ValueError):
        return None
