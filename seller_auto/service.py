"""抓取会话。浏览器操作都在同一条线程上执行。"""

from __future__ import annotations

import queue
import threading
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from seller_auto.amazon import (
    has_next_page,
    is_seller_list,
    list_title_entries,
    product_image_locator,
    read_product,
    title_locator,
)
from seller_auto.browser import NEEDS_HUMAN, ArgentinaBrowser, log
from seller_auto.categories import category_reason, classify_breadcrumb
from seller_auto.images import save_original, to_client_jpeg
from seller_auto.lingdong import collect_market_quotes, quote_links
from seller_auto.matches import (
    TITLE_FILTER_MODEL,
    TITLE_MISMATCH_REASON,
    MatchInputError,
    filter_matches,
    title_obviously_different,
)
from seller_auto.mercadolibre import (
    collect_ad_cards,
    collect_grid_cards,
    hero_image_url,
    hero_locator,
    open_next_results_page,
    read_price,
    read_sales_text,
    sales_from_text,
)
from seller_auto.workbook import template_path, write_product_workbook
from seller_auto.pacing import DelaySettings
from seller_auto.sales import sales_action
from seller_auto.sellersprite import read_package_size
from seller_auto.sourcing import read_source_average
from seller_auto.progress import next_required_step
from seller_auto.storage import (
    atomic_write_json,
    build_export,
    discard_finished_images,
    export_filename,
    find_state_by_session,
    load_json,
    seller_dir,
    similarity_rows,
    state_path,
    public_url,
)
from seller_auto.step_log import step
from seller_auto.urls import UrlError, clean_keywords, ml_search_url, parse_focus_product, resolve_store_input

BATCH_SIZE = 24


@dataclass
class ToolPayload:
    data: dict[str, Any]
    images: list[tuple[str, bytes]] = field(default_factory=list)


