"""用整理好的美客多链接去蓝鲸选品查 30 天销量。尾程运费另按包装和最低售价计算。"""

from __future__ import annotations

import re
import time
import traceback
from typing import Any

from seller_auto.pacing import Cooldown
from seller_auto.step_log import step
from seller_auto.storage import public_url
from seller_auto.workbook import dimension_cm, lowest_selling_price, ranked_matches, weight_kg

ALL_ITEMS_URL = "https://xp.lingdongsz.com/#/allItems"
_VIEWPORT = {"width": 1600, "height": 1000}
_ITEM_ID = re.compile(r"(ML[A-Z])-?(\d{3,})", re.IGNORECASE)
_ARS = re.compile(r"ARS\s*([0-9][0-9.,]*)", re.IGNORECASE)
_NUMBER = re.compile(r"(\d+(?:\.\d+)?)")
# 阿根廷官方海外仓：体积重量（kg）= 长 × 宽 × 高（cm）/ 4000。
MLA_VOLUME_DIVISOR = 4000
# 每档是（重量下限不含，上限含，六个售价档）。最后一档上限为空，表示大于下限。
# 售价档顺序：二手或免费、小于 15999、16000–23999、24000–32999、33000–50000、大于等于 50000。
# 和页面一致：有上下限时售价 >= 下限且 < 上限；绿色声誉。
_RATE_TABLE = (
    (0, 0.3, (13580, 1330, 2740, 3320, 7428, 8148)),
    (0.3, 0.5, (14580, 1370, 2760, 3340, 8148, 8748)),
    (0.5, 1, (16580, 1390, 2780, 3360, 9348, 9948)),
    (1, 1.5, (17180, 1410, 2800, 3380, 9588, 10308)),
    (1.5, 2, (17580, 1430, 2820, 3400, 9948, 10548)),
    (2, 3, (19180, 1450, 2860, 3470, 10668, 11508)),
    (3, 4, (21780, 1470, 2910, 3520, 11748, 13068)),
    (4, 5, (23780, 1500, 3040, 3670, 12948, 14268)),
    (5, 8, (26180, 1520, 3130, 3760, 14148, 15708)),
    (8, 10, (28380, 1560, 3180, 3910, 15348, 17028)),
    (10, 13, (30380, 1590, 3220, 4020, 16548, 18228)),
    (13, 15, (32580, 1620, 3280, 4060, 17748, 19548)),
    (15, 20, (38780, 1640, 3320, 4100, 21108, 23268)),
    (20, 25, (46780, 1660, 3380, 4170, 25068, 28068)),
    (25, 30, (64180, 1680, 3410, 4210, 34308, 38508)),
    (30, 40, (73980, 1700, 3440, 4250, 39108, 44388)),
    (40, 50, (78180, 1720, 3460, 4300, 41268, 46908)),
    (50, 60, (87180, 1740, 3480, 4320, 45708, 52308)),
    (60, 70, (90980, 1760, 3510, 4350, 47508, 54588)),
    (70, 80, (105380, 1780, 3530, 4370, 54948, 63228)),
    (80, 90, (130380, 1800, 3560, 4400, 67668, 78228)),
    (90, 100, (150180, 1820, 3580, 4420, 77988, 90108)),
    (100, 120, (163980, 1840, 3600, 4450, 85068, 98388)),
    (120, 140, (184580, 1860, 3620, 4470, 95748, 110748)),
    (140, 160, (205380, 1880, 3650, 4500, 106428, 123228)),
    (160, 180, (225980, 1900, 3680, 4520, 116988, 135588)),
    (180, None, (246580, 1920, 3700, 4550, 127668, 147948)),
)
_PRICE_BANDS = (
    (None, None),
    (None, 15999),
    (16000, 23999),
    (24000, 32999),
    (33000, 50000),
    (50000, None),
)

