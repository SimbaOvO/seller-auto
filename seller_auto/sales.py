"""美客多详情页销量文本。"""

from __future__ import annotations

import re

_SOLD = re.compile(r"vendidos?", re.IGNORECASE)
_COUNT = re.compile(r"\+?\s*(\d[\d.]*)\s*vendidos?", re.IGNORECASE)


def has_sales(text: str) -> bool:
    """有 “vendido” 或 “vendidos” 才算有销量。卖家信誉上的 “ventas” 不算。"""

    return _SOLD.search(text or "") is not None


def pick_sales_line(*texts: str) -> str:
    """从标题旁边的几段文字里挑出商品销量。短句优先，长文只抽出 vendido 那一段。"""

    cleaned = [" ".join((text or "").split()) for text in texts]
    cleaned = [text for text in cleaned if text]
    for text in cleaned:
        if has_sales(text) and len(text) <= 80:
            return text
    for text in cleaned:
        match = _COUNT.search(text)
        if match:
            return match.group(0).strip()
    return ""


def sales_count(text: str) -> int:
    """从销量文案取出数字。阿根廷用点当分位，+1.000 vendidos 是 1000。"""

    match = _COUNT.search(text or "")
    if not match:
        return 0
    return int(match.group(1).replace(".", ""))


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
