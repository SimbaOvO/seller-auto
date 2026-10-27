"""每个 ASIN 走完就导出；写完后店铺没结束再打开下一条。"""

from __future__ import annotations

from typing import Any


def next_required_step(state: dict[str, Any]) -> str:
    if state.get("export_pending"):
        return "session_status"
    products = state.get("products") or {}
    current_id = state.get("current_product_id")
    product = products.get(current_id or "")
    if product and not product.get("workbook_written"):
        pending = unfinished_step(product)
        if pending:
            return pending
        return "export_results"
    if state.get("amazon_done") and not _sales_pending(state):
        return "export_results"
    if not product:
        return "next_amazon_product"
    if state.get("single_product"):
        return "export_results"
    return "next_amazon_product"


def unfinished_step(product: dict[str, Any]) -> str | None:
    """这条 ASIN 还没走完时，返回下一步；走完则返回 None。"""

    if product.get("sales_pending"):
        return "session_status"
    if not product.get("keywords"):
        return "set_keywords"
    listing = product.get("listing") or {}
    candidates = listing.get("candidates") or []
    judged = set(product.get("judged_ids") or [])
    sent = candidates[: int(listing.get("next_index") or 0)]
    if any(item.get("candidate_id") not in judged for item in sent):
        return "submit_listing_matches"
    if not listing.get("page2_done") or int(listing.get("next_index") or 0) < len(candidates):
        return "next_listing_batch"
    search_matches = [item for item in product.get("matches") or [] if item.get("source") == "search"]
    for item in search_matches:
        ad_candidates = item.get("ad_candidates") or []
        cursor = len(ad_candidates) if item.get("ads_complete") else int(item.get("ad_next_index") or 0)
        delivered = ad_candidates[:cursor]
        if any(candidate.get("candidate_id") not in judged for candidate in delivered):
            return "submit_ad_matches"
        if not item.get("ads_complete"):
            return "next_ad_batch"
    return None


def products_ready_to_export(state: dict[str, Any]) -> list[str]:
    """流程已走完、还没写入表格的 ASIN。"""

    products = state.get("products") or {}
    ready: list[str] = []
    for product_id in state.get("product_order") or []:
        product = products.get(product_id) or {}
        if product.get("workbook_written"):
            continue
        if unfinished_step(product):
            continue
        ready.append(product_id)
    return ready


def _sales_pending(state: dict[str, Any]) -> bool:
    products = state.get("products") or {}
    return any(bool((product or {}).get("sales_pending")) for product in products.values())
