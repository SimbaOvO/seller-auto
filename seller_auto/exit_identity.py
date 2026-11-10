"""浏览器语言、时区和定位固定为中国。出口 IP 由代理模块单独指定。"""

from __future__ import annotations

from dataclasses import dataclass

from seller_auto.step_log import step

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
    """主浏览器和资料浏览器都用这一套中国环境。"""

    identity = china_identity()
    step("浏览器", f"使用中国环境：{identity.summary()}")
    return identity