class _Worker:
    def __init__(self, name: str = "seller-auto-browser") -> None:
        self._queue: queue.Queue[Any] = queue.Queue()
        self._thread = threading.Thread(target=self._loop, name=name, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            function, args, kwargs, slot = item
            try:
                slot.put((True, function(*args, **kwargs)))
            except Exception as exc:
                slot.put((False, exc))

    def submit(self, function: Callable[..., Any], *args: Any, **kwargs: Any) -> queue.Queue[Any]:
        slot: queue.Queue[Any] = queue.Queue(maxsize=1)
        self._queue.put((function, args, kwargs, slot))
        return slot

    def call(self, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        ok, value = self.submit(function, *args, **kwargs).get()
        if not ok:
            raise value
        return value


def _storage_id(state: dict[str, Any]) -> str:
    parts = [state["seller_id"]]
    run_id = state.get("run_id") or ""
    focus = state.get("single_product") or ""
    if run_id:
        parts.append(run_id)
    if focus:
        parts.append(f"focus-{focus}")
    return "/".join(parts)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _empty_listing() -> dict[str, Any]:
    return {"page1_done": False, "page2_done": False, "candidates": [], "next_index": 0}


def _new_state(session_id: str, seller_id: str, store_url: str) -> dict[str, Any]:
    timestamp = _now()
    return {
        "session_id": session_id,
        "seller_id": seller_id,
        "store_url": store_url,
        "created_at": timestamp,
        "updated_at": timestamp,
        "list_url": store_url,
        "amazon_page": 1,
        "amazon_done": False,
        "single_product": "",
        "run_id": "",
        "visited": [],
        "excluded": [],
        "products": {},
        "product_order": [],
        "current_product_id": None,
        "challenge_pending": False,
        "challenge_site": "",
    }


class SellerAutoService:
    def __init__(
        self,
        settings: DelaySettings,
        output_dir: str | None = None,
        profile_dir: str | None = None,
        workbook_path: str | None = None,
        headless: bool | None = None,
        worker: _Worker | None = None,
    ) -> None:
        self.settings = settings.validate()
        self.output_dir = Path(output_dir or settings.output_dir)
        self.workbook_path = Path(workbook_path) if workbook_path else template_path()
        self.profile_dir = Path(profile_dir or settings.profile_dir)
        self.headless = settings.headless if headless is None else headless
        self.browser: ArgentinaBrowser | None = None
        self.enrich_browser: ArgentinaBrowser | None = None
        self.state: dict[str, Any] | None = None
        self._worker = worker or _Worker()
        self._enrich_worker = _Worker(name="seller-auto-enrich")
        self._enrich_events: dict[str, threading.Event] = {}
        self._state_lock = threading.Lock()

    def open_store(self, seller_id: str, product: str = "") -> ToolPayload:
        return self._guard(self._open_store, seller_id, product)

    def next_amazon_product(self) -> ToolPayload:
        return self._guard(self._next_amazon_product)

    def set_keywords(self, product_id: str, keywords: str, chinese_name: str = "") -> ToolPayload:
        return self._guard(self._set_keywords, product_id, keywords, chinese_name)

    def next_listing_batch(self) -> ToolPayload:
        return self._guard(self._next_listing_batch)

    def submit_listing_matches(self, matches: list[Any], model_name: str = "") -> ToolPayload:
        return self._guard(self._submit_listing_matches, matches, model_name)

    def next_ad_batch(self, product_id: str) -> ToolPayload:
        return self._guard(self._next_ad_batch, product_id)

    def submit_ad_matches(self, matches: list[Any], model_name: str = "") -> ToolPayload:
        return self._guard(self._submit_ad_matches, matches, model_name)

    def session_status(self) -> ToolPayload:
        return self._guard(self._session_status)

    def resume_session(self, session_id: str) -> ToolPayload:
        return self._guard(self._resume_session, session_id)

    def export_results(self) -> ToolPayload:
        return self._guard(self._export_results)

    def _guard(self, function: Callable[..., ToolPayload], *args: Any) -> ToolPayload:
        try:
            return self._worker.call(self._invoke, function, *args)
        except Exception as exc:
            log(traceback.format_exc())
            message = str(exc)
            if "Executable doesn't exist" in message or "browserType.launch" in message:
                message = f"{message}。请先在项目目录执行 .venv/bin/python -m playwright install chromium"
            step("错误", message)
            return ToolPayload({"ok": False, "status": "error", "message": message})

    def _invoke(self, function: Callable[..., ToolPayload], *args: Any) -> ToolPayload:
        try:
            return function(*args)
        except _Blocked as blocked:
            return self._blocked(blocked.payload)
        except (UrlError, MatchInputError) as exc:
            return ToolPayload({"ok": False, "status": "error", "message": str(exc)})

    def _ensure_browser(self) -> ArgentinaBrowser:
        if self.browser is None:
            self.browser = ArgentinaBrowser(self.settings, self.profile_dir, headless=self.headless)
        self.browser.start()
        assert self.browser is not None
        return self.browser

    def _require_state(self) -> dict[str, Any]:
        if self.state is None:
            raise UrlError("还没有会话。请先调用 open_store 或 resume_session")
        return self.state

    def _save(self) -> None:
        with self._state_lock:
            self._write_state()

    def _write_state(self) -> None:
        state = self._require_state()
        if self.browser is not None:
            state["challenge_pending"] = self.browser.challenge_pending
            state["challenge_site"] = self.browser.challenge_site
        state["updated_at"] = _now()
        atomic_write_json(state_path(self.output_dir, _storage_id(state)), state)

    def close(self) -> None:
        def close_browser(current: ArgentinaBrowser | None) -> None:
            if current is not None:
                current.close()

        try:
            self._worker.call(close_browser, self.browser)
        except Exception:
            log("关闭主浏览器失败")
        self.browser = None
        try:
            self._enrich_worker.call(close_browser, self.enrich_browser)
        except Exception:
            log("关闭资料查询浏览器失败")
        self.enrich_browser = None

    def _apply_challenge_flag(self) -> None:
        browser = self._ensure_browser()
        state = self._require_state()
        browser.challenge_pending = bool(state.get("challenge_pending"))
        browser.challenge_site = state.get("challenge_site") or ""

    def _status(self) -> dict[str, Any]:
        state = self._require_state()
        current = state.get("current_product_id")
        remaining = 0
        if current and current in state["products"]:
            listing = state["products"][current]["listing"]
            remaining = max(0, len(listing["candidates"]) - int(listing["next_index"]))
        return {
            "session_id": state["session_id"],
            "seller_id": state["seller_id"],
            "store_url": state["store_url"],
            "amazon_done": state["amazon_done"],
            "amazon_page": state["amazon_page"],
            "visited": len(state["visited"]),
            "excluded": len(state["excluded"]),
            "kept": len(state["product_order"]),
            "current_product_id": current,
            "listing_remaining": remaining,
            "next_step": next_required_step(state),
        }

    def _payload(self, data: dict[str, Any], images: list[tuple[str, str]] | None = None) -> ToolPayload:
        if self.state is not None:
            data = {**self._status(), **data}
        encoded: list[tuple[str, bytes]] = []
        for caption, path in images or []:
            jpeg = _jpeg_file(path)
            if jpeg is not None:
                encoded.append((caption, jpeg))
        status = data.get("status") or ""
        if status:
            step(str(status), str(data.get("message") or ""))
        return ToolPayload(data, encoded)

    def _blocked(self, blocked: dict[str, Any]) -> ToolPayload:
        self._save()
        return self._payload(blocked)

    def _open_store(self, seller_id: str, product: str = "") -> ToolPayload:
        seller_id, store_url = resolve_store_input(seller_id)
        focus = parse_focus_product(product)
        browser = self._ensure_browser()
        session_id = str(uuid.uuid4())
        self.state = _new_state(session_id, seller_id, store_url)
        self.state["single_product"] = focus
        self.state["run_id"] = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + session_id[:8]
        self._apply_challenge_flag()
        browser.amazon_cooldown.acquire()
        if focus:
            target = f"https://www.amazon.com.mx/dp/{focus}"
        else:
            target = store_url
        browser.goto(browser.list_page, target)
        blocked = browser.ensure_clear(browser.list_page, "amazon")
        if blocked:
            return self._blocked(blocked)
        browser.pause_amazon_read()
        if not focus:
            self.state["list_url"] = browser.list_page.url
            self._save()
            return self._payload(
                {
                    "ok": True,
                    "status": "store_ready",
                    "resumed": False,
                    "single_product": "",
                    "message": "新的一轮，店铺已打开。请调用 next_amazon_product。不要恢复旧会话。",
                }
            )
        return self._save_opened_amazon_product(
            browser,
            browser.list_page,
            False,
            {"index": -1, "href": target, "asin": focus, "text": "", "key": focus},
        )

    def _resume_session(self, session_id: str) -> ToolPayload:
        path = find_state_by_session(self.output_dir, session_id.strip())
        if path is None:
            return ToolPayload({"ok": False, "status": "error", "message": "没有找到这个会话"})
        self.state = load_json(path)
        self._clear_prefetch_flags()
        self._ensure_browser()
        self._apply_challenge_flag()
        self._save()
        step_name = next_required_step(self.state)
        return self._payload(
            {
                "ok": True,
                "status": "resumed",
                "message": f"已从本地进度恢复。请继续 {step_name}，不要重新打开店铺或重设关键词。",
            }
        )

    def _session_status(self) -> ToolPayload:
        self._require_state()
        return self._payload({"ok": True, "status": "status"})

    def _export_results(self) -> ToolPayload:
        self._wait_enrichment()
        market_note = self._collect_market_quotes()
        state = self._require_state()
        payload = build_export(state)
        directory = seller_dir(self.output_dir, _storage_id(state))
        path = directory / export_filename()
        atomic_write_json(path, payload)
        workbook_path = self.workbook_path
        products = [state["products"][product_id] for product_id in state.get("product_order", [])]
        written = write_product_workbook(workbook_path, products)
        removed = self._discard_finished_images()
        if written:
            if state.get("single_product"):
                message = f"这一条商品的结果已追加到 {workbook_path}。不要再调用 next_amazon_product。"
            elif state.get("amazon_done"):
                message = f"店铺列表已遍历完，结果已追加到 {workbook_path}。"
            else:
                message = f"这是部分结果，已追加到 {workbook_path}。店铺还有未查看的商品，请继续 next_amazon_product，直到返回 end_of_store。"
        elif state.get("single_product"):
            message = "这一条没有同款，没有写入表格。不要再调用 next_amazon_product。"
        elif state.get("amazon_done"):
            message = "店铺列表已遍历完，没有同款，没有写入表格。"
        else:
            message = "当前没有同款，没有写入表格。店铺还有未查看的商品，请继续 next_amazon_product，直到返回 end_of_store。"
        if removed:
            message = f"{message} 已删除过程图片 {removed} 个。"
        if market_note:
            message = f"{message} {market_note}"
        return self._payload(
            {"ok": True, "status": "exported", "path": str(path), "workbook": str(workbook_path), "message": message}
        )

    def _discard_finished_images(self) -> int:
        try:
            removed = discard_finished_images(self.output_dir)
        except Exception as exc:
            log(f"清理过程图片失败：{exc}")
            return 0
        if removed:
            step("清理", f"已删除过程图片 {removed} 个")
        return removed

    def _next_amazon_product(self) -> ToolPayload:
        state = self._require_state()
        if state["amazon_done"]:
            return self._payload({"ok": True, "status": "end_of_store", "message": "店铺列表已经读完"})
        if self.settings.max_products and len(state["product_order"]) >= self.settings.max_products:
            return self._payload({"ok": True, "status": "limit_reached", "message": "已达到 max_products"})
        browser = self._ensure_browser()
        focus = state.get("single_product") or ""
        if focus:
            if focus in state["products"]:
                saved = state["products"][focus]
                return self._payload(
                    {
                        "ok": True,
                        "status": "amazon_product",
                        "product_id": focus,
                        "title": saved.get("title") or "",
                        "brand": saved.get("brand") or "",
                        "url": saved.get("url") or "",
                        "message": "这个商品已经读过。请直接 set_keywords，不要再打开。",
                    },
                    [(f"亚马逊主图 product_id={focus}", saved.get("image_path") or "")],
                )
            if focus in state["visited"]:
                state["amazon_done"] = True
                self._save()
                return self._payload(
                    {
                        "ok": True,
                        "status": "end_of_store",
                        "message": f"指定商品 {focus} 已结束。不要再翻店铺。",
                    }
                )
            browser.close_detail()
            url = f"https://www.amazon.com.mx/dp/{focus}"
            current = browser.list_page.url or ""
            if focus not in current:
                browser.amazon_cooldown.acquire()
                browser.goto(browser.list_page, url)
            blocked_page = browser.ensure_clear(browser.list_page, "amazon")
            if blocked_page:
                return self._blocked(blocked_page)
            browser.pause_amazon_read()
            page = browser.list_page
            opened_new_tab = False
            entry = {"index": -1, "href": url, "asin": focus, "text": "", "key": focus}
        else:
            blocked = self._ensure_amazon_list()
            if blocked:
                return blocked
            entry = self._next_unvisited_entry()
            if entry is None:
                if not self._turn_amazon_page():
                    state["amazon_done"] = True
                    self._save()
                    return self._payload({"ok": True, "status": "end_of_store", "message": "店铺列表已经读完"})
                entry = self._next_unvisited_entry()
                if entry is None:
                    state["amazon_done"] = True
                    self._save()
                    return self._payload({"ok": True, "status": "end_of_store", "message": "这一页没有新的商品"})
            browser.amazon_cooldown.acquire()
            locator = title_locator(browser.list_page, entry["index"])
            page, opened_new_tab = browser.click_result(locator)
            blocked_page = browser.ensure_clear(page, "amazon")
            if blocked_page:
                return self._blocked(blocked_page)
            page = browser.live_page(page)
            browser.pause_amazon_read()
        return self._save_opened_amazon_product(browser, page, opened_new_tab, entry)

    def _save_opened_amazon_product(
        self,
        browser: ArgentinaBrowser,
        page: Any,
        opened_new_tab: bool,
        entry: dict[str, Any],
    ) -> ToolPayload:
        state = self._require_state()
        try:
            page.wait_for_selector("#productTitle", timeout=15000)
        except Exception:
            if browser.needs_verification(page):
                browser.challenge_pending = True
                browser.challenge_site = "amazon"
                return self._blocked(
                    {"ok": False, "status": "needs_human_verification", "message": NEEDS_HUMAN, "url": page.url}
                )
            self._mark_visited(entry["key"])
            self._leave_amazon_detail(browser, opened_new_tab, state)
            self._save()
            return self._payload(
                {
                    "ok": False,
                    "status": "error",
                    "message": "没有读到商品标题，已跳过这一条",
                    "url": entry["href"],
                }
            )
        product = read_product(page)
        key = product["asin"] or entry["key"]
        self._mark_visited(entry["key"])
        self._mark_visited(key)
        label = classify_breadcrumb(product["breadcrumb"])
        if label:
            reason = category_reason(label) or label
            state["excluded"].append(
                {
                    "product_id": key,
                    "url": product["url"] or entry["href"],
                    "title": product["title"] or entry["text"],
                    "reason": reason,
                }
            )
            self._leave_amazon_detail(browser, opened_new_tab, state)
            if state.get("single_product"):
                state["amazon_done"] = True
                message = f"已跳过{reason}类商品。这一条已结束，请 export_results，不要再翻店铺。"
            else:
                message = f"已跳过{reason}类商品。请继续调用 next_amazon_product。"
            self._save()
            return self._payload(
                {
                    "ok": True,
                    "status": "excluded",
                    "reason": reason,
                    "product_id": key,
                    "title": product["title"] or entry["text"],
                    "url": product["url"] or entry["href"],
                    "message": message,
                }
            )
        image_path = self._download(
            product["image_url"],
            self._image_stem(key),
            product_image_locator(page),
            page.url,
        )
        state["products"][key] = {
            "product_id": key,
            "url": product["url"] or entry["href"],
            "title": product["title"] or entry["text"],
            "brand": product["brand"],
            "image_path": image_path,
            "image_url": product.get("image_url") or "",
            "keywords": "",
            "ml_search_url": "",
            "listing": _empty_listing(),
            "judged_ids": [],
            "checked_keys": [],
            "matches": [],
            "package_size": None,
            "source_price": None,
        }
        state["product_order"].append(key)
        state["current_product_id"] = key
        self._leave_amazon_detail(browser, opened_new_tab, state)
        self._save()
        self._schedule_enrichment(key, product.get("image_url") or "")
        if state.get("single_product"):
            message = (
                "请根据主图给出西班牙语搜索关键词，然后调用 set_keywords。"
                "卖家精灵和货源查询已在后台并行开始，不要等它们。"
                "这是指定的一条商品，不要再调用 next_amazon_product。"
            )
        else:
            message = (
                "请根据主图给出西班牙语搜索关键词，然后调用 set_keywords。"
                "卖家精灵和货源查询已在后台并行开始，不要等它们。"
                "美客多流程结束后再调用 next_amazon_product。"
            )
        return self._payload(
            {
                "ok": True,
                "status": "amazon_product",
                "product_id": key,
                "title": state["products"][key]["title"],
                "brand": product["brand"],
                "url": state["products"][key]["url"],
                "message": message,
            },
            [(f"亚马逊主图 product_id={key}", image_path)],
        )

    def _schedule_enrichment(self, product_id: str, image_url: str) -> None:
        event = threading.Event()
        self._enrich_events[product_id] = event
        step("资料", f"{product_id} 的卖家精灵和货源查询已在后台开始，美客多比对可以同时进行")
        self._enrich_worker.submit(self._run_enrichment, product_id, image_url, event)

    def _wait_enrichment(self) -> None:
        events = list(self._enrich_events.values())
        if not events:
            return
        pending = [event for event in events if not event.is_set()]
        if pending:
            step("资料", "导出前等待卖家精灵和货源查询结束")
        for event in events:
            event.wait()

    def _collect_market_quotes(self) -> str:
        state = self._require_state()
        jobs = []
        for product_id in state.get("product_order", []):
            product = state["products"].get(product_id) or {}
            links = quote_links(product)
            if not links:
                continue
            jobs.append({"product_id": product_id, "links": links, "package": product.get("package_size")})
        if not jobs:
            return ""
        try:
            return self._enrich_worker.call(self._run_market_quotes, jobs)
        except Exception as exc:
            step("蓝鲸", f"查询失败：{exc}")
            log(traceback.format_exc())
            return "蓝鲸选品没有查成，名称和 FBA 运费这次没写。"

    def _run_market_quotes(self, jobs: list[dict[str, Any]]) -> str:
        browser = self._ensure_enrich_browser()
        quotes = collect_market_quotes(
            browser,
            [(job["links"], job.get("package")) for job in jobs],
        )
        wrote = False
        state = self._require_state()
        for job, quote in zip(jobs, quotes):
            product = state["products"].get(job["product_id"])
            if not product:
                continue
            if quote.get("orders") is not None:
                product["market_orders"] = quote["orders"]
                wrote = True
            if quote.get("fba_fee") is not None:
                product["fba_fee"] = quote["fba_fee"]
                wrote = True
        self._save()
        if wrote:
            return "蓝鲸选品已把 30 天销量写入名称，并把尾程运费写入 FBA 运费。"
        return "蓝鲸选品没有查到这些链接，名称和 FBA 运费保持原样。"

    def _ensure_enrich_browser(self) -> ArgentinaBrowser:
        if self.enrich_browser is None:
            enrich_dir = Path(str(self.profile_dir) + "-enrich")
            self.enrich_browser = ArgentinaBrowser(self.settings, enrich_dir, headless=self.headless)
        self.enrich_browser.start()
        assert self.enrich_browser is not None
        return self.enrich_browser

    def _run_enrichment(self, product_id: str, image_url: str, event: threading.Event) -> None:
        try:
            browser = self._ensure_enrich_browser()
            package = self._read_package_size(browser, product_id)
            source = self._read_source_price(browser, image_url)
            with self._state_lock:
                if self.state and product_id in self.state.get("products", {}):
                    self.state["products"][product_id]["package_size"] = package
                    self.state["products"][product_id]["source_price"] = source
                    self._write_state()
            step("资料", f"{product_id} 包装和货源已写回")
        except Exception as exc:
            step("资料", f"{product_id} 后台查询失败：{exc}")
            log(traceback.format_exc())
        finally:
            event.set()

    def _read_package_size(self, browser: ArgentinaBrowser, asin: str) -> dict[str, str] | None:
        try:
            return read_package_size(browser, asin)
        except Exception as exc:
            step("卖家精灵", f"没有读到包装尺寸：{exc}")
            log(traceback.format_exc())
            try:
                browser.close_detail()
            except Exception:
                log("关闭卖家精灵标签失败")
            return None

    def _read_source_price(self, browser: ArgentinaBrowser, image_url: str) -> dict[str, Any] | None:
        try:
            return read_source_average(browser, image_url)
        except Exception as exc:
            step("1688", f"没有读到货源均价：{exc}")
            log(traceback.format_exc())
            try:
                browser.close_detail()
            except Exception:
                log("关闭1688标签失败")
            return None

    def _leave_amazon_detail(self, browser: ArgentinaBrowser, opened_new_tab: bool, state: dict[str, Any]) -> None:
        if state.get("single_product"):
            browser.close_detail()
            return
        browser.finish_detail(opened_new_tab, state["list_url"])

    def _set_keywords(self, product_id: str, keywords: str, chinese_name: str = "") -> ToolPayload:
        state = self._require_state()
        product = self._product(product_id)
        text = clean_keywords(keywords)
        if chinese_name.strip():
            product["chinese_name"] = chinese_name.strip()
        search_url = ml_search_url(text)
        fresh = product.get("keywords") != text
        if fresh:
            product["keywords"] = text
            product["ml_search_url"] = search_url
            product["listing"] = _empty_listing()
        state["current_product_id"] = product_id
        browser = self._ensure_browser()
        browser.close_detail()
        browser.ml_cooldown.acquire()
        browser.goto(browser.list_page, search_url)
        blocked = browser.ensure_clear(browser.list_page, "ml")
        if blocked:
            self._save()
            return self._blocked(blocked)
        browser.pause_ml_scroll()
        self._save()
        payload = self._next_listing_batch()
        payload.data["keywords"] = text
        payload.data["ml_search_url"] = search_url
        if fresh and payload.data.get("status") == "listing_batch":
            payload.data["message"] = (
                "搜索结果已打开。下面是第一批列表图。"
                + self._compare_message("listing", payload.data.get("title_dropped") or [])
            )
        return payload

    def _next_listing_batch(self) -> ToolPayload:
        self._require_state()
        product = self._current_product()
        if not product.get("ml_search_url"):
            return self._payload({"ok": False, "status": "need_keywords", "message": "请先调用 set_keywords"})
        listing = product["listing"]
        browser = self._ensure_browser()
        if not listing["page1_done"]:
            blocked = self._ensure_search(product)
            if blocked:
                return blocked
            self._scroll_results()
            cards = collect_grid_cards(browser.list_page)
            self._add_candidates(product, cards, 1)
            listing["page1_done"] = True
            self._save()
        batch = self._take_candidates(product)
        if batch:
            self._schedule_listing_prefetch(product)
            return self._candidate_payload(product, batch, "listing_batch")
        if listing["page2_done"]:
            message = "前两页已看完" if listing["candidates"] else "前两页没有找到商品主图"
            return self._payload(
                {"ok": True, "status": "listing_complete", "product_id": product["product_id"], "message": message}
            )
        blocked = self._load_listing_page2(product)
        if blocked:
            return blocked
        batch = self._take_candidates(product)
        if not batch:
            message = "前两页已看完" if product["listing"]["candidates"] else "前两页没有找到商品主图"
            return self._payload(
                {"ok": True, "status": "listing_complete", "product_id": product["product_id"], "message": message}
            )
        return self._candidate_payload(product, batch, "listing_batch")

    def _load_listing_page2(self, product: dict[str, Any]) -> ToolPayload | None:
        listing = product["listing"]
        if listing.get("page2_done"):
            return None
        browser = self._ensure_browser()
        browser.ml_cooldown.acquire()
        moved = open_next_results_page(browser.list_page, product["ml_search_url"])
        blocked = browser.ensure_clear(browser.list_page, "ml")
        if blocked:
            return self._blocked(blocked)
        if moved:
            self._scroll_results()
            cards = collect_grid_cards(browser.list_page)
            self._add_candidates(product, cards, 2)
        listing["page2_done"] = True
        self._save()
        return None

    def _schedule_listing_prefetch(self, product: dict[str, Any]) -> None:
        listing = product["listing"]
        if listing.get("page2_done") or listing.get("page2_prefetching") or not product.get("ml_search_url"):
            return
        listing["page2_prefetching"] = True
        self._worker.submit(self._prefetch_listing_page2, product["product_id"])

    def _prefetch_listing_page2(self, product_id: str) -> None:
        listing: dict[str, Any] | None = None
        try:
            product = (self.state or {}).get("products", {}).get(product_id)
            if not product:
                return
            listing = product.get("listing")
            if not isinstance(listing, dict) or listing.get("page2_done") or not product.get("ml_search_url"):
                return
            step("预取", f"{product_id} 开始抓美客多列表第二页")
            blocked = self._load_listing_page2(product)
            if blocked:
                step("预取", f"{product_id} 列表第二页需要手动验证")
            else:
                step("预取", f"{product_id} 列表第二页已写入，当前候选 {len(listing.get('candidates') or [])} 条")
        except Exception as exc:
            step("预取", f"{product_id} 列表第二页失败：{exc}")
            log(traceback.format_exc())
        finally:
            if listing is not None:
                listing["page2_prefetching"] = False
                try:
                    self._save()
                except Exception:
                    log("保存列表预取状态失败")

    def _clear_prefetch_flags(self) -> None:
        state = self.state
        if not state:
            return
        for product in state.get("products", {}).values():
            listing = product.get("listing")
            if isinstance(listing, dict):
                listing.pop("page2_prefetching", None)
            product.pop("ad_prefetching", None)

    def _submit_listing_matches(self, matches: list[Any], model_name: str) -> ToolPayload:
        product = self._current_product()
        payload = self._open_judged_matches(product, matches, model_name, source="search", parent_url=None)
        return self._attach_followup(payload)

    def _open_ml_detail_with_retry(self, url: str) -> Any:
        browser = self._ensure_browser()
        last_error: Exception | None = None
        for attempt in range(2):
            browser.ml_cooldown.acquire()
            try:
                return browser.open_detail(url, timeout=60000)
            except Exception as exc:
                last_error = exc
                log(f"美客多详情打开失败（第 {attempt + 1} 次）：{exc}")
                browser.close_detail()
        log(f"美客多详情重试后仍失败：{last_error}")
        return None

    def _next_ad_batch(self, product_id: str) -> ToolPayload:
        product = self._product(product_id)
        self._require_state()["current_product_id"] = product_id
        for match in product["matches"]:
            if match.get("source") != "search" or match.get("ads_complete"):
                continue
            if not match.get("ads_scraped"):
                blocked = self._scrape_ad_match(product, match)
                if blocked:
                    return blocked
                if match.get("ads_complete"):
                    continue
            start = int(match.get("ad_next_index") or 0)
            batch, start, dropped = self._select_image_batch(product, match["ad_candidates"], start)
            match["ad_next_index"] = start
            if match["ad_next_index"] >= len(match["ad_candidates"]):
                match["ads_complete"] = True
            if not batch:
                self._save()
                continue
            self._ensure_candidate_files(batch, page_url=match["url"])
            self._save()
            images = [(f"参照图 美客多详情主图 parent_url={match['url']}", match.get("image_path") or "")]
            payload_items = []
            for candidate in batch:
                payload_items.append(self._candidate_summary(candidate))
                images.append((f"广告图 candidate_id={candidate['candidate_id']} url={candidate['url']}", candidate.get("image_path") or ""))
            self._schedule_ad_prefetch(product)
            return self._payload(
                {
                    "ok": True,
                    "status": "ad_batch",
                    "product_id": product["product_id"],
                    "parent_url": match["url"],
                    "amazon_title": product.get("title") or "",
                    "candidates": payload_items,
                    "title_dropped": dropped,
                    "message": self._compare_message("ad", dropped),
                },
                images,
            )
        message = self._step_message("这个亚马逊商品的广告位已经看完。", next_required_step(self._require_state()))
        return self._payload(
            {
                "ok": True,
                "status": "ads_complete",
                "product_id": product["product_id"],
                "message": message,
            }
        )

    def _scrape_ad_match(self, product: dict[str, Any], match: dict[str, Any]) -> ToolPayload | None:
        if match.get("ads_scraped"):
            return None
        browser = self._ensure_browser()
        page = self._open_ml_detail_with_retry(public_url(match["url"]))
        if page is None:
            match["ad_candidates"] = []
            match["ad_next_index"] = 0
            match["ads_scraped"] = True
            match["ads_complete"] = True
            self._save()
            return None
        blocked = browser.ensure_clear(page, "ml")
        if blocked:
            return self._blocked(blocked)
        page = browser.live_page(page)
        browser.pause_ml_read()
        cards = collect_ad_cards(page, browser.pause_ml_scroll)
        log(f"广告位主图 {len(cards)} 张：{public_url(match['url'])}")
        match["ad_candidates"] = self._ad_candidates(product, match, cards)
        match["ad_next_index"] = 0
        match["ads_scraped"] = True
        browser.close_detail()
        self._save()
        return None

    def _next_unscraped_ad_match(self, product: dict[str, Any]) -> dict[str, Any] | None:
        for match in product.get("matches") or []:
            if match.get("source") != "search" or match.get("ads_complete") or match.get("ads_scraped"):
                continue
            return match
        return None

    def _schedule_ad_prefetch(self, product: dict[str, Any]) -> None:
        if product.get("ad_prefetching") or self._next_unscraped_ad_match(product) is None:
            return
        product["ad_prefetching"] = True
        self._worker.submit(self._prefetch_next_ad, product["product_id"])

    def _prefetch_next_ad(self, product_id: str) -> None:
        product: dict[str, Any] | None = None
        try:
            product = (self.state or {}).get("products", {}).get(product_id)
            if not product:
                return
            match = self._next_unscraped_ad_match(product)
            if match is None:
                return
            step("预取", f"{product_id} 开始抓下一条广告位")
            blocked = self._scrape_ad_match(product, match)
            if blocked:
                step("预取", f"{product_id} 广告位需要手动验证")
            else:
                step("预取", f"{product_id} 下一条广告位已写入 {len(match.get('ad_candidates') or [])} 张")
        except Exception as exc:
            step("预取", f"{product_id} 广告位预取失败：{exc}")
            log(traceback.format_exc())
        finally:
            if product is not None:
                product["ad_prefetching"] = False
                try:
                    self._save()
                except Exception:
                    log("保存广告预取状态失败")

    def _submit_ad_matches(self, matches: list[Any], model_name: str) -> ToolPayload:
        product = self._current_product()
        if not matches:
            return self._payload({"ok": True, "status": "no_matches", "message": "没有提交广告判断"})
        payload = self._open_judged_matches(product, matches, model_name, source="recommendation", parent_url="")
        return self._attach_followup(payload)

    def _open_judged_matches(
        self,
        product: dict[str, Any],
        matches: list[Any],
        model_name: str,
        source: str,
        parent_url: str | None,
    ) -> ToolPayload:
        kept, dropped = filter_matches(matches, self.settings.similarity_threshold)
        known = self._candidate_index(product)
        recorded = []
        skipped = []
        unknown = []
        for item in kept + dropped:
            candidate = known.get(item["candidate_id"])
            if candidate is None:
                unknown.append(item["candidate_id"])
                continue
            candidate["similarity"] = item["confidence"]
            candidate["similar"] = item["similar"]
            candidate["reason"] = item["reason"]
            candidate["model_name"] = model_name
            self._remember_judged(product, item["candidate_id"])
        quiet_skips = 0
        for item in kept:
            candidate = known.get(item["candidate_id"])
            if candidate is None:
                continue
            if candidate["key"] in product["checked_keys"]:
                skipped.append(candidate["candidate_id"])
                continue
            action = sales_action(source, candidate["sales_text"] if "sales_text" in candidate else None)
            if action == "skip":
                product["checked_keys"].append(candidate["key"])
                skipped.append(candidate["candidate_id"])
                quiet_skips += 1
                continue
            if action == "record":
                self._remember_sale(
                    product,
                    candidate,
                    item,
                    source,
                    parent_url,
                    model_name,
                    candidate.get("sales_text") or "",
                    price=candidate.get("price"),
                )
                recorded.append(product["matches"][-1])
                step("销量", f"卡片上已有销量，不打开详情 {candidate.get('sales_text') or ''} {public_url(candidate['url'])}")
                continue
            browser = self._ensure_browser()
            browser.ml_cooldown.acquire()
            page = browser.open_detail(candidate["url"])
            blocked = browser.ensure_clear(page, "ml")
            if blocked:
                self._save()
                return self._blocked(blocked)
            page = browser.live_page(page)
            browser.pause_ml_read()
            sales = sales_from_text(read_sales_text(page))
            product["checked_keys"].append(candidate["key"])
            if not sales["has_sales"]:
                step("销量", f"没有 vendido，不记录 {public_url(candidate['url'])}")
                browser.close_detail()
                self._save()
                skipped.append(candidate["candidate_id"])
                continue
            image_path = self._download(hero_image_url(page), self._image_stem(candidate["key"]), hero_locator(page), page.url)
            self._remember_sale(
                product,
                candidate,
                item,
                source,
                parent_url,
                model_name,
                sales["sales_text"],
                image_path,
                read_price(page) or candidate.get("price"),
            )
            recorded.append(product["matches"][-1])
            step("销量", f"有销量，已记录 {sales['sales_text']} {public_url(candidate['url'])}")
            browser.close_detail()
            self._save()
        if quiet_skips:
            step("销量", f"卡片上没有 vendido，跳过 {quiet_skips} 条，没有打开详情")
        self._save()
        return self._payload(
            {
                "ok": True,
                "status": "matches_recorded" if recorded else "no_sales_or_not_similar",
                "product_id": product["product_id"],
                "source": source,
                "recorded": [self._match_summary(item) for item in recorded],
                "top_links": _top_links(product),
                "skipped": skipped,
                "unknown_ids": unknown,
                "message": "这一批销量已处理。",
            },
        )

    def _remember_sale(
        self,
        product: dict[str, Any],
        candidate: dict[str, Any],
        item: dict[str, Any],
        source: str,
        parent_url: str | None,
        model_name: str,
        sales_text: str,
        image_path: str | None = None,
        price: float | None = None,
    ) -> None:
        if not image_path:
            self._ensure_candidate_files([candidate], page_url=candidate.get("url") or "")
            image_path = candidate.get("image_path") or ""
        if candidate["key"] not in product["checked_keys"]:
            product["checked_keys"].append(candidate["key"])
        parent = candidate.get("parent_url") if source == "recommendation" else parent_url
        product["matches"].append(
            {
                "url": candidate["url"],
                "key": candidate["key"],
                "title": candidate.get("title") or "",
                "sales_text": sales_text,
                "price": price if price is not None else candidate.get("price"),
                "confidence": item["confidence"],
                "reason": item["reason"],
                "source": source,
                "parent_url": parent,
                "image_path": image_path,
                "model_name": model_name,
                "candidate_id": candidate["candidate_id"],
                "ad_candidates": [],
                "ad_next_index": 0,
                "ads_scraped": source != "search",
                "ads_complete": source != "search",
            }
        )

    def _attach_followup(self, payload: ToolPayload) -> ToolPayload:
        """把下一批要看的图放进同一次返回，避免再单独走一步 MCP。"""

        data = payload.data
        step_name = data.get("next_step")
        product_id = data.get("product_id")
        if step_name == "next_listing_batch":
            follow = self._next_listing_batch()
            if follow.data.get("status") == "listing_batch":
                follow.data["message"] = (
                    "上一批销量已处理。下面是下一批列表图。"
                    + self._compare_message("listing", follow.data.get("title_dropped") or [])
                )
                return follow
            payload = follow
            data = payload.data
            step_name = data.get("next_step")
        if step_name == "next_ad_batch" and product_id:
            follow = self._next_ad_batch(product_id)
            if follow.data.get("status") == "ad_batch":
                follow.data["message"] = (
                    "下面是下一条有销量详情的广告图。"
                    + self._compare_message("ad", follow.data.get("title_dropped") or [])
                )
                return follow
            return follow
        data["message"] = self._step_message(str(data.get("message") or ""), str(data.get("next_step") or ""))
        return payload

    def _step_message(self, done: str, step_name: str) -> str:
        hints = {
            "submit_listing_matches": "请提交剩下的列表判断。",
            "submit_ad_matches": "请提交剩下的广告判断。",
            "export_results": "这一条商品已筛完，请 export_results。",
            "next_amazon_product": "请继续 next_amazon_product。",
            "set_keywords": "请调用 set_keywords。",
        }
        hint = hints.get(step_name, "")
        return f"{done}{hint}" if hint else done

    def _ensure_amazon_list(self) -> ToolPayload | None:
        state = self._require_state()
        browser = self._ensure_browser()
        browser.close_detail()
        if is_seller_list(browser.list_page.url, state["seller_id"]):
            blocked = browser.ensure_clear(browser.list_page, "amazon")
            if blocked:
                return self._blocked(blocked)
            return None
        browser.amazon_cooldown.acquire()
        browser.goto(browser.list_page, state.get("list_url") or state["store_url"])
        blocked = browser.ensure_clear(browser.list_page, "amazon")
        if blocked:
            return self._blocked(blocked)
        browser.pause_amazon_read()
        state["list_url"] = browser.list_page.url
        self._save()
        return None

    def _ensure_search(self, product: dict[str, Any]) -> ToolPayload | None:
        browser = self._ensure_browser()
        browser.close_detail()
        current = browser.list_page.url or ""
        slug = product["ml_search_url"].split("?", 1)[0].rstrip("/")
        if current.startswith(slug):
            blocked = browser.ensure_clear(browser.list_page, "ml")
            if blocked:
                return self._blocked(blocked)
            return None
        browser.ml_cooldown.acquire()
        browser.goto(browser.list_page, product["ml_search_url"])
        blocked = browser.ensure_clear(browser.list_page, "ml")
        if blocked:
            return self._blocked(blocked)
        browser.pause_ml_scroll()
        return None

    def _turn_amazon_page(self) -> bool:
        state = self._require_state()
        browser = self._ensure_browser()
        if self.settings.max_pages and state["amazon_page"] >= self.settings.max_pages:
            return False
        if not has_next_page(browser.list_page):
            return False
        browser.amazon_cooldown.acquire()
        browser.list_page.locator("a.s-pagination-next").first.click()
        try:
            browser.list_page.wait_for_load_state("domcontentloaded", timeout=20000)
        except Exception:
            log("亚马逊翻页等待超时，继续读取当前页")
        blocked = browser.ensure_clear(browser.list_page, "amazon")
        if blocked:
            raise _Blocked(blocked)
        browser.pause_amazon_read()
        state["amazon_page"] += 1
        state["list_url"] = browser.list_page.url
        self._save()
        return True

    def _next_unvisited_entry(self) -> dict[str, Any] | None:
        state = self._require_state()
        visited = set(state["visited"])
        for entry in list_title_entries(self._ensure_browser().list_page):
            if entry["key"] not in visited:
                return entry
        return None

    def _mark_visited(self, key: str) -> None:
        if key and key not in self._require_state()["visited"]:
            self._require_state()["visited"].append(key)

    def _scroll_results(self) -> None:
        browser = self._ensure_browser()
        browser.scroll_down(browser.list_page, 5, browser.pause_ml_scroll)

    def _add_candidates(self, product: dict[str, Any], cards: list[dict[str, str]], page_no: int) -> None:
        listing = product["listing"]
        existing = {item["key"] for item in listing["candidates"]}
        checked = set(product["checked_keys"])
        for card in cards:
            if card["key"] in existing or card["key"] in checked:
                continue
            candidate_id = f"{product['product_id']}-p{page_no}-{len(listing['candidates'])}"
            listing["candidates"].append(
                {
                    "candidate_id": candidate_id,
                    "url": card["url"],
                    "image_url": card["image_url"],
                    "image_path": "",
                    "title": card["title"],
                    "sales_text": card.get("sales_text") or "",
                    "price": card.get("price"),
                    "key": card["key"],
                    "page": page_no,
                    "parent_url": "",
                }
            )
            existing.add(card["key"])

    def _take_candidates(self, product: dict[str, Any]) -> list[dict[str, Any]]:
        listing = product["listing"]
        batch, next_index, dropped = self._select_image_batch(
            product, listing["candidates"], int(listing["next_index"])
        )
        listing["next_index"] = next_index
        listing["title_dropped"] = dropped
        if batch:
            self._ensure_candidate_files(batch, page_url=product["ml_search_url"])
        self._save()
        return batch

    def _select_image_batch(
        self,
        product: dict[str, Any],
        candidates: list[dict[str, Any]],
        start: int,
    ) -> tuple[list[dict[str, Any]], int, list[dict[str, str]]]:
        """跳过标题明显不是同一商品的候选，只把剩下的送去看图。"""

        batch: list[dict[str, Any]] = []
        dropped: list[dict[str, str]] = []
        index = start
        amazon_title = str(product.get("title") or "")
        extra = str(product.get("keywords") or "")
        while index < len(candidates) and len(batch) < BATCH_SIZE:
            candidate = candidates[index]
            index += 1
            if title_obviously_different(amazon_title, str(candidate.get("title") or ""), extra):
                self._reject_title_mismatch(product, candidate)
                dropped.append(
                    {
                        "candidate_id": candidate["candidate_id"],
                        "title": str(candidate.get("title") or ""),
                        "reason": TITLE_MISMATCH_REASON,
                    }
                )
                continue
            batch.append(candidate)
        return batch, index, dropped

    def _reject_title_mismatch(self, product: dict[str, Any], candidate: dict[str, Any]) -> None:
        candidate["similar"] = False
        candidate["similarity"] = 1
        candidate["reason"] = TITLE_MISMATCH_REASON
        candidate["model_name"] = TITLE_FILTER_MODEL
        self._remember_judged(product, candidate["candidate_id"])
        step("标题", f"{TITLE_MISMATCH_REASON} {candidate.get('title') or ''} {public_url(candidate.get('url') or '')}")

    def _compare_message(self, kind: str, dropped: list[Any]) -> str:
        submit = "submit_listing_matches" if kind == "listing" else "submit_ad_matches"
        skipped = f"已按标题跳过 {len(dropped)} 条明显不是同一商品的，这些没有进入看图。" if dropped else ""
        return (
            f"{skipped}请先对照亚马逊标题再看剩下的候选标题。"
            "标题已经能明显看出不是同一商品的，直接记 similar=false，不要进图片对比。"
            "剩下的再看图：颜色不同也可以算同款；外形、结构和整体构图高度相似则为 true，外形明显不同才是 false。"
            f"判断完再调用 {submit}。"
        )

    def _candidate_payload(self, product: dict[str, Any], batch: list[dict[str, Any]], status: str) -> ToolPayload:
        reference = product.get("image_path") or ""
        images = [(f"参照图 亚马逊主图 product_id={product['product_id']}", reference)]
        summaries = []
        for candidate in batch:
            summaries.append(self._candidate_summary(candidate))
            images.append(
                (f"列表图 candidate_id={candidate['candidate_id']} url={candidate['url']}", candidate.get("image_path") or "")
            )
        dropped = list(product.get("listing", {}).get("title_dropped") or [])
        return self._payload(
            {
                "ok": True,
                "status": status,
                "product_id": product["product_id"],
                "amazon_title": product.get("title") or "",
                "candidates": summaries,
                "title_dropped": dropped,
                "message": self._compare_message("listing", dropped),
            },
            images,
        )

    def _ad_candidates(self, product: dict[str, Any], match: dict[str, Any], cards: list[dict[str, str]]) -> list[dict[str, Any]]:
        existing = set(product["checked_keys"])
        for item in product["matches"]:
            existing.add(item["key"])
        candidates = []
        for card in cards:
            if card["key"] in existing:
                continue
            candidate_id = f"{product['product_id']}-ad-{card['key']}-{len(candidates)}"
            candidates.append(
                {
                    "candidate_id": candidate_id,
                    "url": card["url"],
                    "image_url": card["image_url"],
                    "image_path": "",
                    "title": card["title"],
                    "sales_text": card.get("sales_text") or "",
                    "price": card.get("price"),
                    "key": card["key"],
                    "parent_url": match["url"],
                }
            )
            existing.add(card["key"])
        return candidates

    def _ensure_candidate_files(self, candidates: list[dict[str, Any]], page_url: str) -> None:
        browser = self._ensure_browser()
        for candidate in candidates:
            if candidate.get("image_path"):
                continue
            candidate["image_path"] = self._download(
                candidate.get("image_url") or "",
                self._image_stem(candidate["candidate_id"]),
                None,
                page_url or browser.list_page.url,
            )

    def _download(self, url: str, stem: Path, locator: Any, referer: str) -> str:
        browser = self._ensure_browser()
        if url:
            try:
                data = browser.download_bytes(url, referer)
                return str(save_original(data, stem))
            except Exception as exc:
                log(f"下载图片失败，改用页面截图：{exc}")
        if locator is not None:
            try:
                if locator.count() > 0:
                    path = stem.with_suffix(".png")
                    path.parent.mkdir(parents=True, exist_ok=True)
                    locator.first.screenshot(path=str(path))
                    return str(path)
            except Exception as exc:
                log(f"截图失败：{exc}")
        return ""

    def _image_stem(self, name: str) -> Path:
        state = self._require_state()
        safe = "".join(char if char.isalnum() or char in "-_" else "_" for char in name)
        return seller_dir(self.output_dir, _storage_id(state)) / "images" / safe

    def _product(self, product_id: str) -> dict[str, Any]:
        state = self._require_state()
        product = state["products"].get(product_id)
        if product is None:
            raise UrlError(f"没有这个商品：{product_id}")
        return product

    def _current_product(self) -> dict[str, Any]:
        state = self._require_state()
        current = state.get("current_product_id")
        if not current:
            raise UrlError("还没有当前商品。请先调用 next_amazon_product")
        return self._product(current)

    def _candidate_index(self, product: dict[str, Any]) -> dict[str, dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        for candidate in product["listing"]["candidates"]:
            found[candidate["candidate_id"]] = candidate
        for match in product["matches"]:
            for candidate in match.get("ad_candidates") or []:
                found[candidate["candidate_id"]] = candidate
        return found

    def _remember_judged(self, product: dict[str, Any], candidate_id: str) -> None:
        if candidate_id not in product["judged_ids"]:
            product["judged_ids"].append(candidate_id)

    def _candidate_summary(self, candidate: dict[str, Any]) -> dict[str, Any]:
        return {
            "candidate_id": candidate["candidate_id"],
            "url": candidate["url"],
            "title": candidate.get("title") or "",
            "parent_url": candidate.get("parent_url") or "",
        }

    def _match_summary(self, match: dict[str, Any]) -> dict[str, Any]:
        return {
            "candidate_id": match.get("candidate_id"),
            "url": match.get("url"),
            "title": match.get("title") or "",
            "sales_text": match.get("sales_text") or "",
            "confidence": match.get("confidence"),
            "source": match.get("source"),
            "parent_url": match.get("parent_url"),
            "model_name": match.get("model_name") or "",
        }


class _Blocked(Exception):
    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(NEEDS_HUMAN)
        self.payload = payload


def _top_links(product: dict[str, Any]) -> list[dict[str, Any]]:
    return similarity_rows(product)[:10]


def _jpeg_file(path: str) -> bytes | None:
    if not path:
        return None
    file = Path(path)
    if not file.is_file():
        return None
    try:
        return to_client_jpeg(file.read_bytes())
    except ValueError:
        return None
