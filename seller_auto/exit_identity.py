"""主浏览器跟阿根廷出口。卖家精灵那套浏览器单独用中国环境，并且不走代理。"""

from __future__ import annotations

from dataclasses import dataclass

from seller_auto.step_log import step

ARGENTINA_LOCALE = "es-AR"
ARGENTINA_ACCEPT_LANGUAGE = "es-AR,es;q=0.9,en;q=0.8"
ARGENTINA_TIMEZONE_ID = "America/Argentina/Buenos_Aires"
ARGENTINA_GEOLOCATION = {"latitude": -34.6037, "longitude": -58.3816}

CHINA_LOCALE = "zh-CN"
CHINA_ACCEPT_LANGUAGE = "zh-CN,zh;q=0.9,en;q=0.8"
CHINA_TIMEZONE_ID = "Asia/Shanghai"
CHINA_GEOLOCATION = {"latitude": 31.2304, "longitude": 121.4737}


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
            f"{self.country} {self.city} / {self.locale} / {self.timezone_id} / ({lat:.4f},{lon:.4f})"
        ).strip()


def argentina_identity() -> ExitIdentity:
    return ExitIdentity(
        ip="",
        country="AR",
        city="Buenos Aires",
        locale=ARGENTINA_LOCALE,
        accept_language=ARGENTINA_ACCEPT_LANGUAGE,
        timezone_id=ARGENTINA_TIMEZONE_ID,
        geolocation=dict(ARGENTINA_GEOLOCATION),
        source="argentina",
    )


def china_identity() -> ExitIdentity:
    return ExitIdentity(
        ip="",
        country="CN",
        city="Shanghai",
        locale=CHINA_LOCALE,
        accept_language=CHINA_ACCEPT_LANGUAGE,
        timezone_id=CHINA_TIMEZONE_ID,
        geolocation=dict(CHINA_GEOLOCATION),
        source="china",
    )


def resolve_exit_identity() -> ExitIdentity:
    """走阿根廷代理的主浏览器用阿根廷环境。卖家精灵浏览器另用中国环境。"""

    identity = argentina_identity()
    step("浏览器", f"使用阿根廷环境：{identity.summary()}")
    return identity
