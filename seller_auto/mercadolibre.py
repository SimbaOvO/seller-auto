"""美客多阿根廷搜索列表、销量和广告位。"""

from __future__ import annotations

import re
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
AD_CAROUSEL_SELECTOR = ".ui-recommendations-carousel-snapped"
AD_TITLE_SELECTOR = ".ui-recommendations-title-link"
WANTED_AD_TITLES = (
    "productos relacionados",
    "quienes vieron este producto también compraron",
)
MATCHING_AD_COUNT_SCRIPT = """
() => {
  const wanted = new Set([
    "productos relacionados",
    "quienes vieron este producto también compraron",
  ]);
  const normalize = (text) => (text || "").replace(/\\s+/g, " ").trim().toLowerCase();
  return [...document.querySelectorAll(".ui-recommendations-carousel-snapped")].reduce((count, block) => {
    const title = block.querySelector(".ui-recommendations-title-link");
    return count + (title && wanted.has(normalize(title.textContent)) ? 1 : 0);
  }, 0);
}
"""
MATCHING_AD_INDEXES_SCRIPT = """
() => {
  const wanted = new Set([
    "productos relacionados",
    "quienes vieron este producto también compraron",
  ]);
  const normalize = (text) => (text || "").replace(/\\s+/g, " ").trim().toLowerCase();
  return [...document.querySelectorAll(".ui-recommendations-carousel-snapped")].flatMap((block, index) => {
    const title = block.querySelector(".ui-recommendations-title-link");
    return title && wanted.has(normalize(title.textContent)) ? [index] : [];
  });
}
"""
CARD_SCRIPT = """
(root) => {
  const images = [...root.querySelectorAll('img.poly-component__picture, .poly-card__portada img')];
  const productLink = (card, el) => {
    const anchors = card ? [...card.querySelectorAll('a[href]')] : [];
    const hrefs = anchors.map((anchor) => anchor.href || '');
    const product = hrefs.find((href) => (
      /mercadolibre\\./i.test(href)
      && !/click1\\.mercadolibre|\\/mclics\\//i.test(href)
      && /MLA/i.test(href)
    ));
    if (product) return product;
    const direct = el.closest('a[href]');
    if (direct && direct.href && direct.href.includes('mercadolibre.')) return direct.href;
    return hrefs.find((href) => href.includes('mercadolibre.')) || '';
  };
  return images.map((el) => {
    const card = el.closest('.poly-card, .ui-search-layout__item, .andes-card, li, .ui-recommendations-card');
    const href = productLink(card, el);
    let title = '';
    if (card) {
      const heading = card.querySelector('.poly-component__title');
      title = heading ? heading.textContent : '';
    }
    if (!title) title = el.getAttribute('alt') || '';
    const src = el.currentSrc || el.getAttribute('src') || el.getAttribute('data-src') || '';
    let sales = '';
    let price = '';
    if (card) {
      const sold = card.querySelector(
        '.poly-component__sold-quantity, .ui-search-item__group__element--sold, [class*="sold-quantity"]'
      );
      sales = sold ? (sold.textContent || '') : '';
      if (!/vendidos?/i.test(sales)) {
        const found = (card.innerText || '').match(/\\+?\\d[\\d.]*\\s*vendidos?/i);
        sales = found ? found[0] : '';
      }
      const amount = card.querySelector(
        '.andes-money-amount:not(.andes-money-amount--previous) .andes-money-amount__fraction, .poly-price__current .andes-money-amount__fraction'
      );
      const cents = card.querySelector(
        '.andes-money-amount:not(.andes-money-amount--previous) .andes-money-amount__cents, .poly-price__current .andes-money-amount__cents'
      );
      if (amount) {
        price = (amount.textContent || '').trim();
        if (cents && (cents.textContent || '').trim()) {
          price += ',' + (cents.textContent || '').trim();
        }
      }
    }
    return {
      href,
      src,
      title: (title || '').replace(/\\s+/g, ' ').trim(),
      sales: (sales || '').replace(/\\s+/g, ' ').trim(),
      price: (price || '').replace(/\\s+/g, ' ').trim(),
    };
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


def parse_price(text: str) -> float | None:
    """读美客多阿根廷标价。12.345 是一万二千，12.345,50 带小数。"""

    cleaned = re.sub(r"[^\d.,]", "", text or "")
    if not cleaned:
        return None
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    elif cleaned.count(".") > 1:
        cleaned = cleaned.replace(".", "")
    elif cleaned.count(".") == 1:
        whole, fraction = cleaned.split(".")
        if len(fraction) == 3:
            cleaned = whole + fraction
    try:
        return float(cleaned)
    except ValueError:
        return None


def read_price(page: Any) -> float | None:
    specific = page.locator(
        ".ui-pdp-price .andes-money-amount:not(.andes-money-amount--previous) .andes-money-amount__fraction"
    )
    text = _text(specific) if specific.count() > 0 else ""
    if not text:
        text = _text(page.locator("meta[itemprop='price']"))
    if not text:
        try:
            content = page.locator("meta[itemprop='price']").first.get_attribute("content")
            text = content or ""
        except Exception:
            text = ""
    if not text:
        text = _text(page.locator(".andes-money-amount__fraction"))
    cents = _text(
        page.locator(
            ".ui-pdp-price .andes-money-amount:not(.andes-money-amount--previous) .andes-money-amount__cents"
        )
    )
    if text and cents:
        text = f"{text},{cents}"
    return parse_price(text)


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


def note_ad_block_count(previous: int, current: int, stable_rounds: int) -> tuple[int, bool]:
    """广告位分几块懒加载。数量连续两次不变，才算这几块都出来了。"""

    if current > 0 and current == previous:
        stable_rounds += 1
    else:
        stable_rounds = 0
    return stable_rounds, stable_rounds >= 2


def is_wanted_ad_title(text: str) -> bool:
    normalized = " ".join((text or "").split()).casefold()
    return normalized in WANTED_AD_TITLES


def collect_ad_cards(page: Any, pause: Any) -> list[dict[str, str]]:
    """只收集三个指定轮播：两块 Productos relacionados，一块 Quienes vieron este producto también compraron。"""

    _wait_for_ad_blocks(page, pause)
    cards: list[dict[str, str]] = []
    seen: set[str] = set()
    carousels = page.locator(AD_CAROUSEL_SELECTOR)
    for index in _matching_ad_indexes(page):
        container = carousels.nth(index)
        try:
            container.scroll_into_view_if_needed(timeout=8000)
        except Exception:
            pass
        _wait_for_pictures(container, pause)
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


def _matching_ad_indexes(page: Any) -> list[int]:
    try:
        indexes = page.evaluate(MATCHING_AD_INDEXES_SCRIPT)
    except Exception:
        return []
    return [int(index) for index in indexes or []]


def _wait_for_ad_blocks(page: Any, pause: Any) -> int:
    previous = -1
    stable_rounds = 0
    count = 0
    for _ in range(8):
        try:
            page.mouse.wheel(0, 1600)
        except Exception:
            break
        pause()
        try:
            count = int(page.evaluate(MATCHING_AD_COUNT_SCRIPT))
        except Exception:
            count = 0
        stable_rounds, settled = note_ad_block_count(previous, count, stable_rounds)
        previous = count
        if count >= 3 and settled:
            break
        if settled and count > 0 and _ >= 4:
            break
    return count


def _wait_for_pictures(container: Any, pause: Any) -> None:
    for _ in range(3):
        try:
            ready = container.evaluate(
                """el => {
                    const img = el.querySelector('img.poly-component__picture');
                    return !!(img && (img.currentSrc || img.getAttribute('src') || img.getAttribute('data-src')));
                }"""
            )
        except Exception:
            return
        if ready:
            return
        pause()


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


def _is_product_href(href: str) -> bool:
    lowered = (href or "").lower()
    if not lowered or "mercadolibre." not in lowered:
        return False
    if "click1.mercadolibre" in lowered or "/mclics/" in lowered:
        return False
    return True


def _dedupe_cards(raw_cards: list[dict[str, Any]], base_url: str) -> list[dict[str, str]]:
    cards: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in raw_cards or []:
        href = absolute_amazon_url(str(raw.get("href") or ""), base_url)
        if not _is_product_href(href):
            continue
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
                "sales_text": " ".join(str(raw.get("sales") or "").split()),
                "price": parse_price(str(raw.get("price") or "")),
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