_GATE_JS = r"""
() => {
  const shown = (el) => {
    if (!el) return false;
    const style = getComputedStyle(el);
    return style.display !== "none" && style.visibility !== "hidden" && el.getClientRects().length > 0;
  };
  const label = [...document.querySelectorAll("span.label")].find((el) => (el.textContent || "").trim() === "商品链接");
  let node = label;
  for (let i = 0; i < 6 && node; i += 1) {
    if ([...node.querySelectorAll("input, textarea")].some(shown)) return "ready";
    node = node.parentElement;
  }
  const hash = location.hash || "";
  const password = document.querySelector('input[type="password"]');
  if (hash.includes("/login") || (password && password.getClientRects().length > 0)) return "login";
  const text = ((document.body && document.body.innerText) || "").slice(0, 800);
  if (text.includes("请登录") || text.includes("立即登录")) return "login";
  return null;
}
"""

_HOOK_JS = r"""
() => {
  if (window.__ldHooked) return window.__ldNet || 0;
  window.__ldHooked = true;
  window.__ldNet = window.__ldNet || 0;
  const bump = () => { window.__ldNet += 1; };
  const origFetch = window.fetch;
  if (typeof origFetch === "function") {
    window.fetch = function () {
      const pending = origFetch.apply(this, arguments);
      return Promise.resolve(pending).finally(bump);
    };
  }
  const proto = XMLHttpRequest.prototype;
  const origOpen = proto.open;
  proto.open = function () {
    this.addEventListener("loadend", bump);
    return origOpen.apply(this, arguments);
  };
  return window.__ldNet;
}
"""

_TABLE_JS = r"""
() => {
  const isShown = (el) => {
    if (!el) return false;
    const style = getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden" || style.opacity === "0") return false;
    return el.getClientRects().length > 0;
  };
  const table = document.querySelector(".vxe-table");
  if (!table) return {ready: false, loading: false, empty: false, rows: []};
  let source = [];
  try {
    const proxy = table.__vueParentComponent && table.__vueParentComponent.proxy;
    const data = proxy && proxy.getTableData && proxy.getTableData();
    source = (data && (data.visibleData || data.tableData)) || [];
  } catch (error) {
    source = [];
  }
  const rows = source.map((row) => ({
    id: String((row && row.id) || ""),
    sale30: row ? row.sale30 : null,
    price: row ? row.price : null,
    url: String((row && row.url) || ""),
    item_weight: row ? (row.itemWeight ?? "") : "",
    item_length: row ? (row.itemLength ?? "") : "",
    item_width: row ? (row.itemWidth ?? "") : "",
    item_height: row ? (row.itemHeight ?? "") : "",
  }));
  const loading = [...document.querySelectorAll(".el-loading-mask, .vxe-loading, .vxe-table--loading")].some(isShown);
  const empty = [...table.querySelectorAll(".vxe-table--empty-block, .vxe-table--empty-placeholder")].some(isShown);
  return {ready: true, loading, empty, rows};
}
"""

_SET_VALUE_JS = r"""
(el, value) => {
  const proto = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
  proto.set.call(el, value);
  el.dispatchEvent(new Event("input", { bubbles: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
}
"""

# 商品链接是单行输入框，一次填一条美客多地址。一批查完再查下一批。
LINK_BATCH_SIZE = 5

_STATUS_ALL_JS = r"""
() => {
  const label = [...document.querySelectorAll("span.label")].find((el) => (el.textContent || "").trim() === "商品状态");
  let node = label;
  for (let i = 0; i < 6 && node; i += 1) {
    const option = [...node.querySelectorAll("label.el-radio")].find((el) => {
      return [...el.querySelectorAll(".el-radio__label")].some((item) => (item.textContent || "").trim() === "全部");
    });
    if (option) {
      if (option.classList.contains("is-checked")) return "ready";
      option.click();
      return "clicked";
    }
    node = node.parentElement;
  }
  return "missing";
}
"""


class LingdongError(RuntimeError):
    pass


