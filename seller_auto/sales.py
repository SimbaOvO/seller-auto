"""美客多详情页销量文本。"""

from __future__ import annotations

import re

_SOLD = re.compile(r"vendidos?", re.IGNORECASE)


def has_sales(text: str) -> bool:
    """有 “vendido” 或 “vendidos” 才算有销量。"""

    return _SOLD.search(text or "") is not None
