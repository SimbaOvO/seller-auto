"""客户端提交的同款判断，以及进看图前的标题预筛。"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

TITLE_MISMATCH_REASON = "标题明显不是同一商品"
TITLE_FILTER_MODEL = "title-filter"
RELATION_SAME = "same"
RELATION_FUNCTIONAL = "functional"
RELATION_SPEC = "spec"
VARIANT_RELATIONS = (RELATION_FUNCTIONAL, RELATION_SPEC)
_RELATION_ALIASES = {
    "functional": RELATION_FUNCTIONAL,
    "rectangle": RELATION_FUNCTIONAL,
    "rectangular": RELATION_FUNCTIONAL,
    "矩形": RELATION_FUNCTIONAL,
    "spec": RELATION_SPEC,
    "capacity": RELATION_SPEC,
    "unit": RELATION_SPEC,
    "规格": RELATION_SPEC,
    "容量": RELATION_SPEC,
}
VARIANT_ROWS = {
    RELATION_FUNCTIONAL: {
        "orders": "functional_market_orders",
        "fee": "functional_fba_fee",
        "mark": "（矩形）",
        "fallback": "矩形相似",
        "label": "矩形相似",
    },
    RELATION_SPEC: {
        "orders": "spec_market_orders",
        "fee": "spec_fba_fee",
        "mark": "（规格）",
        "fallback": "规格不同",
        "label": "规格不同",
    },
}

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


def match_relation(value: Any) -> str:
    """同款是 same。矩形相似和规格不同各自单独成行。"""

    raw = value.get("relation") if isinstance(value, dict) else value
    return _RELATION_ALIASES.get(str(raw or "").strip().casefold(), RELATION_SAME)


def is_variant(relation: str) -> bool:
    return relation in VARIANT_ROWS


def orders_field(relation: str) -> str:
    row = VARIANT_ROWS.get(relation)
    return str(row["orders"]) if row else "market_orders"


def fee_field(relation: str) -> str:
    row = VARIANT_ROWS.get(relation)
    return str(row["fee"]) if row else "fba_fee"


def variant_label(relation: str) -> str:
    row = VARIANT_ROWS.get(relation)
    return str(row["label"]) if row else ""


def variant_name(name: str, relation: str) -> str:
    row = VARIANT_ROWS[relation]
    mark = str(row["mark"])
    base = (name or "").strip()
    if base.endswith(mark):
        return base
    if not base:
        return str(row["fallback"])
    return f"{base}{mark}"


def separate_row_name(name: str) -> bool:
    return any((name or "").endswith(str(row["mark"])) for row in VARIANT_ROWS.values())


def known_orders_fields() -> set[str]:
    return {"market_orders", *(str(row["orders"]) for row in VARIANT_ROWS.values())}


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
    relation = match_relation(data)
    similar = coerce_bool(data.get("similar"))
    if is_variant(relation):
        similar = False
    return {
        "candidate_id": candidate_id,
        "similar": similar,
        "relation": relation,
        "confidence": confidence,
        "reason": str(data.get("reason") or "").strip(),
    }


def filter_matches(matches: list[Any], threshold: float) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """保留同款，以及达到阈值的矩形相似和规格不同。每类进自己的池；后两类记下自己这条链接。"""

    if not 0 <= threshold <= 1:
        raise MatchInputError("相似度阈值必须在 0 到 1 之间")
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for raw in matches:
        item = normalize_match(raw)
        if item["confidence"] >= threshold and (item["similar"] or is_variant(item["relation"])):
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