def item_id(value: str) -> str:
    match = _ITEM_ID.search(value or "")
    if match is None:
        return ""
    return f"{match.group(1).upper()}{match.group(2)}"


def parse_ars_amount(text: str) -> int | float | None:
    """从「ARS 2740」或「ARS2,740.00」取出金额。"""

    match = _ARS.search(text or "")
    if match is None:
        return None
    number = _parse_localized_number(match.group(1))
    if number is None:
        return None
    if number.is_integer():
        return int(number)
    return float(number)


def apply_last_mile(product: dict[str, Any]) -> int | None:
    """用卖家精灵的重量、长宽高，和前 10 条同款的最低售价，记下尾程运费。"""

    package = product.get("package_size")
    if not package_fields(package):
        return None
    price = lowest_selling_price(ranked_matches(product))
    if price is None:
        return None
    kilograms = chargeable_kilograms({}, package)
    fee = last_mile_fee(kilograms, price)
    if fee is None:
        return None
    product["fba_fee"] = fee
    step("运费", f"计费重量 {kilograms:.3f} kg，最低售价 {price:g}，尾程运费 {fee}")
    return fee


def last_mile_fee(weight_kg: float, price: float) -> int | None:
    """阿根廷官方海外仓、绿色声誉下的尾程运费，单位是比索。"""

    try:
        amount = float(price)
    except (TypeError, ValueError):
        return None
    if amount <= 0:
        return None
    try:
        kilograms = float(weight_kg)
    except (TypeError, ValueError):
        kilograms = 0
    rates = _rates_for_weight(kilograms)
    found = rates[0]
    for index, (low, high) in enumerate(_PRICE_BANDS):
        if index == 0:
            continue
        matched = False
        if low is not None and high is not None:
            matched = amount >= low and amount < high
        elif low is not None:
            matched = amount >= low
        elif high is not None:
            matched = amount < high
        if matched:
            found = rates[index]
    return found


def chargeable_kilograms(row: dict[str, Any], package: dict[str, Any] | None) -> float:
    """官方海外仓取实重和体积重量的较大值。重量和尺寸优先用卖家精灵。"""

    fields = package_fields(package) or {}
    weight = fields.get("weight")
    if weight is None:
        weight = _kilograms(row.get("item_weight")) or 0
    length = fields.get("length")
    if length is None:
        length = _centimeters(row.get("item_length")) or 0
    width = fields.get("width")
    if width is None:
        width = _centimeters(row.get("item_width")) or 0
    height = fields.get("height")
    if height is None:
        height = _centimeters(row.get("item_height")) or 0
    volume = 0.0
    if length > 0 and width > 0 and height > 0:
        volume = length * width * height / MLA_VOLUME_DIVISOR
    return max(weight, volume)


def link_batches(links: list[str], size: int = LINK_BATCH_SIZE) -> list[list[str]]:
    """商品链接按批切开。每批里的地址仍逐条填入。"""

    step_size = size if size > 0 else LINK_BATCH_SIZE
    cleaned = [url for url in links if url]
    return [cleaned[index:index + step_size] for index in range(0, len(cleaned), step_size)]


def quote_links(product: dict[str, Any]) -> list[str]:
    """和表格 MKD链接 同一批：按销量倒序的前 10 条，去掉跟踪参数。"""

    found: list[str] = []
    for item in ranked_matches(product):
        url = public_url(str(item.get("url") or ""), str(item.get("key") or item.get("candidate_id") or ""))
        if url and url not in found:
            found.append(url)
    return found


def total_orders(rows: list[dict[str, Any]]) -> int | None:
    seen: set[str] = set()
    total = 0
    found = False
    for row in rows:
        key = item_id(str(row.get("id") or "")) or item_id(str(row.get("url") or "")) or str(row.get("source_url") or "")
        if key and key in seen:
            continue
        count = _count(row.get("sale30"))
        if count is None:
            continue
        if key:
            seen.add(key)
        total += count
        found = True
    return total if found else None


