"""亚马逊面包屑里的服装和食品类目。"""

from __future__ import annotations

import re

CLOTHING_KEYWORDS = (
    "ropa",
    "zapatos",
    "calzado",
    "moda",
    "clothing",
    "shoes",
    "apparel",
)
FOOD_KEYWORDS = (
    "alimentos",
    "bebidas",
    "abarrotes",
    "comestibles",
    "gourmet",
    "grocery",
    "food",
)

CATEGORY_LABELS = {
    "clothing": "服装",
    "food": "食品",
}


def _word_at(text: str, word: str) -> int | None:
    match = re.search(rf"(?<!\w){re.escape(word)}(?!\w)", text)
    if match is None:
        return None
    return match.start()


def classify_breadcrumb(text: str) -> str | None:
    """返回 clothing、food，或 None。只看面包屑文本，不看商品标题。"""

    folded = (text or "").casefold()
    hits: list[tuple[int, str]] = []
    for label, words in (("clothing", CLOTHING_KEYWORDS), ("food", FOOD_KEYWORDS)):
        for word in words:
            position = _word_at(folded, word.casefold())
            if position is not None:
                hits.append((position, label))
    if not hits:
        return None
    hits.sort(key=lambda item: item[0])
    return hits[0][1]


def category_reason(label: str | None) -> str | None:
    if label is None:
        return None
    return CATEGORY_LABELS.get(label, label)
