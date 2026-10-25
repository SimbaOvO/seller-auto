"""客户端提交的同款判断。"""

from __future__ import annotations

from typing import Any


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
    """只保留 similar 且置信度达到阈值的判断。"""

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
