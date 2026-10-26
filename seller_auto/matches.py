"""客户端提交的同款判断，以及进看图前的标题预筛。"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

TITLE_MISMATCH_REASON = "标题明显不是同一商品"
TITLE_FILTER_MODEL = "title-filter"

_STOPWORDS = {
    "a",
    "al",
    "and",
    "con",
    "de",
    "del",
    "e",
    "el",
    "en",
    "for",
    "la",
    "las",
    "los",
    "o",
    "of",
    "para",
    "por",
    "que",
    "sin",
    "su",
    "sus",
    "the",
    "un",
    "una",
    "unas",
    "unos",
    "with",
    "y",
}

_NOISE = {
    "azul",
    "blanco",
    "blanca",
    "calidad",
    "cm",
    "color",
    "colores",
    "combo",
    "envio",
    "estilo",
    "gratis",
    "gris",
    "kit",
    "marca",
    "ml",
    "mm",
    "modelo",
    "negro",
    "negra",
    "nueva",
    "nuevo",
    "oferta",
    "original",
    "pack",
    "par",
    "pares",
    "pcs",
    "pieza",
    "piezas",
    "premium",
    "profesional",
    "promocion",
    "roja",
    "rojo",
    "rosa",
    "set",
    "talla",
    "tipo",
    "unidad",
    "unidades",
    "verde",
}


class MatchInputError(ValueError):
    """同款判断的字段不完整。"""


def coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "y", "si", "sí", "sim"}
    if isinstance(value, (int, float)):
        return value != 0
    return False


def normalize_match(raw: Any) -> dict[str, Any]:
    if hasattr(raw, "model_dump"):
        data = raw.model_dump()
    elif isinstance(raw, dict):
        data = raw
    else:
        raise MatchInputError("每条判断必须是对象")
    candidate_id = str(data.get("candidate_id") or "").strip()
    if not candidate_id:
        raise MatchInputError("判断缺少 candidate_id")
    try:
        confidence = float(data.get("confidence") or 0)
    except (TypeError, ValueError) as exc:
        raise MatchInputError(f"{candidate_id} 的 confidence 不是数字") from exc
    return {
        "candidate_id": candidate_id,
        "similar": coerce_bool(data.get("similar")),
        "confidence": confidence,
        "reason": str(data.get("reason") or "").strip(),
    }


def filter_matches(matches: list[Any], threshold: float) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """只保留 similar 且置信度达到阈值的判断。达到标准的全部进入后续销量筛选。"""

    if not 0 <= threshold <= 1:
        raise MatchInputError("相似度阈值必须在 0 到 1 之间")
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for raw in matches:
        item = normalize_match(raw)
        if item["similar"] and item["confidence"] >= threshold:
            kept.append(item)
        else:
            dropped.append(item)
    return kept, dropped


def title_tokens(*parts: str) -> set[str]:
    folded = _fold(" ".join(part for part in parts if part))
    tokens: set[str] = set()
    for item in re.findall(r"[a-z]+", folded):
        if item in _STOPWORDS or item in _NOISE or len(item) < 3:
            continue
        tokens.add(_stem(item))
    return tokens


def title_obviously_different(amazon_title: str, candidate_title: str, extra: str = "") -> bool:
    """只有两边有效词完全对不上时才算明显不是同一商品。标题空着或拿不准的留给看图。"""

    amazon = title_tokens(amazon_title, extra)
    candidate = title_tokens(candidate_title)
    if not amazon or len(candidate) < 2:
        return False
    return not _tokens_related(amazon, candidate)


def _fold(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text or "")
    return "".join(char for char in normalized if not unicodedata.combining(char)).casefold()


def _stem(token: str) -> str:
    if len(token) > 5 and token.endswith("es"):
        return token[:-2]
    if len(token) > 3 and token.endswith("s"):
        return token[:-1]
    return token


def _tokens_related(left: set[str], right: set[str]) -> bool:
    if left & right:
        return True
    for first in left:
        for second in right:
            if len(first) >= 4 and len(second) >= 4 and (first.startswith(second) or second.startswith(first)):
                return True
    return False
