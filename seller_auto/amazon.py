"""亚马逊墨西哥店铺列表和详情页的读取。"""

from __future__ import annotations

from typing import Any

from seller_auto.urls import (
    absolute_amazon_url,
    canonical_amazon_url,
    choose_amazon_image_url,
    extract_asin,
    parse_brand,
)

TITLE_SELECTOR = '[data-cy="title-recipe"]'
PRODUCT_IMAGE_SELECTOR = "img.a-dynamic-image.media-block-image-tag.a-stretch-vertical"
SALES_PAGE_READY = "#productTitle"


def is_seller_list(url: str, seller_id: str) -> bool:
    lowered = (url or "").lower()
    return "amazon." in lowered and f"me={seller_id.lower()}" in lowered and "/s" in lowered


def list_title_entries(page: Any) -> list[dict[str, Any]]:
    recipes = page.locator(TITLE_SELECTOR)
    entries: list[dict[str, Any]] = []
    count = recipes.count()
    for index in range(count):
        recipe = recipes.nth(index)
        link = recipe.locator("a[href]").first
        if link.count() == 0:
            continue
        href = absolute_amazon_url(link.get_attribute("href") or "")
        if not href or href.startswith("javascript:"):
            continue
        asin = extract_asin(href)
        if "/dp/" not in href and "/gp/product/" not in href and not asin:
            continue
        try:
            text = " ".join(link.inner_text().split())
        except Exception:
            text = ""
        entries.append(
            {
                "index": index,
                "href": href,
                "asin": asin,
                "text": text,
                "key": asin or canonical_amazon_url(href),
            }
        )
    return entries


def title_locator(page: Any, index: int) -> Any:
    recipe = page.locator(TITLE_SELECTOR).nth(index)
    link = recipe.locator("a[href]").first
    if link.count() == 0:
        return recipe
    return link


def has_next_page(page: Any) -> bool:
    button = page.locator("a.s-pagination-next")
    if button.count() == 0:
        return False
    css = button.first.get_attribute("class") or ""
    if "s-pagination-disabled" in css:
        return False
    return (button.first.get_attribute("aria-disabled") or "").lower() != "true"


def read_product(page: Any) -> dict[str, str]:
    title = _text(page.locator(SALES_PAGE_READY))
    brand = parse_brand(_text(page.locator("#bylineInfo")))
    breadcrumb = _text(page.locator("#wayfinding-breadcrumbs_feature_div"))
    image = _image_locator(page)
    image_url = ""
    if image is not None and image.count() > 0:
        image_url = choose_amazon_image_url(
            image.first.get_attribute("data-old-hires"),
            image.first.get_attribute("data-a-dynamic-image"),
            image.first.get_attribute("src"),
        ) or ""
    asin = extract_asin(page.url)
    return {
        "title": title,
        "brand": brand,
        "breadcrumb": breadcrumb,
        "image_url": absolute_amazon_url(image_url) if image_url else "",
        "asin": asin,
        "url": canonical_amazon_url(page.url) if asin else page.url,
    }


def product_image_locator(page: Any) -> Any:
    return _image_locator(page)


def _image_locator(page: Any) -> Any:
    preferred = page.locator(PRODUCT_IMAGE_SELECTOR)
    if preferred.count() > 0:
        return preferred
    landing = page.locator("#landingImage")
    if landing.count() > 0:
        return landing
    wrapped = page.locator("#imgTagWrapperId img")
    if wrapped.count() > 0:
        return wrapped
    return page.locator(PRODUCT_IMAGE_SELECTOR)


def _text(locator: Any) -> str:
    try:
        if locator.count() == 0:
            return ""
        return " ".join(locator.first.inner_text(timeout=5000).split())
    except Exception:
        return ""
