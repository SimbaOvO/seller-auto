"""用亚马逊主图拼接 1688 找货源链接，记录前 10 个商品的价格均价。"""

from __future__ import annotations

import re
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
from urllib.parse import urlencode

from seller_auto.step_log import step

_IMAGE_ID_RE = re.compile(r"/images/I/([^./]+)")
_SOURCE_LIMIT = 10
_WAIT_MS = 25000
_POLL_MS = 400
_LANDING = "https://aibuy.1688.com/landingpage/new-home/find-products.html"
_LOGIN_HOSTS = (
    "login.taobao.com",
    "passport.alibaba.com",
    "passport.1688.com",
    "login.1688.com",
    "havana.1688.com",
)

READ_PRICES_JS = r"""
() => {
  const listed = [];
  const seenPrices = new Set();
  for (const block of document.querySelectorAll('[class*="productCell"] [class*="productPrice"]')) {
    if (seenPrices.has(block)) continue;
    seenPrices.add(block);
    const integer = block.querySelector('[class*="priceInteger"]');
    if (!integer) continue;
    const whole = (integer.textContent || "").replace(/\s+/g, "");
    if (!/^\d+$/.test(whole)) continue;
    const decimalNode = block.querySelector('[class*="priceDecimal"]');
    const decimal = decimalNode ? (decimalNode.textContent || "").replace(/\s+/g, "") : "";
    const fraction = decimal.startsWith(".") ? decimal.slice(1) : decimal;
    listed.push(fraction && /^\d+$/.test(fraction) ? whole + "." + fraction : whole);
    if (listed.length >= 10) return listed;
  }
  if (listed.length) return listed;
  const skipClass = /del|through|old|origin|line-through/i;
  const priceText = /^[¥￥]?(\d+(?:\.\d+)?)(?:[-~～]\d+(?:\.\d+)?)?(?:起)?$/;
  const offerSelector = 'a[href*="detail.1688.com/offer/"], a[href*="detail.m.1688.com/offer/"]';
  const isSkipped = (el) => {
    const cls = String(el.className || "");
    let deco = "";
    try { deco = getComputedStyle(el).textDecoration || ""; } catch (error) {}
    return skipClass.test(cls) || skipClass.test(deco);
  };
  const leafPrice = (root) => {
    const leaves = [...root.querySelectorAll("*")].filter((node) => node.children.length === 0);
    for (const leaf of leaves) {
      if (isSkipped(leaf)) continue;
      const text = (leaf.textContent || "").replace(/\s+/g, "");
      const match = text.match(priceText);
      if (match) return match[1];
    }
    return "";
  };
  const cardOf = (anchor) => {
    let node = anchor;
    while (node.parentElement) {
      const count = node.parentElement.querySelectorAll(offerSelector).length;
      if (count > 1) return node;
      node = node.parentElement;
    }
    return node;
  };
  const prices = [];
  const seen = new Set();
  for (const anchor of document.querySelectorAll(offerSelector)) {
    const card = cardOf(anchor);
    if (seen.has(card)) continue;
    seen.add(card);
    const price = leafPrice(card);
    if (!price) continue;
    prices.push(price);
    if (prices.length >= 10) break;
  }
  return prices;
}
"""

_DISMISS_JS = r"""
() => {
  const names = ["关闭", "跳过", "稍后再说", "我知道了", "暂不", "知道了", "以后再说", "暂不登录", "先逛逛", "游客浏览", "确定", "取消", "Close"];
  const blocked = ["立即开通", "升级会员", "领取", "去登录", "确定支付", "立即购买", "马上开通"];
  const buttons = [...document.querySelectorAll('button, [role="button"], [class*="close"], [aria-label]')];
  let clicked = false;
  for (const button of buttons) {
    const text = ((button.innerText || button.getAttribute("aria-label") || "")).replace(/\s+/g, "");
    if (!text || blocked.some((word) => text.includes(word))) continue;
    const closable = names.some((name) => text.includes(name)) || /close/i.test(String(button.className || ""));
    if (!closable) continue;
    const dialog = button.closest('[class*="dialog"], [class*="modal"], [class*="popup"], [role="dialog"], [class*="overlay"]');
    if (!dialog) continue;
    const style = getComputedStyle(button);
    if (style.display === "none" || style.visibility === "hidden" || !button.getClientRects().length) continue;
    button.click();
    clicked = true;
  }
  return clicked;
}
"""


