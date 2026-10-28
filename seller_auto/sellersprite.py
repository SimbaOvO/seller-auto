"""亚马逊商品打开后，到卖家精灵读取包装重量和长宽高。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from seller_auto.step_log import step

LOGIN_URL = "https://www.sellersprite.com/cn/w/user/login"
LOOKUP_VIEWPORT = (2048, 1200)
LOOKUP_TIMEOUT_MS = 45000
_ASIN_RE = re.compile(r"^B0[A-Z0-9]{8}$")
_LABEL = r"(?:profundidad|longitud|ancho|alto|largo|prof\.?|alt\.?|an\.?|l\.?)"
_UNIT_WORD = r"(?:centimeters?|centimetres?|pulgadas?|pulgada|inches|inch|cm|in|厘米|英寸)"
_GAP = rf"(?:\s*{_LABEL})?\s*(?:{_UNIT_WORD})?(?:\s+de\s+{_LABEL})?"
_SEP = r"[x×*＊✕✖ｘ]"
_DIMENSION_RE = re.compile(
    rf"(\d+(?:[.,]\d+)?){_GAP}\s*{_SEP}\s*"
    rf"(\d+(?:[.,]\d+)?){_GAP}\s*{_SEP}\s*"
    rf"(\d+(?:[.,]\d+)?)(?:\s*{_LABEL})?\s*({_UNIT_WORD})?(?:\s+de\s+{_LABEL})?",
    re.IGNORECASE,
)
_DIMENSION_2D_RE = re.compile(
    rf"(\d+(?:[.,]\d+)?){_GAP}\s*{_SEP}\s*"
    rf"(\d+(?:[.,]\d+)?)(?:\s*{_LABEL})?\s*({_UNIT_WORD})?(?:\s+de\s+{_LABEL})?",
    re.IGNORECASE,
)
_WEIGHT_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*"
    r"(kilograms?|kilos?|grams?|gramos?|pounds?|libras?|ounces?|onzas?|kg|g|lb|lbs|oz|千克|公斤|克|磅|盎司)"
    r"(?!\w)",
    re.IGNORECASE,
)
_WEIGHT_UNITS = {
    "g": "g",
    "克": "g",
    "kg": "kg",
    "千克": "kg",
    "公斤": "kg",
    "lb": "lb",
    "lbs": "lb",
    "磅": "lb",
    "oz": "oz",
    "盎司": "oz",
    "kilogram": "kg",
    "kilograms": "kg",
    "kilo": "kg",
    "kilos": "kg",
    "gram": "g",
    "grams": "g",
    "gramo": "g",
    "gramos": "g",
    "pound": "lb",
    "pounds": "lb",
    "libra": "lb",
    "libras": "lb",
    "ounce": "oz",
    "ounces": "oz",
    "onza": "oz",
    "onzas": "oz",
}

# 同时认「包装重量 / 包装尺寸」和「商品重量 / 商品尺寸」。
# 包装列两项都有就只用包装列。包装列只有重量或只有尺寸时，用商品列补上缺的那一项。
# 两列都没有重量和尺寸就留空。
LOOKUP_STATE_JS = r"""
(asin) => {
  const markers = [
    "包装重量", "包装尺寸", "商品重量", "商品尺寸",
    "Package Weight", "Package Dimensions", "Item Weight", "Item Dimensions",
    "梱包重量", "梱包サイズ"
  ];
  const hints = ["未登录", "请先登录", "您是游客", "游客身份", "仅对注册用户开放"];
  try {
    const root = document.querySelector("#app") && document.querySelector("#app").__vue__;
    const research = root && root.$store && root.$store.state && root.$store.state.productResearch;
    if (research && Array.isArray(research.showColumns) && !research.showColumns.includes(17)) {
      root.$store.dispatch("productResearch/setShowColumns", research.showColumns.concat([17]));
    }
  } catch (error) {}
  const isShown = (el) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden" || style.opacity === "0") return false;
    return el.getClientRects().length > 0;
  };
  const needsLogin = () => {
    if ((location.pathname || "").includes("/user/login")) return true;
    const form = document.querySelector("#form_signin_passW");
    if (isShown(form)) return true;
    const dialogs = document.querySelectorAll(".el-message-box, .el-dialog, .bootbox, .swal2-popup");
    for (const el of dialogs) {
      if (!isShown(el)) continue;
      const text = el.innerText || "";
      if (hints.some((hint) => text.includes(hint))) return true;
    }
    const body = (document.body && document.body.innerText) || "";
    return hints.some((hint) => body.includes(hint));
  };
  const score = (text) => {
    const size = /\d+(?:[.,]\d+)?\s*(?:prof\.?|alt\.?|an\.?|l\.?|profundidad|ancho|alto|largo)?\s*(?:cm|in|inches|厘米|英寸|centimeters?|pulgadas?)?\s*[x×*＊✕✖ｘ]\s*\d+/i.test(text);
    const weight = /\d+(?:[.,]\d+)?\s*(?:kilograms?|kilos?|grams?|gramos?|pounds?|libras?|ounces?|onzas?|kg|g|lb|lbs|oz|千克|公斤|克|磅|盎司)(?!\w)/i.test(text);
    return (size ? 2 : 0) + (weight ? 1 : 0);
  };
  const readText = (el) => {
    if (!el) return "";
    const versions = [el.innerText || "", el.textContent || ""];
    if (el.getAttribute) versions.push(el.getAttribute("title") || "");
    let chosen = "";
    let chosenScore = -1;
    for (const version of versions) {
      const text = version.replace(/\u00a0/g, " ").trim();
      const value = score(text);
      if (value > chosenScore) {
        chosen = text;
        chosenScore = value;
      }
    }
    return chosen;
  };
  const columnClass = (el) => ([...el.classList].find((name) => /^el-table_\d+_column_\d+$/.test(name)) || "");
  const isPackageHeader = (text) => /包装重量|包装尺寸|Package Weight|Package Dimensions|梱包重量|梱包サイズ/.test(text);
  const prefer = (current, next) => {
    if (!current || next.score > current.score) return next;
    return current;
  };
  const readCell = () => {
    const wraps = [...document.querySelectorAll(".el-table__header-wrapper")];
    const wanted = String(asin || "").toUpperCase();
    let headerOnly = false;
    let packageBest = null;
    let productBest = null;
    for (const wrap of wraps) {
      if (wrap.closest(".el-table__fixed, .el-table__fixed-right, .el-table__fixed-header-wrapper")) continue;
      const table = wrap.closest(".el-table");
      if (!table) continue;
      const body = table.querySelector(".el-table__body-wrapper");
      const dataRows = body ? [...body.querySelectorAll("tbody tr.el-table__row")] : [];
      const headerRows = [...wrap.querySelectorAll("thead tr")];
      const headerHit = headerRows.some((tr) => markers.some((marker) => (tr.innerText || "").includes(marker)));
      if (!dataRows.length) {
        if (headerHit) headerOnly = true;
        continue;
      }
      const row = dataRows.find((item) => (item.innerText || "").toUpperCase().includes(wanted)) || dataRows[0];
      const tds = [...row.children].filter((el) => el.tagName === "TD" && !el.classList.contains("gutter"));
      for (const tr of headerRows) {
        const ths = [...tr.children].filter((el) => el.tagName === "TH" && !el.classList.contains("gutter"));
        ths.forEach((th, index) => {
          const headerText = th.innerText || "";
          if (!markers.some((marker) => headerText.includes(marker))) return;
          const key = columnClass(th);
          let td = key ? tds.find((cell) => cell.classList.contains(key)) : null;
          if (!td && ths.length === tds.length) td = tds[index];
          const text = readText(td);
          const ranked = {text, score: score(text)};
          if (isPackageHeader(headerText)) packageBest = prefer(packageBest, ranked);
          else productBest = prefer(productBest, ranked);
        });
      }
    }
    const pkg = packageBest && packageBest.score > 0 ? packageBest.text : "";
    const prod = productBest && productBest.score > 0 ? productBest.text : "";
    const mergeText = () => {
      if (!pkg) return prod;
      if (!prod) return pkg;
      const pkgSize = packageBest.score >= 2;
      const pkgWeight = packageBest.score % 2 === 1;
      if (pkgSize && pkgWeight) return pkg;
      if (!pkgSize && productBest.score >= 2) return pkg + "\n" + prod;
      if (!pkgWeight && productBest.score % 2 === 1) return pkg + "\n" + prod;
      return pkg;
    };
    if (pkg || prod) return {found: true, rows: true, text: mergeText()};
    if (packageBest || productBest) return {found: true, rows: true, text: ""};
    return {found: headerOnly, rows: false, text: ""};
  };
  const cell = readCell();
  if (cell.rows) return {status: "table", text: cell.text};
  if ([...document.querySelectorAll(".el-loading-mask")].some(isShown)) return null;
  if (needsLogin()) return {status: "login", text: ""};
  const empty = document.querySelector(".el-table__empty-block");
  if (cell.found && isShown(empty)) return {status: "empty", text: ""};
  return null;
}
"""


class SellerSpriteError(RuntimeError):
    pass


def competitor_lookup_url(asin: str) -> str:
    query = urlencode(
        [
            ("market", "MX"),
            ("monthName", "bsr_sales_nearly"),
            ("asins", json.dumps([asin.strip().upper()], separators=(",", ":"))),
            ("page", "1"),
            ("nodeIdPaths", "[]"),
            ("symbolFlag", "true"),
            ("size", "60"),
            ("order[field]", "amz_unit"),
            ("order[desc]", "true"),
            ("lowPrice", "N"),
            ("salesGrowthType", "30d"),
        ]
    )
    return f"https://www.sellersprite.com/v3/competitor-lookup?{query}"


def _plain_number(raw: str) -> str:
    if re.fullmatch(r"\d{1,3}(?:,\d{3})+", raw):
        return raw.replace(",", "")
    return raw.replace(",", ".")


def _normalized_unit(raw: str) -> str:
    unit = (raw or "").lower()
    if unit in {"in", "inch", "inches", "英寸", "pulgada", "pulgadas"}:
        return "in"
    if unit in {"cm", "厘米", "centimeter", "centimeters", "centimetre", "centimetres"}:
        return "cm"
    return unit


def parse_package_dimensions(text: str) -> dict[str, str] | None:
    cleaned = (text or "").replace("\u00a0", " ")
    match = _DIMENSION_RE.search(cleaned)
    height = True
    if match is None:
        match = _DIMENSION_2D_RE.search(cleaned)
        height = False
    if match is None:
        return None
    parsed = {
        "length": _plain_number(match.group(1)),
        "width": _plain_number(match.group(2)),
    }
    if height:
        parsed["height"] = _plain_number(match.group(3))
    unit = _normalized_unit(match.group(4) if height else match.group(3))
    if unit:
        parsed["unit"] = unit
    return parsed


def parse_package_weight(text: str) -> dict[str, str] | None:
    cleaned = (text or "").replace("\u00a0", " ")
    dimensions = _DIMENSION_RE.search(cleaned)
    if dimensions is not None:
        cleaned = cleaned[: dimensions.start()] + cleaned[dimensions.end() :]
    match = _WEIGHT_RE.search(cleaned)
    if match is None:
        return None
    unit = _WEIGHT_UNITS.get(match.group(2).lower(), match.group(2).lower())
    return {"weight": _plain_number(match.group(1)), "weight_unit": unit}


def parse_package_cell(text: str) -> dict[str, str] | None:
    parsed: dict[str, str] = {}
    dimensions = parse_package_dimensions(text)
    if dimensions is not None:
        parsed.update(dimensions)
    weight = parse_package_weight(text)
    if weight is not None:
        parsed.update(weight)
    return parsed or None


def load_login(env_file: Path | None = None) -> tuple[str, str]:
    path = env_file or Path(__file__).resolve().parent.parent / ".env.local"
    if not path.is_file():
        raise SellerSpriteError("没有找到 .env.local，无法登录卖家精灵")
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    username = values.get("E2E_USERNAME") or ""
    password = values.get("E2E_PASSWORD") or ""
    if not username or not password:
        raise SellerSpriteError("请在 .env.local 配置 E2E_USERNAME 和 E2E_PASSWORD")
    return username, password


MARK_SOURCE_BUTTON_JS = r"""
(asin) => {
  const wanted = String(asin || "").toUpperCase();
  document.querySelectorAll("[data-seller-auto-source]").forEach((el) => el.removeAttribute("data-seller-auto-source"));
  const norm = (el) => (el.innerText || "").replace(/\s+/g, "");
  const rows = [...document.querySelectorAll("tr")].filter((tr) => (tr.innerText || "").toUpperCase().includes(wanted));
  rows.sort((a, b) => (a.innerText || "").length - (b.innerText || "").length);
  const row = rows[0];
  if (!row) return "no-row";
  const scopes = [row];
  let sibling = row.nextElementSibling;
  for (let index = 0; sibling && index < 3; index += 1, sibling = sibling.nextElementSibling) scopes.push(sibling);
  const match = (el) => {
    const text = norm(el);
    if (!text.includes("1688找货源")) return false;
    return ![...el.children].some((child) => norm(child).includes("1688找货源"));
  };
  for (const scope of scopes) {
    const hit = [...scope.querySelectorAll("a, button, span, div")].find(match);
    if (!hit) continue;
    hit.setAttribute("data-seller-auto-source", "1");
    try { hit.scrollIntoView({block: "center"}); } catch (error) {}
    return "marked";
  }
  return "no-button";
}
"""


def open_competitor(browser: Any, asin: str) -> tuple[dict[str, str] | None, Any | None]:
    """打开查竞品页并读包装。页面留着，方便接着点这一行下面的 1688找货源。"""

    target = (asin or "").strip().upper()
    if not _ASIN_RE.fullmatch(target):
        step("卖家精灵", "这条没有可查询的 ASIN，跳过包装尺寸")
        return None, None
    _use_chinese(browser)
    step("卖家精灵", f"打开查竞品 {target}")
    page = browser.open_detail(competitor_lookup_url(target), viewport=LOOKUP_VIEWPORT)
    state = _wait_state(page, target)
    if state["status"] == "login":
        _login(browser, page)
        _use_chinese(browser)
        browser.goto(page, competitor_lookup_url(target))
        state = _wait_state(page, target)
        if state["status"] == "login":
            raise SellerSpriteError("登录后仍然提示未登录")
    if state["status"] != "table":
        step("卖家精灵", "表格里没有读到重量和尺寸")
        return None, page
    parsed = parse_package_cell(state["text"])
    if parsed is None:
        snippet = " ".join((state.get("text") or "").split())
        if len(snippet) > 80:
            snippet = snippet[:80] + "…"
        step("卖家精灵", f"这一列没有重量和长宽高：{snippet or '空'}")
        return None, page
    parts: list[str] = []
    if "weight" in parsed:
        parts.append(f"{parsed['weight']} {parsed['weight_unit']}")
    if "length" in parsed:
        unit = parsed.get("unit") or ""
        dims = [parsed["length"], parsed["width"]]
        if parsed.get("height"):
            dims.append(parsed["height"])
        parts.append((" x ".join(dims) + (f" {unit}" if unit else "")).rstrip())
    step("卖家精灵", "已记录包装 " + "，".join(parts))
    return parsed, page


def read_package_size(browser: Any, asin: str) -> dict[str, str] | None:
    try:
        package, _page = open_competitor(browser, asin)
        return package
    finally:
        browser.close_detail()


def open_1688_from_row(browser: Any, page: Any, asin: str) -> Any | None:
    """悬停商品行，点该行下方出现的「1688找货源」，进入对应页面。"""

    target = (asin or "").strip().upper()
    step("1688", "在卖家精灵这一行下方点击1688找货源")
    row = page.locator("tr", has_text=target).first
    marked = "no-button"
    for _ in range(20):
        try:
            row.hover(timeout=2000)
        except Exception:
            pass
        try:
            marked = str(page.evaluate(MARK_SOURCE_BUTTON_JS, target) or "")
        except Exception:
            marked = "no-button"
        if marked == "marked":
            break
        try:
            page.wait_for_timeout(400)
        except Exception:
            break
    if marked != "marked":
        step("1688", "这一行下面没有出现1688找货源")
        return None
    button = page.locator("[data-seller-auto-source='1']").first
    opened = page
    clicked = False
    try:
        with browser.context.expect_page(timeout=8000) as caught:
            button.click(timeout=5000)
            clicked = True
        opened = caught.value
    except Exception:
        if not clicked:
            try:
                button.click(timeout=5000)
            except Exception as exc:
                step("1688", f"没有点开1688找货源：{exc}")
                return None
        opened = page
    try:
        opened.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    browser.detail_page = opened
    try:
        browser.trim_tabs()
    except Exception:
        pass
    current = browser.detail_page or opened
    url = ""
    try:
        url = current.url or ""
    except Exception:
        url = ""
    if "1688.com" not in url:
        try:
            page.wait_for_timeout(1500)
            url = (browser.detail_page or current).url or ""
        except Exception:
            url = ""
        current = browser.detail_page or current
    if "1688.com" not in url:
        step("1688", "点击后没有进入1688页面")
        return None
    step("1688", "已进入1688找货源页面")
    return current


def _use_chinese(browser: Any) -> None:
    from seller_auto.browser import browser_closed_error

    cookies = [
        {
            "name": "ecookie",
            "value": "cn",
            "url": "https://www.sellersprite.com/",
        }
    ]
    browser.start()
    try:
        browser.context.add_cookies(cookies)
    except Exception as exc:
        if not browser_closed_error(exc):
            raise
        step("卖家精灵", "资料浏览器已断开，重新打开后再设中文")
        browser.close()
        browser.start()
        browser.context.add_cookies(cookies)


def _wait_state(page: Any, asin: str) -> dict[str, str]:
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    def read() -> dict[str, str] | None:
        try:
            handle = page.wait_for_function(LOOKUP_STATE_JS, arg=asin, timeout=LOOKUP_TIMEOUT_MS)
        except PlaywrightTimeout:
            return None
        value = handle.json_value()
        if not isinstance(value, dict):
            return None
        return {"status": str(value.get("status") or "timeout"), "text": str(value.get("text") or "")}

    state = read()
    if state is None:
        return {"status": "timeout", "text": ""}
    if state["status"] != "login":
        return state
    # 页头会先闪出「未登录」，登录名晚一拍才换上。确认一下再去登录页。
    try:
        page.wait_for_timeout(2000)
    except PlaywrightTimeout:
        return state
    try:
        again = page.evaluate(LOOKUP_STATE_JS, asin)
    except Exception:
        return state
    if isinstance(again, dict) and again.get("status") and again.get("status") != "login":
        return {"status": str(again.get("status")), "text": str(again.get("text") or "")}
    if again is None:
        return read() or state
    return state


def _show_account_form(page: Any) -> Any:
    """右上角先切到账号登录。密码框已经在这一页时，不要去点隐藏的「密码登录」。"""

    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    account = page.get_by_text("账号登录", exact=True)
    if account.count() and account.first.is_visible():
        account.first.click()
    form = page.locator("#form_signin_passW")
    try:
        form.wait_for(state="visible", timeout=8000)
    except PlaywrightTimeout:
        link = page.locator("#login-password")
        if link.count() and link.is_visible():
            link.click()
            form.wait_for(state="visible", timeout=8000)
        else:
            raise SellerSpriteError("账号登录里没有出现密码框")
    return form


def _login(browser: Any, page: Any) -> None:
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    username, password = load_login()
    step("卖家精灵", "页面提示未登录，改去登录页")
    try:
        browser.goto(page, LOGIN_URL)
    except Exception as exc:
        if "ERR_ABORTED" not in str(exc):
            raise
    try:
        page.wait_for_function(
            "() => !location.pathname.includes('/user/login') || !!(window.CryptoJS && window.jQuery)",
            timeout=20000,
        )
    except PlaywrightTimeout as exc:
        raise SellerSpriteError("登录页没有加载完成") from exc
    if "/user/login" not in (page.url or ""):
        step("卖家精灵", "登录态还在，回到查竞品")
        return
    step("卖家精灵", "切换到账号登录")
    form = _show_account_form(page)
    form.locator("input[name='email']").fill(username)
    form.locator("input[type='password']").fill(password)
    form.locator("button.login-btn").click()
    _finish_login(page)


def _finish_login(page: Any) -> None:
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    try:
        page.wait_for_function(
            """() => {
                if (!(location.pathname || '').includes('/user/login')) return 'ok';
                const force = [...document.querySelectorAll('button')].some((button) => {
                    return (button.innerText || '').includes('强制登录') && button.getClientRects().length > 0;
                });
                if (force) return 'force';
                const error = document.querySelector('#form_signin_passW .login_error_message');
                if (error && (error.innerText || '').trim()) return 'error';
                return null;
            }""",
            timeout=25000,
        )
    except PlaywrightTimeout as exc:
        raise SellerSpriteError("登录没有完成") from exc
    state = page.evaluate(
        """() => {
            if (!(location.pathname || '').includes('/user/login')) return 'ok';
            const force = [...document.querySelectorAll('button')].some((button) => {
                return (button.innerText || '').includes('强制登录') && button.getClientRects().length > 0;
            });
            if (force) return 'force';
            const error = document.querySelector('#form_signin_passW .login_error_message');
            const message = error ? (error.innerText || '').trim() : '';
            return message ? 'error:' + message : 'stay';
        }"""
    )
    if state == "ok":
        return
    if state == "force":
        step("卖家精灵", "账号已在别处登录，确认强制登录")
        page.locator("button", has_text="强制登录").first.click()
        try:
            page.wait_for_function("() => !(location.pathname || '').includes('/user/login')", timeout=25000)
        except PlaywrightTimeout as exc:
            raise SellerSpriteError("强制登录后仍停留在登录页") from exc
        return
    if str(state).startswith("error:"):
        raise SellerSpriteError(str(state).split(":", 1)[1] or "登录失败")
    raise SellerSpriteError("登录没有完成")
