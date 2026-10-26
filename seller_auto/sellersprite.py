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
_DIMENSION_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(?:cm|in|inches|厘米|英寸)?\s*[x×*＊]\s*"
    r"(\d+(?:[.,]\d+)?)\s*(?:cm|in|inches|厘米|英寸)?\s*[x×*＊]\s*"
    r"(\d+(?:[.,]\d+)?)\s*(cm|in|inches|厘米|英寸)?",
    re.IGNORECASE,
)
_WEIGHT_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(kg|g|lb|lbs|oz|千克|公斤|克|磅|盎司)(?!\w)",
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
}

# 表头 .cell 的文字包含「包装重量」。站点语言偶发是英文或日文时，认同一列。
LOOKUP_STATE_JS = r"""
(asin) => {
  const markers = ["包装重量", "Package Weight", "梱包重量"];
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
  const readCell = () => {
    const wraps = [...document.querySelectorAll(".el-table__header-wrapper, [class*='table-header']")];
    let headerOnly = false;
    for (const wrap of wraps) {
      if (wrap.closest(".el-table__fixed, .el-table__fixed-header-wrapper")) continue;
      const rows = wrap.matches("tr") ? [wrap] : [...wrap.querySelectorAll("thead tr, tr")];
      for (const tr of rows) {
        const ths = [...tr.children].filter((el) => el.tagName === "TH" || el.classList.contains("cell"));
        const index = ths.findIndex((th) => markers.some((marker) => (th.innerText || "").includes(marker)));
        if (index < 0) continue;
        const table = wrap.closest(".el-table") || wrap.parentElement;
        const body = table && table.querySelector(".el-table__body-wrapper");
        const dataRows = body ? [...body.querySelectorAll("tbody tr.el-table__row")] : [];
        if (!dataRows.length) {
          headerOnly = true;
          continue;
        }
        const wanted = String(asin || "").toUpperCase();
        const row = dataRows.find((item) => (item.innerText || "").toUpperCase().includes(wanted)) || dataRows[0];
        const tds = [...row.children].filter((el) => el.tagName === "TD");
        const cells = tds.length ? tds : [...row.children];
        return {found: true, rows: true, text: ((cells[index] && cells[index].innerText) || "").trim()};
      }
    }
    return {found: headerOnly, rows: false, text: ""};
  };
  const cell = readCell();
  if (cell.rows) return {status: "table", text: cell.text};
  if (needsLogin()) return {status: "login", text: ""};
  if ([...document.querySelectorAll(".el-loading-mask")].some(isShown)) return null;
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


def parse_package_dimensions(text: str) -> dict[str, str] | None:
    match = _DIMENSION_RE.search((text or "").replace("\u00a0", " "))
    if match is None:
        return None
    unit = (match.group(4) or "").lower()
    if unit in {"in", "inches", "英寸"}:
        unit = "in"
    elif unit in {"cm", "厘米"}:
        unit = "cm"
    parsed = {
        "length": _plain_number(match.group(1)),
        "width": _plain_number(match.group(2)),
        "height": _plain_number(match.group(3)),
    }
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


def read_package_size(browser: Any, asin: str) -> dict[str, str] | None:
    target = (asin or "").strip().upper()
    if not _ASIN_RE.fullmatch(target):
        step("卖家精灵", "这条没有可查询的 ASIN，跳过包装尺寸")
        return None
    try:
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
            step("卖家精灵", "表格里没有读到包装重量列")
            return None
        parsed = parse_package_cell(state["text"])
        if parsed is None:
            step("卖家精灵", "包装重量列里没有重量和长宽高")
            return None
        parts: list[str] = []
        if "weight" in parsed:
            parts.append(f"{parsed['weight']} {parsed['weight_unit']}")
        if "length" in parsed:
            unit = parsed.get("unit") or ""
            parts.append(f"{parsed['length']} x {parsed['width']} x {parsed['height']} {unit}".rstrip())
        step("卖家精灵", "已记录包装 " + "，".join(parts))
        return parsed
    finally:
        browser.close_detail()


def _use_chinese(browser: Any) -> None:
    browser.start()
    browser.context.add_cookies(
        [
            {
                "name": "ecookie",
                "value": "cn",
                "url": "https://www.sellersprite.com/",
            }
        ]
    )


def _wait_state(page: Any, asin: str) -> dict[str, str]:
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    try:
        handle = page.wait_for_function(LOOKUP_STATE_JS, arg=asin, timeout=LOOKUP_TIMEOUT_MS)
    except PlaywrightTimeout:
        return {"status": "timeout", "text": ""}
    value = handle.json_value()
    if not isinstance(value, dict):
        return {"status": "timeout", "text": ""}
    return {"status": str(value.get("status") or "timeout"), "text": str(value.get("text") or "")}


def _login(browser: Any, page: Any) -> None:
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    username, password = load_login()
    step("卖家精灵", "页面提示未登录，改去登录页")
    browser.goto(page, LOGIN_URL)
    try:
        page.wait_for_function("() => !!(window.CryptoJS && window.jQuery)", timeout=20000)
    except PlaywrightTimeout as exc:
        raise SellerSpriteError("登录页没有加载完成") from exc
    form = page.locator("#form_signin_passW")
    if form.count() == 0 or not form.is_visible():
        page.locator("#login-password").click()
        form = page.locator("#form_signin_passW")
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
