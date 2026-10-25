"""店铺遍历还没结束时，下一步不能停在当前这一条商品上。"""

from __future__ import annotations

from typing import Any


def next_required_step(state: dict[str, Any]) -> str:
    if state.get("amazon_done"):
        return "export_results"
    products = state.get("products") or {}
    current_id = state.get("current_product_id")
    product = products.get(current_id or "")
    if not product:
        return "next_amazon_product"
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
        candidates = item.get("ad_candidates") or []
        cursor = len(candidates) if item.get("ads_complete") else int(item.get("ad_next_index") or 0)
        delivered = candidates[:cursor]
        if any(candidate.get("candidate_id") not in judged for candidate in delivered):
            return "submit_ad_matches"
        if not item.get("ads_complete"):
            return "next_ad_batch"
    if state.get("single_product"):
        return "export_results"
    return "next_amazon_product"
