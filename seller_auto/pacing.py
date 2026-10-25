"""亚马逊与美客多分开计时的导航冷却。"""

from __future__ import annotations

import argparse
import random
import time
from dataclasses import dataclass
from typing import Callable


class DelayError(ValueError):
    """间隔或阈值不合法。"""


@dataclass
class DelaySettings:
    """页面操作的等待范围。时间单位是秒。"""

    amazon_min_delay: float = 8
    amazon_max_delay: float = 15
    ml_min_delay: float = 12
    ml_max_delay: float = 25
    amazon_read_min: float = 2
    amazon_read_max: float = 4
    ml_read_min: float = 3
    ml_read_max: float = 6
    ml_scroll_min: float = 3
    ml_scroll_max: float = 5
    ml_captcha_min: float = 30
    ml_captcha_max: float = 60
    similarity_threshold: float = 0.8
    max_pages: int = 0
    max_products: int = 0
    headless: bool = False
    profile_dir: str = "data/profile"
    output_dir: str = "output"

    def validate(self) -> "DelaySettings":
        ranges = (
            ("亚马逊点击间隔", self.amazon_min_delay, self.amazon_max_delay),
            ("美客多风控间隔", self.ml_min_delay, self.ml_max_delay),
            ("亚马逊阅读等待", self.amazon_read_min, self.amazon_read_max),
            ("美客多详情等待", self.ml_read_min, self.ml_read_max),
            ("美客多滚动间隔", self.ml_scroll_min, self.ml_scroll_max),
            ("美客多验证后冷却", self.ml_captcha_min, self.ml_captcha_max),
        )
        for name, low, high in ranges:
            if low < 0 or high < 0:
                raise DelayError(f"{name}不能为负数")
            if low > high:
                raise DelayError(f"{name}的最小值不能大于最大值")
        if not 0 <= self.similarity_threshold <= 1:
            raise DelayError("相似度阈值必须在 0 到 1 之间")
        if self.max_pages < 0 or self.max_products < 0:
            raise DelayError("页数和商品数量限制不能为负数")
        return self

    @classmethod
    def from_argv(cls, argv: list[str] | None = None) -> "DelaySettings":
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument("--amazon-min-delay", type=float, default=8)
        parser.add_argument("--amazon-max-delay", type=float, default=15)
        parser.add_argument("--ml-min-delay", type=float, default=12)
        parser.add_argument("--ml-max-delay", type=float, default=25)
        parser.add_argument("--similarity-threshold", type=float, default=0.8)
        parser.add_argument("--max-pages", type=int, default=0)
        parser.add_argument("--max-products", type=int, default=0)
        parser.add_argument("--headless", action="store_true")
        parser.add_argument("--profile-dir", default="data/profile")
        parser.add_argument("--output-dir", default="output")
        args, _unknown = parser.parse_known_args(argv)
        settings = cls(
            amazon_min_delay=args.amazon_min_delay,
            amazon_max_delay=args.amazon_max_delay,
            ml_min_delay=args.ml_min_delay,
            ml_max_delay=args.ml_max_delay,
            similarity_threshold=args.similarity_threshold,
            max_pages=args.max_pages,
            max_products=args.max_products,
            headless=args.headless,
            profile_dir=args.profile_dir,
            output_dir=args.output_dir,
        )
        return settings.validate()


class Cooldown:
    """两次导航之间必须等满一个随机间隔。

    第一次调用也会先等满一个间隔，这样从亚马逊切到美客多时不会立刻打开搜索页。
    如果上一个间隔还没结束，本次只补足剩余时间。
    """

    def __init__(
        self,
        min_seconds: float,
        max_seconds: float,
        sleep: Callable[[float], None] | None = None,
        now: Callable[[], float] | None = None,
        rng: Callable[[float, float], float] | None = None,
    ) -> None:
        if min_seconds < 0 or max_seconds < 0 or min_seconds > max_seconds:
            raise DelayError("冷却间隔不合法")
        self.min_seconds = min_seconds
        self.max_seconds = max_seconds
        self._sleep = sleep or time.sleep
        self._now = now or time.monotonic
        self._rng = rng or random.uniform
        self.ready_at: float | None = None

    def sample(self) -> float:
        return float(self._rng(self.min_seconds, self.max_seconds))

    def acquire(self) -> float:
        now = self._now()
        if self.ready_at is None:
            wait = self.sample()
        else:
            wait = max(0.0, self.ready_at - now)
        if wait > 0:
            self._sleep(wait)
        self.ready_at = self._now() + self.sample()
        return wait

    def penalize(self, seconds: float) -> None:
        if seconds < 0:
            raise DelayError("额外冷却不能为负数")
        now = self._now()
        base = now if self.ready_at is None else max(self.ready_at, now)
        self.ready_at = base + seconds