def choose_quote_row(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """尾程运费跟售价档位走。表格售价取最低价，所以用查到的最低价那一条。"""

    if not rows:
        return None

    def price_of(row: dict[str, Any]) -> float | None:
        try:
            price = float(row.get("price"))
        except (TypeError, ValueError):
            return None
        if price <= 0:
            return None
        return price

    priced = [row for row in rows if price_of(row) is not None]
    pool = priced or list(rows)
    return min(pool, key=lambda row: (price_of(row) if price_of(row) is not None else 0, rows.index(row)))


def package_fields(package: dict[str, Any] | None) -> dict[str, float] | None:
    if not package:
        return None
    fields = {
        "length": dimension_cm(package, "length"),
        "width": dimension_cm(package, "width"),
        "height": dimension_cm(package, "height"),
        "weight": weight_kg(package),
    }
    if all(value is None for value in fields.values()):
        return None
    return fields


def collect_market_quotes(browser: Any, jobs: list[tuple[list[str], dict[str, Any] | None]]) -> list[dict[str, Any]]:
    page = _open_page(browser)
    try:
        _prepare(page)
        results: list[dict[str, Any]] = []
        for links, package in jobs:
            try:
                results.append(_quote_on_page(page, links, package, browser))
            except Exception as exc:
                _fail("这一条没查成", exc)
                results.append({"orders": None, "fba_fee": None})
        return results
    finally:
        try:
            browser.close_detail()
        except Exception:
            step("蓝鲸", "关闭蓝鲸标签失败")


def _parse_localized_number(raw: str) -> float | None:
    text = (raw or "").strip()
    if not text:
        return None
    if "," in text and "." in text:
        if text.rfind(".") > text.rfind(","):
            text = text.replace(",", "")
        else:
            text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        parts = text.split(",")
        if all(len(part) == 3 for part in parts[1:]):
            text = text.replace(",", "")
        else:
            text = text.replace(",", ".")
    elif text.count(".") > 1:
        text = text.replace(".", "")
    try:
        return float(text)
    except ValueError:
        return None


def _count(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    return int(number)


def _signature(state: dict[str, Any]) -> str:
    rows = state.get("rows") or []
    body = "|".join(f"{row.get('id')}:{row.get('sale30')}:{row.get('price')}" for row in rows)
    return f"{body}:{bool(state.get('empty'))}"


def table_decision(before_sig: str, state: dict[str, Any], saw_loading: bool, quiet_for: float) -> str:
    """表格还在转圈就继续等。内容变了，或加载结束后停稳了，才读第一条。"""

    if state.get("loading"):
        return "wait"
    if _signature(state) != before_sig:
        return "row" if state.get("rows") else "empty"
    if not state.get("rows") and state.get("empty"):
        return "empty"
    if saw_loading and quiet_for >= 0.8:
        return "row" if state.get("rows") else "empty"
    return "wait"


def _same_item(row: dict[str, Any], url: str) -> bool:
    wanted = item_id(url)
    if not wanted:
        return True
    text = f"{row.get('id') or ''} {row.get('url') or ''}".upper().replace("-", "")
    return wanted in text


def _matching_row(rows: list[dict[str, Any]], url: str) -> dict[str, Any] | None:
    for row in rows:
        if _same_item(row, url):
            return dict(row)
    return None


def _rates_for_weight(kilograms: float) -> tuple[int, ...]:
    if kilograms <= 0:
        return _RATE_TABLE[0][2]
    for low, high, rates in _RATE_TABLE:
        if high is None:
            if kilograms > low:
                return rates
        elif low < kilograms <= high:
            return rates
    return _RATE_TABLE[0][2]


def _centimeters(value: Any) -> float | None:
    text = str(value or "").strip().lower()
    match = _NUMBER.search(text)
    if match is None:
        return None
    number = float(match.group(1))
    if number <= 0:
        return None
    if "mm" in text:
        return number / 10
    if "in" in text:
        return number * 2.54
    return number


def _kilograms(value: Any) -> float | None:
    text = str(value or "").strip().lower()
    match = _NUMBER.search(text)
    if match is None:
        return None
    number = float(match.group(1))
    if number <= 0:
        return None
    if "kg" in text:
        return number
    if re.search(r"\dg\b", text) or text.endswith("g"):
        return number / 1000
    # 蓝鲸列表里的 itemWeight 不带单位，数值是克。
    if number >= 50:
        return number / 1000
    return number


def _fail(prefix: str, exc: Exception) -> None:
    step("蓝鲸", f"{prefix}：{exc}")
    step("蓝鲸", traceback.format_exc())


def _viewport(page: Any) -> None:
    try:
        page.set_viewport_size(_VIEWPORT)
    except Exception:
        step("蓝鲸", "设置页面尺寸失败")


def _wait_gate(page: Any, timeout_ms: int) -> str:
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    try:
        handle = page.wait_for_function(_GATE_JS, timeout=timeout_ms)
    except PlaywrightTimeout:
        return "timeout"
    value = handle.json_value()
    if value in {"ready", "login"}:
        return str(value)
    return "timeout"


def _open_page(browser: Any) -> Any:
    page = browser.open_detail("about:blank", viewport=(1600, 1000))
    _viewport(page)
    try:
        page.set_extra_http_headers({"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
    except Exception:
        step("蓝鲸", "没有改成中文请求头")
    browser.goto(page, ALL_ITEMS_URL)
    gate = _wait_gate(page, 25000)
    if gate == "login":
        step("蓝鲸", "需要登录，请在前台窗口完成")
        page = browser.reveal(page, "蓝鲸选品需要登录，暂时打开前台窗口")
        _viewport(page)
        gate = _wait_gate(page, 180000)
        if gate != "ready":
            raise LingdongError("蓝鲸选品需要登录后才能查询商品链接")
        page = browser.restore_background(page)
        _viewport(page)
        if _wait_gate(page, 25000) != "ready":
            raise LingdongError("回到后台后没有再看到商品链接")
    elif gate != "ready":
        raise LingdongError("蓝鲸选品页面没有出现商品链接")
    return page


def _prepare(page: Any) -> None:
    page.evaluate(_HOOK_JS)
    reset = _reset_button(page)
    if reset.count():
        reset.first.click()
        page.wait_for_timeout(500)
    _select_all_status(page)
    _wait_idle(page)
    page.evaluate(_HOOK_JS)


def _select_all_status(page: Any) -> None:
    clicked = page.evaluate(_STATUS_ALL_JS)
    if clicked == "ready":
        step("蓝鲸", "商品状态已是全部")
        return
    if clicked == "missing":
        step("蓝鲸", "没有找到商品状态的全部")
        return
    page.wait_for_function(
        """() => {
          const label = [...document.querySelectorAll("span.label")].find((el) => (el.textContent || "").trim() === "商品状态");
          let node = label;
          for (let i = 0; i < 6 && node; i += 1) {
            const option = [...node.querySelectorAll("label.el-radio")].find((el) => {
              return [...el.querySelectorAll(".el-radio__label")].some((item) => (item.textContent || "").trim() === "全部");
            });
            if (option) return option.classList.contains("is-checked");
            node = node.parentElement;
          }
          return false;
        }""",
        timeout=8000,
    )
    step("蓝鲸", "商品状态已选择全部")


def _wait_idle(page: Any) -> None:
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    try:
        page.wait_for_function(
            """() => {
              const shown = (el) => {
                if (!el) return false;
                const style = getComputedStyle(el);
                return style.display !== "none" && style.visibility !== "hidden" && el.getClientRects().length > 0;
              };
              return ![...document.querySelectorAll(".el-loading-mask, .vxe-loading")].some(shown);
            }""",
            timeout=15000,
        )
    except PlaywrightTimeout:
        step("蓝鲸", "筛选区还在加载，继续查询")


def _link_input(page: Any) -> Any:
    return page.locator("span.label", has_text=re.compile(r"^\s*商品链接\s*$")).locator(
        "xpath=ancestor::div[contains(@class,'flexRow')][1]//input"
    ).first


def _query_button(page: Any) -> Any:
    return page.locator("button", has_text=re.compile(r"^\s*查询\s*$")).first


def _reset_button(page: Any) -> Any:
    return _query_button(page).locator("xpath=ancestor::*[contains(@class,'el-row')][1]//button").filter(
        has_text=re.compile(r"^\s*重置\s*$")
    )


def _quote_on_page(page: Any, links: list[str], package: dict[str, Any] | None, browser: Any) -> dict[str, Any]:
    hits: list[dict[str, Any]] = []
    for index, batch in enumerate(link_batches(links), start=1):
        step("蓝鲸", f"第 {index} 批商品链接，共 {len(batch)} 条")
        for url in batch:
            row = _search_row(page, url, browser)
            if row is None:
                step("蓝鲸", f"没有对应商品行：{item_id(url) or url}")
                continue
            row["source_url"] = url
            hits.append(row)
            step("蓝鲸", f"{row.get('id') or item_id(url)} 30天销量 {row.get('sale30')}")
            page.wait_for_timeout(400)
    _ = package
    orders = total_orders(hits)
    if orders is not None:
        step("蓝鲸", f"30天合计 {orders}")
    return {"orders": orders}


def _query_cooldown(browser: Any) -> Cooldown:
    """蓝鲸每次点查询之前等 8–15 秒，和亚马逊点击同一档。"""

    cooldown = getattr(browser, "lingdong_cooldown", None)
    if isinstance(cooldown, Cooldown):
        if cooldown.ready_at is None:
            cooldown.ready_at = cooldown._now() + cooldown.sample()
        return cooldown
    settings = getattr(browser, "settings", None)
    low = float(getattr(settings, "amazon_min_delay", 8))
    high = float(getattr(settings, "amazon_max_delay", 15))
    cooldown = Cooldown(low, high, label="蓝鲸")
    cooldown.ready_at = cooldown._now() + cooldown.sample()
    try:
        browser.lingdong_cooldown = cooldown
    except Exception:
        step("蓝鲸", "没有记上查询间隔")
    return cooldown


def _search_row(page: Any, url: str, browser: Any) -> dict[str, Any] | None:
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    field = _link_input(page)
    field.click()
    field.fill(url)
    if field.input_value() != url:
        field.evaluate(_SET_VALUE_JS, url)
    _query_cooldown(browser).acquire()
    before = page.evaluate(_TABLE_JS)
    before_sig = _signature(before)
    start = page.evaluate("() => window.__ldNet || 0")
    try:
        _query_button(page).click(timeout=8000)
    except Exception as exc:
        step("蓝鲸", f"没有点到查询：{exc}")
        return None
    try:
        page.wait_for_function("(start) => (window.__ldNet || 0) > start", arg=start, timeout=30000)
    except PlaywrightTimeout:
        step("蓝鲸", f"查询没有等到接口返回：{item_id(url) or url}")
    deadline = time.monotonic() + 20
    saw_loading = False
    quiet_since: float | None = None
    while time.monotonic() < deadline:
        state = page.evaluate(_TABLE_JS)
        if state.get("loading"):
            saw_loading = True
            quiet_since = None
        quiet_for = 0.0 if quiet_since is None else time.monotonic() - quiet_since
        decision = table_decision(before_sig, state, saw_loading, quiet_for)
        if decision == "row":
            return dict((state.get("rows") or [])[0])
        if decision == "empty":
            return None
        if saw_loading and not state.get("loading") and quiet_since is None:
            quiet_since = time.monotonic()
        page.wait_for_timeout(250)
    state = page.evaluate(_TABLE_JS)
    return _matching_row(state.get("rows") or [], url)


