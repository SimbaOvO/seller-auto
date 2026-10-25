"""美客多阿根廷搜索列表、销量和广告位。"""

from __future__ import annotations

from typing import Any

from seller_auto.sales import has_sales
from seller_auto.urls import absolute_amazon_url, ml_item_key, ml_page2_url

SALES_SELECTOR = (
    "#ui-pdp-main-container > div.ui-pdp-container__col.col-3.ui-pdp-container--column-center.pb-40 "
    "> div > div.ui-pdp-container__row.ui-pdp-with--separator--fluid.ui-pdp-with--separator--40-24 "
    "> div.ui-pdp-container__col.col-2.mr-24.mt-8 > div.ui-pdp-container__top-wrapper.mt-40 "
    "> div > div.ui-pdp-header__subtitle"
)
GRID_SELECTOR = ".ui-search-layout.ui-search-layout--grid"
AD_SELECTORS = (
    ".ui-recommendations-carousel-snapped",
    ".ui-recommendations-list.ui-recommendations-list__container--pads.ui-box-component-pdp__visible--desktop",
)
CARD_SCRIPT = """
(root) => {
  const images = [...root.querySelectorAll('img.poly-component__picture')];
  return images.map((el) => {
    const card = el.closest('.poly-card, .ui-search-layout__item, .andes-card, li, .ui-recommendations-card');
    const direct = el.closest('a[href]');
    let href = direct ? direct.href : '';
    if (!href && card) {
      const link = card.querySelector('a[href]');
      href = link ? link.href : '';
    }
    let title = '';
    if (card) {
      const heading = card.querySelector('.poly-component__title');
      title = heading ? heading.textContent : '';
    }
    if (!title) title = el.getAttribute('alt') || '';
    const src = el.currentSrc || el.getAttribute('src') || el.getAttribute('data-src') || '';
    return {href, src, title: (title || '').replace(/\\s+/g, ' ').trim()};
  });
}
"""


def read_sales_text(page: Any) -> str:
    specific = page.locator(SALES_SELECTOR)
    if specific.count() > 0:
        return _text(specific)
    return _text(page.locator("div.ui-pdp-header__subtitle"))


def sales_from_text(text: str) -> dict[str, Any]:
    cleaned = " ".join((text or "").split())
    return {"sales_text": cleaned, "has_sales": has_sales(cleaned)}


def hero_locator(page: Any) -> Any:
    figure = page.locator("figure.ui-pdp-gallery__figure img")
    if figure.count() > 0:
        return figure
    return page.locator("img.ui-pdp-image")


def hero_image_url(page: Any) -> str:
    locator = hero_locator(page)
    if locator.count() == 0:
        return ""
    image = locator.first
    src = image.get_attribute("data-zoom") or image.get_attribute("src") or ""
    return absolute_amazon_url(src, page.url)


def collect_grid_cards(page: Any) -> list[dict[str, str]]:
    grid = page.locator(GRID_SELECTOR)
    root = grid.first if grid.count() > 0 else page.locator("body").first
    try:
        raw_cards = root.evaluate(CARD_SCRIPT)
    except Exception:
        raw_cards = []
    return _dedupe_cards(raw_cards, page.url)


def collect_ad_cards(page: Any, pause: Any) -> list[dict[str, str]]:
    """慢速滚过广告位，并在每一屏收集当前可见的主图。"""

    cards: list[dict[str, str]] = []
    seen: set[str] = set()
    for selector in AD_SELECTORS:
        containers = page.locator(selector)
        for index in range(containers.count()):
            container = containers.nth(index)
            try:
                container.scroll_into_view_if_needed(timeout=5000)
            except Exception:
                pass
            last_position = -1.0
            for _ in range(6):
                _absorb_cards(cards, seen, _cards_from(container, page.url))
                try:
                    position = float(
                        container.evaluate(
                            """el => {
                                const scroller = el.querySelector(
                                    '.andes-carousel-snapped__scroll, [class*="scroll"]'
                                ) || el;
                                const before = scroller.scrollLeft || 0;
                                const delta = Math.max((scroller.clientWidth || 480) * 0.9, 280);
                                scroller.scrollLeft = before + delta;
                                return scroller.scrollLeft || 0;
                            }"""
                        )
                    )
                except Exception:
                    break
                if position == last_position:
                    break
                last_position = position
                pause()
            _absorb_cards(cards, seen, _cards_from(container, page.url))
    return cards


def _cards_from(root: Any, base_url: str) -> list[dict[str, str]]:
    try:
        raw_cards = root.evaluate(CARD_SCRIPT)
    except Exception:
        raw_cards = []
    return _dedupe_cards(raw_cards, base_url)


def _absorb_cards(cards: list[dict[str, str]], seen: set[str], found: list[dict[str, str]]) -> None:
    for card in found:
        if card["key"] in seen:
            continue
        seen.add(card["key"])
        cards.append(card)


def open_next_results_page(page: Any, search_url: str) -> bool:
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    button = page.locator(
        "a.andes-pagination__link[title='Siguiente'], li.andes-pagination__button--next a"
    )
    if button.count() > 0:
        try:
            button.first.click(timeout=10000)
            page.wait_for_load_state("domcontentloaded", timeout=20000)
            return True
        except PlaywrightTimeout:
            pass
    if "_Desde_" in (page.url or ""):
        return False
    target = ml_page2_url(page.url or search_url)
    if target == (page.url or ""):
        return False
    page.goto(target, wait_until="domcontentloaded")
    return True


def _dedupe_cards(raw_cards: list[dict[str, Any]], base_url: str) -> list[dict[str, str]]:
    cards: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in raw_cards or []:
        href = absolute_amazon_url(str(raw.get("href") or ""), base_url)
        image = absolute_amazon_url(str(raw.get("src") or ""), base_url)
        if not href:
            continue
        key = ml_item_key(href)
        if not key or key in seen:
            continue
        seen.add(key)
        cards.append(
            {
                "url": href,
                "image_url": image,
                "title": str(raw.get("title") or "").strip(),
                "key": key,
            }
        )
    return cards


def _text(locator: Any) -> str:
    try:
        if locator.count() == 0:
            return ""
        return " ".join(locator.first.inner_text(timeout=5000).split())
    except Exception:
        return ""
