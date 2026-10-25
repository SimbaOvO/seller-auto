"""会话进度和导出结果。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


def seller_dir(output_dir: Path, seller_id: str) -> Path:
    path = output_dir / seller_id
    path.mkdir(parents=True, exist_ok=True)
    (path / "images").mkdir(exist_ok=True)
    return path


def state_path(output_dir: Path, seller_id: str) -> Path:
    return seller_dir(output_dir, seller_id) / "state.json"


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def find_state_by_session(output_dir: Path, session_id: str) -> Path | None:
    if not output_dir.exists():
        return None
    for path in output_dir.glob("*/state.json"):
        try:
            data = load_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("session_id") == session_id:
            return path
    return None


def build_export(state: dict[str, Any]) -> dict[str, Any]:
    products = []
    model_names: list[str] = []
    for product_id in state.get("product_order", []):
        product = state["products"][product_id]
        matches = []
        for item in product.get("matches", []):
            model_name = item.get("model_name") or ""
            if model_name and model_name not in model_names:
                model_names.append(model_name)
            matches.append(
                {
                    "url": item.get("url"),
                    "sales_text": item.get("sales_text"),
                    "similarity": item.get("confidence"),
                    "reason": item.get("reason") or "",
                    "source": item.get("source"),
                    "parent_url": item.get("parent_url"),
                    "image_path": item.get("image_path"),
                    "model_name": model_name,
                    "title": item.get("title") or "",
                }
            )
        products.append(
            {
                "product_id": product_id,
                "url": product.get("url"),
                "title": product.get("title"),
                "brand": product.get("brand") or "",
                "keywords": product.get("keywords") or "",
                "image_path": product.get("image_path"),
                "ml_search_url": product.get("ml_search_url"),
                "mercadolibre_matches": matches,
            }
        )
    return {
        "store_url": state.get("store_url"),
        "seller_id": state.get("seller_id"),
        "session_id": state.get("session_id"),
        "excluded": state.get("excluded", []),
        "model_names": model_names,
        "products": products,
    }


def export_filename(now: datetime | None = None) -> str:
    current = now or datetime.now().astimezone()
    return current.strftime("%Y%m%d-%H%M%S") + ".json"
