"""会话进度和导出结果。"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from seller_auto.progress import next_required_step
from seller_auto.sales import sales_count

_EXPORT_JSON = re.compile(r"^\d{8}-\d{6}\.json$")


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
    for path in output_dir.glob("**/state.json"):
        try:
            data = load_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("session_id") == session_id:
            return path
    return None


def public_url(url: str, item_key: str = "") -> str:
    if "click1.mercadolibre" in (url or "") or "/mclics/" in (url or ""):
        match = re.search(r"MLA(\d+)", item_key or "", flags=re.IGNORECASE)
        if match:
            return f"https://www.mercadolibre.com.ar/p/MLA{match.group(1)}"
    parts = urlsplit(url or "")
    if not parts.netloc:
        return url or ""
    return urlunsplit((parts.scheme or "https", parts.netloc, parts.path, "", ""))


def similarity_rows(product: dict[str, Any]) -> list[dict[str, Any]]:
    """只对已经通过相似和销量筛选的同款按销量倒序，销量相同再看相似度。"""

    rows = []
    for match in product.get("matches") or []:
        if match.get("confidence") is None:
            continue
        rows.append(
            {
                "candidate_id": match.get("candidate_id"),
                "url": public_url(str(match.get("url") or ""), str(match.get("key") or "")),
                "title": match.get("title") or "",
                "similarity": match.get("confidence"),
                "reason": match.get("reason") or "",
                "sales_text": match.get("sales_text") or "",
                "price": match.get("price"),
                "source": match.get("source"),
            }
        )
    rows.sort(
        key=lambda row: (
            -sales_count(str(row.get("sales_text") or "")),
            -float(row["similarity"] or 0),
            str(row.get("candidate_id") or row.get("url") or ""),
        )
    )
    return rows


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
                    "candidate_id": item.get("candidate_id"),
                    "url": public_url(str(item.get("url") or ""), str(item.get("key") or "")),
                    "sales_text": item.get("sales_text"),
                    "price": item.get("price"),
                    "similarity": item.get("confidence"),
                    "reason": item.get("reason") or "",
                    "source": item.get("source"),
                    "parent_url": public_url(str(item.get("parent_url") or "")) or None,
                    "image_path": item.get("image_path"),
                    "model_name": model_name,
                    "title": item.get("title") or "",
                }
            )
        matches.sort(
            key=lambda row: (
                -sales_count(str(row.get("sales_text") or "")),
                -float(row["similarity"] or 0),
                str(row.get("candidate_id") or row.get("url") or ""),
            )
        )
        products.append(
            {
                "product_id": product_id,
                "url": product.get("url"),
                "title": product.get("title"),
                "brand": product.get("brand") or "",
                "package_size": product.get("package_size"),
                "source_price": product.get("source_price"),
                "keywords": product.get("keywords") or "",
                "chinese_name": product.get("chinese_name") or "",
                "market_orders": product.get("market_orders"),
                "fba_fee": product.get("fba_fee"),
                "image_path": product.get("image_path"),
                "ml_search_url": product.get("ml_search_url"),
                "mercadolibre_matches": matches,
                "top_links": similarity_rows(product)[:10],
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


def discard_product_images(product: dict[str, Any]) -> int:
    """删掉这一条 ASIN 已经写入表格后不再需要的过程图片。"""

    removed = 0
    parents: set[Path] = set()
    for path in _product_image_paths(product):
        if not path.is_symlink() and not path.is_file():
            continue
        path.unlink()
        removed += 1
        parents.add(path.parent)
    for parent in parents:
        try:
            parent.rmdir()
        except OSError:
            pass
    return removed


def _product_image_paths(product: dict[str, Any]) -> list[Path]:
    values = [product.get("image_path")]
    listing = product.get("listing") or {}
    for candidate in listing.get("candidates") or []:
        values.append(candidate.get("image_path"))
    for match in product.get("matches") or []:
        values.append(match.get("image_path"))
        for candidate in match.get("ad_candidates") or []:
            values.append(candidate.get("image_path"))
    paths: list[Path] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in {str(path) for path in paths}:
            paths.append(Path(text))
    return paths


def discard_images(run_dir: Path) -> int:
    """删掉一轮里的过程图片。表格已经嵌进主图，状态和导出 JSON 留着。"""

    images = run_dir / "images"
    if not images.is_dir():
        return 0
    removed = 0
    for path in list(images.iterdir()):
        if path.is_symlink() or path.is_file():
            path.unlink()
            removed += 1
    try:
        images.rmdir()
    except OSError:
        pass
    return removed


def discard_finished_images(output_dir: Path) -> int:
    """已经导出、并且这条流程不再需要看图时，清掉过程图片。"""

    if not output_dir.is_dir():
        return 0
    removed = 0
    for state_file in output_dir.glob("**/state.json"):
        try:
            state = load_json(state_file)
        except (OSError, json.JSONDecodeError):
            continue
        run_dir = state_file.parent
        if next_required_step(state) != "export_results":
            continue
        if not any(_EXPORT_JSON.fullmatch(item.name) for item in run_dir.glob("*.json")):
            continue
        removed += discard_images(run_dir)
    return removed


def export_filename(now: datetime | None = None) -> str:
    current = now or datetime.now().astimezone()
    return current.strftime("%Y%m%d-%H%M%S") + ".json"
