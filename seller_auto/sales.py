"""美客多详情页销量文本。"""

from __future__ import annotations

import re

_SOLD = re.compile(r"vendidos?", re.IGNORECASE)


def has_sales(text: str) -> bool:
    """有 “vendido” 或 “vendidos” 才算有销量。"""

    return _SOLD.search(text or "") is not None


def sales_action(source: str, sales_text: str | None) -> str:
    """卡片上写了 vendido 才可以直接记。没写不等于没销量。

    低销量经常只出现在商品详情页，列表和广告卡片都会把它藏起来。
    record：卡片上已经有 vendido。
    open：卡片没写，或还没采集到卡片文字，要打开详情再看。
    """

    _ = source
    if sales_text and has_sales(sales_text):
        return "record"
    return "open"