def amazon_search_image(url: str) -> str:
    match = _IMAGE_ID_RE.search(url or "")
    if match is None:
        return (url or "").strip()
    return f"https://m.media-amazon.com/images/I/{match.group(1)}._AC_US200_.jpg"


def sourcing_url(image_url: str) -> str:
    query = urlencode(
        [
            ("bizType", "selectionTool"),
            ("customerId", "sellerspriteLP"),
            ("outImageAddress", amazon_search_image(image_url)),
            ("lang", "zh"),
            ("currency", "CNY"),
        ]
    )
    return f"{_LANDING}?{query}"


def is_login_url(url: str) -> bool:
    lowered = (url or "").lower()
    if any(host in lowered for host in _LOGIN_HOSTS):
        return True
    return "1688.com" in lowered and any(part in lowered for part in ("login", "passport", "signin", "havana"))


def average_price(prices: list[str]) -> str | None:
    numbers: list[Decimal] = []
    for raw in prices[:_SOURCE_LIMIT]:
        try:
            number = Decimal(str(raw))
        except Exception:
            continue
        if number <= 0:
            continue
        numbers.append(number)
    if not numbers:
        return None
    mean = sum(numbers, Decimal(0)) / Decimal(len(numbers))
    return str(mean.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


_SORT_BY_SALES_JS = r"""
() => {
  const exact = (el) => (el.innerText || "").replace(/\s+/g, "") === "销量" && el.children.length === 0;
  const nodes = [...document.querySelectorAll("a, span, div, li, button")].filter(exact);
  const sort = nodes.find((el) => {
    const parent = el.parentElement;
    return !!parent && (parent.innerText || "").includes("综合");
  });
  if (!sort) return false;
  sort.click();
  return true;
}
"""


def source_from_prices(prices: list[str], url: str) -> dict[str, Any] | None:
    average = average_price(prices)
    if average is None:
        return None
    kept = [str(item) for item in prices[:_SOURCE_LIMIT] if str(item)]
    return {
        "average": average,
        "currency": "CNY",
        "count": len(kept),
        "prices": kept,
        "url": url,
    }


_LIST_READY_JS = r"""
() => {
  const prices = [...document.querySelectorAll('[class*="productCell"] [class*="productPrice"]')];
  let ready = 0;
  for (const price of prices) {
    let node = price;
    for (let depth = 0; depth < 8 && node; depth += 1, node = node.parentElement) {
      const image = [...node.querySelectorAll("img")].find((img) => {
        const src = img.currentSrc || img.src || "";
        return src.startsWith("http") && img.getClientRects().length > 0;
      });
      if (!image) continue;
      ready += 1;
      break;
    }
  }
  return ready;
}
"""


_FIRST_PRICE_JS = r"""
() => {
  const block = document.querySelector('[class*="productCell"] [class*="productPrice"]');
  if (!block) return "";
  return (block.innerText || "").replace(/\s+/g, "");
}
"""


def collect_visible_prices(page: Any) -> list[str]:
    """等图搜主图商品列表替换掉页面先闪出的推荐，再按销量读取前 10 个价格。"""

    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    started = _monotonic()
    deadline = started + 30
    placeholder = None
    replaced = False
    stable = ""
    stable_rounds = 0
    sorted_once = False
    announced = False
    latest: list[str] = []
    while _monotonic() < deadline:
        try:
            page.evaluate(_DISMISS_JS)
        except Exception:
            pass
        try:
            ready = int(page.evaluate(_LIST_READY_JS) or 0)
            first = str(page.evaluate(_FIRST_PRICE_JS) or "")
        except Exception:
            ready = 0
            first = ""
        if ready <= 0 or not first:
            try:
                page.wait_for_timeout(_POLL_MS)
            except PlaywrightTimeout:
                break
            continue
        if placeholder is None:
            placeholder = first
            try:
                page.wait_for_timeout(_POLL_MS)
            except PlaywrightTimeout:
                break
            continue
        if first != placeholder:
            replaced = True
        if not replaced and _monotonic() - started < 8:
            try:
                page.wait_for_timeout(_POLL_MS)
            except PlaywrightTimeout:
                break
            continue
        if first != stable:
            stable = first
            stable_rounds = 0
            try:
                page.wait_for_timeout(_POLL_MS)
            except PlaywrightTimeout:
                break
            continue
        stable_rounds += 1
        if stable_rounds < 2:
            try:
                page.wait_for_timeout(_POLL_MS)
            except PlaywrightTimeout:
                break
            continue
        if not announced:
            step("1688", f"主图商品列表已加载 {ready} 条")
            announced = True
        if not sorted_once:
            try:
                sorted_once = bool(page.evaluate(_SORT_BY_SALES_JS))
            except Exception:
                sorted_once = False
            if sorted_once:
                try:
                    page.wait_for_timeout(1500)
                except PlaywrightTimeout:
                    return latest
                continue
        try:
            raw = page.evaluate(READ_PRICES_JS)
        except Exception:
            raw = []
        latest = [str(item) for item in raw or [] if str(item)][:_SOURCE_LIMIT]
        if latest:
            return latest
        try:
            page.wait_for_timeout(_POLL_MS)
        except PlaywrightTimeout:
            break
    return latest


def read_page_source(browser: Any, page: Any) -> dict[str, Any] | None:
    """1688 结果页等到价格出现。需要登录时把窗口调到前台。"""

    import time

    current = page
    if is_login_url(getattr(current, "url", "") or ""):
        current = browser.reveal(current, "1688 需要登录，请在前台窗口完成")
        deadline = _monotonic() + 90
        while _monotonic() < deadline:
            current = browser.live_page(current)
            if not is_login_url(getattr(current, "url", "") or ""):
                current = browser.restore_background(current)
                break
            time.sleep(2)
        else:
            step("1688", "登录没有完成")
            return None
        if is_login_url(getattr(current, "url", "") or ""):
            step("1688", "登录没有完成")
            return None
    prices = collect_visible_prices(current)
    record = source_from_prices(prices, getattr(current, "url", "") or "")
    if record is None:
        step("1688", "没有读到货源价格")
        return None
    step("1688", f"已记录货源均价 {record['average']}（{record['count']}个）")
    return record


def read_source_average(browser: Any, image_url: str) -> dict[str, Any] | None:
    image = amazon_search_image(image_url)
    if not image.startswith("http"):
        step("1688", "没有主图地址，跳过货源均价")
        return None
    url = sourcing_url(image)
    try:
        step("1688", "打开找货源")
        page = browser.open_detail(url)
        prices = _wait_prices(browser, page)
        if not prices:
            step("1688", "没有读到货源价格")
            return None
        average = average_price(prices)
        if average is None:
            step("1688", "没有读到货源价格")
            return None
        record = source_from_prices(prices, url)
        if record is not None:
            step("1688", f"已记录货源均价 {record['average']}（{record['count']}个）")
        return record
    finally:
        browser.close_detail()


def _wait_prices(browser: Any, page: Any) -> list[str]:
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    went_back = False
    deadline = _monotonic() + (_WAIT_MS / 1000)
    latest: list[str] = []
    while _monotonic() < deadline:
        try:
            browser.trim_tabs()
        except Exception:
            pass
        try:
            page.evaluate(_DISMISS_JS)
        except Exception:
            pass
        if is_login_url(getattr(page, "url", "") or "") and not went_back:
            went_back = True
            try:
                page.go_back(wait_until="domcontentloaded", timeout=3000)
            except Exception:
                pass
            continue
        try:
            raw = page.evaluate(READ_PRICES_JS)
        except Exception:
            raw = []
        latest = [str(item) for item in raw or [] if str(item)][:_SOURCE_LIMIT]
        if len(latest) >= _SOURCE_LIMIT:
            return latest
        try:
            page.wait_for_timeout(_POLL_MS)
        except PlaywrightTimeout:
            break
    return latest


def _monotonic() -> float:
    import time

    return time.monotonic()
