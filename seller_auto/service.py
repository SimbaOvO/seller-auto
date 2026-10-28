"""抓取会话。浏览器操作都在同一条线程上执行。"""

from __future__ import annotations

import copy
import queue
import threading
import traceback
import urllib.request
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
from seller_auto.browser import NEEDS_HUMAN, ArgentinaBrowser, browser_closed_error, log
from seller_auto.browser_launch import playwright_install_command
from seller_auto.categories import category_reason, classify_breadcrumb
from seller_auto.images import save_original, to_client_jpeg
from seller_auto.lingdong import LingdongLoginSkipped, apply_last_mile, collect_market_quotes, quote_links
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
from seller_auto.sales import has_sales
from seller_auto.workbook import can_enter_top, template_path, write_product_workbook
from seller_auto.pacing import DelaySettings
from seller_auto.sellersprite import open_1688_from_row, open_competitor
from seller_auto.sourcing import read_page_source
from seller_auto.progress import next_required_step, products_ready_to_export
from seller_auto.storage import (
    atomic_write_json,
    build_export,
    discard_finished_images,
    discard_product_images,
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


def _export_view(state: dict[str, Any], export_ids: list[str]) -> dict[str, Any]:
    """导出用的会话副本。正在打开的下一条不放进去。"""

    view = copy.deepcopy(state)
    products = view.get("products") or {}
    keep = {product_id for product_id in export_ids if product_id in products}
    for product_id, product in products.items():
        if product.get("workbook_written"):
            keep.add(product_id)
    order = [product_id for product_id in view.get("product_order") or [] if product_id in keep]
    view["product_order"] = order
    view["products"] = {product_id: products[product_id] for product_id in order}
    return view


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
        self._state_lock = threading.RLock()
        self._published: dict[str, Any] | None = None

    def open_store(self, seller_id: str, product: str = "") -> ToolPayload:
        return self._guard(self._open_store, seller_id, product)

    def next_amazon_product(self) -> ToolPayload:
        return self._guard(self._next_amazon_product)

    def set_keywords(self, product_id: str, keywords: str, chinese_name: str = "") -> ToolPayload:
        return self._guard(self._set_keywords, product_id, keywords, chinese_name)

    def next_listing_batch(self) -> ToolPayload:
        ready = self._fast_listing_replay()
        if ready is not None:
            return ready
        return self._guard(self._next_listing_batch)

    def submit_listing_matches(self, matches: list[Any], model_name: str = "") -> ToolPayload:
        return self._accept_matches(matches, model_name, "search")

    def next_ad_batch(self, product_id: str) -> ToolPayload:
        return self._guard(self._next_ad_batch, product_id)

    def submit_ad_matches(self, matches: list[Any], model_name: str = "") -> ToolPayload:
        return self._accept_matches(matches, model_name, "recommendation")

    def session_status(self) -> ToolPayload:
        return self._read_status()

    def resume_session(self, session_id: str) -> ToolPayload:
        return self._guard(self._resume_session, session_id)

    def export_results(self) -> ToolPayload:
        try:
            return self._start_export()
        except UrlError as exc:
            return ToolPayload({"ok": False, "status": "error", "message": str(exc)})

    def _guard(self, function: Callable[..., ToolPayload], *args: Any) -> ToolPayload:
        try:
            return self._worker.call(self._invoke, function, *args)
        except Exception as exc:
            log(traceback.format_exc())
            message = str(exc)
            if "Executable doesn't exist" in message or "browserType.launch" in message:
                message = f"{message}。请先在项目目录执行 {playwright_install_command()}"
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
            self._commit_locked()

    def _commit_locked(self) -> None:
        state = self._require_state()
        if self.browser is not None:
            state["challenge_pending"] = self.browser.challenge_pending
            state["challenge_site"] = self.browser.challenge_site
        state["updated_at"] = _now()
        self._published = copy.deepcopy(state)
        self._write_state()

    def _write_state(self) -> None:
        state = self._require_state()
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
        return self._status_from(self._require_state())

    def _status_from(self, state: dict[str, Any]) -> dict[str, Any]:
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

    def _payload(
        self,
        data: dict[str, Any],
        images: list[tuple[str, str]] | None = None,
        state: dict[str, Any] | None = None,
    ) -> ToolPayload:
        current = self.state if state is None else state
        if current is not None:
            data = {**self._status_from(current), **data}
        notice = self._take_pending_notice()
        if notice and notice not in str(data.get("message") or ""):
            data["message"] = f"{data.get('message') or ''} {notice}".strip()
        encoded: list[tuple[str, bytes]] = []
        for caption, path in images or []:
            jpeg = _jpeg_file(path)
            if jpeg is not None:
                encoded.append((caption, jpeg))
        status = data.get("status") or ""
        if status:
            step(str(status), str(data.get("message") or ""))
        return ToolPayload(data, encoded)

    def _set_pending_notice(self, text: str) -> None:
        browser = self.enrich_browser
        if browser is None or not text:
            return
        browser.pending_notice = text

    def _take_pending_notice(self) -> str:
        browser = self.enrich_browser
        if browser is None:
            return ""
        notice = str(getattr(browser, "pending_notice", "") or "").strip()
        if notice:
            browser.pending_notice = ""
        return notice

    def _blocked(self, blocked: dict[str, Any]) -> ToolPayload:
        self._save()
        return self._payload(blocked)

    def _open_store(self, seller_id: str, product: str = "") -> ToolPayload:
        seller_id, store_url = resolve_store_input(seller_id)
        focus = parse_focus_product(product)
        browser = self._ensure_browser()
        session_id = str(uuid.uuid4())
        state = _new_state(session_id, seller_id, store_url)
        state["single_product"] = focus
        state["run_id"] = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + session_id[:8]
        with self._state_lock:
            self.state = state
            self._published = copy.deepcopy(state)
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
        loaded = load_json(path)
        with self._state_lock:
            self.state = loaded
            self._clear_prefetch_flags()
            self._published = copy.deepcopy(self.state)
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

    def _read_status(self) -> ToolPayload:
        try:
            self._kick_sales_if_idle()
            self._kick_export_if_idle()
            with self._state_lock:
                if self.state is None and self._published is None:
                    raise UrlError("还没有会话。请先调用 open_store 或 resume_session")
                snapshot = copy.deepcopy(self._published if self._published is not None else self.state)
            if snapshot is None:
                raise UrlError("还没有会话。请先调用 open_store 或 resume_session")
            product = (snapshot.get("products") or {}).get(snapshot.get("current_product_id") or "") or {}
            if product.get("sales_pending") or snapshot.get("export_pending") or snapshot.get("challenge_pending"):
                return self._payload(
                    {"ok": True, "status": "status", "message": self._status_message(snapshot)},
                    state=snapshot,
                )
            step_name = next_required_step(snapshot)
            if step_name in {"next_listing_batch", "submit_listing_matches", "next_ad_batch", "submit_ad_matches", "export_results"}:
                recorded = self._payload(
                    {
                        "ok": True,
                        "status": "matches_recorded",
                        "product_id": snapshot.get("current_product_id") or "",
                        "message": "进度已更新。",
                    },
                    state=snapshot,
                )
                return self._worker.call(self._attach_followup, recorded)
            return self._payload(
                {"ok": True, "status": "status", "message": self._status_message(snapshot)},
                state=snapshot,
            )
        except UrlError as exc:
            return ToolPayload({"ok": False, "status": "error", "message": str(exc)})

    def _status_message(self, state: dict[str, Any]) -> str:
        if state.get("export_pending"):
            return "正在查询蓝鲸并写入表格。请继续 session_status，不要重复 export_results。"
        products = state.get("products") or {}
        product = products.get(state.get("current_product_id") or "") or {}
        if product.get("sales_pending"):
            if state.get("challenge_pending"):
                return "需要手动验证。在前台完成后再次调用 session_status，不要重复提交。"
            detail = str(product.get("sales_error") or "")
            if detail:
                return f"打开详情没有完成：{detail}。请再次 session_status，不要重复提交。"
            return "同款详情还在打开。请继续 session_status，不要重复提交，也不要取下一批。"
        step_name = next_required_step(state)
        exported_message = self._visible_export_message(state, step_name)
        if exported_message:
            return exported_message
        if step_name == "submit_listing_matches":
            return "这批列表图还没有提交判断。图还在就直接提交；如果上次返回丢了，再调用 next_listing_batch，会把同一批发回来。"
        if step_name == "submit_ad_matches":
            return "这批广告图还没有提交判断。图还在就直接提交；如果上次返回丢了，再调用 next_ad_batch，会把同一批发回来。"
        return self._step_message("进度已更新。", step_name)

    def _visible_export_message(self, state: dict[str, Any], step_name: str) -> str:
        exported = state.get("export_result") or {}
        message = str(exported.get("message") or "")
        if not message:
            return ""
        if state.get("amazon_done") and not products_ready_to_export(state):
            if "遍历完" in message:
                return message
            return "店铺列表已遍历完。不要再调用 next_amazon_product。"
        if step_name == "export_results":
            return message
        if step_name == "next_amazon_product" and exported.get("product_id") == state.get("current_product_id"):
            return message
        return ""

    def _start_export(self) -> ToolPayload:
        with self._state_lock:
            state = self._require_state()
            product = (state.get("products") or {}).get(state.get("current_product_id") or "")
            if product and product.get("sales_pending"):
                snapshot = copy.deepcopy(state)
                waiting = True
            else:
                waiting = False
                if state.get("export_pending"):
                    snapshot = copy.deepcopy(self._published or state)
                    return self._payload(
                        {
                            "ok": True,
                            "status": "exporting",
                            "message": "正在查询蓝鲸并写入表格。请继续 session_status，不要重复 export_results。",
                        },
                        state=snapshot,
                    )
                ready = products_ready_to_export(state)
                if not ready:
                    return self._idle_export_payload(state)
                state["export_ids"] = ready
                state["export_pending"] = True
                state["export_running"] = True
                state["export_result"] = {}
                self._commit_locked()
                snapshot = copy.deepcopy(self._published or state)
        if waiting:
            return self._payload(
                {
                    "ok": True,
                    "status": "status",
                    "message": "同款详情还在打开。请先 session_status，完成后再 export_results。",
                },
                state=snapshot,
            )
        self._enrich_worker.submit(self._finish_export)
        return self._payload(
            {
                "ok": True,
                "status": "exporting",
                "message": "这一条 ASIN 已经走完。正在查询蓝鲸并写入表格。请继续 session_status，不要重复 export_results。",
            },
            state=snapshot,
        )

    def _idle_export_payload(self, state: dict[str, Any]) -> ToolPayload:
        saved = state.get("export_result") or {}
        if state.get("amazon_done"):
            message = "店铺列表已遍历完。不要再调用 next_amazon_product。"
            if saved.get("message") and "遍历完" in str(saved.get("message")):
                message = str(saved["message"])
        elif saved.get("message") and saved.get("product_id") == state.get("current_product_id"):
            message = str(saved["message"])
        elif state.get("single_product"):
            message = "这一条商品已经处理完。不要再调用 next_amazon_product。"
        else:
            message = "这一条商品已经写入表格。请继续 next_amazon_product，直到返回 end_of_store。"
        return self._payload(
            {
                "ok": True,
                "status": "exported",
                "path": saved.get("path") or "",
                "workbook": saved.get("workbook") or "",
                "message": message,
            },
            state=copy.deepcopy(state),
        )

    def _kick_export_if_idle(self) -> None:
        with self._state_lock:
            state = self.state
            if not state or not state.get("export_pending") or state.get("export_running"):
                return
            state["export_running"] = True
            self._commit_locked()
        self._enrich_worker.submit(self._finish_export)

    def _same_session(self, session_id: str) -> bool:
        with self._state_lock:
            return self.state is not None and str(self.state.get("session_id") or "") == session_id

    def _finish_export(self) -> None:
        session_id = ""
        export_ids: list[str] = []
        baseline: dict[str, Any] | None = None
        try:
            with self._state_lock:
                state = self.state
                if state is None:
                    return
                session_id = str(state.get("session_id") or "")
                export_ids = list(state.get("export_ids") or products_ready_to_export(state))
                baseline = _export_view(state, export_ids)
            self._wait_enrichment(export_ids)
            if not self._same_session(session_id):
                self._publish_detached(baseline, export_ids)
                return
            market_note = self._market_quotes_inline(export_ids)
            if not self._same_session(session_id):
                self._publish_detached(baseline, export_ids)
                return
            detached = False
            view: dict[str, Any] | None = None
            freight_note = ""
            with self._state_lock:
                if self.state is None or str(self.state.get("session_id") or "") != session_id:
                    detached = True
                else:
                    freight_note = self._record_last_mile(export_ids)
                    view = _export_view(self.state, export_ids)
            if detached or view is None:
                self._publish_detached(baseline, export_ids)
                return
            message, path, workbook = self._write_export_files(market_note, export_ids, view, freight_note)
            with self._state_lock:
                if self.state is None or str(self.state.get("session_id") or "") != session_id:
                    step("导出", "会话已更换，原商品的表格已写入")
                    return
                for product_id in export_ids:
                    product = self.state["products"].get(product_id)
                    if product is not None:
                        product["workbook_written"] = True
                current = self.state.get("current_product_id")
                product_id = current if current in export_ids else (export_ids[-1] if export_ids else current)
                self.state["export_pending"] = False
                self.state["export_running"] = False
                self.state["export_ids"] = []
                self.state["export_result"] = {
                    "ok": True,
                    "status": "exported",
                    "path": path,
                    "workbook": workbook,
                    "product_id": product_id or "",
                    "message": message,
                }
                self._commit_locked()
            self._discard_finished_images()
        except Exception as exc:
            log(traceback.format_exc())
            with self._state_lock:
                if self.state is not None and session_id and str(self.state.get("session_id") or "") == session_id:
                    self.state["export_pending"] = False
                    self.state["export_running"] = False
                    self.state["export_ids"] = []
                    self.state["export_result"] = {"ok": False, "status": "error", "message": str(exc)}
                    self._commit_locked()
            step("导出", f"没有写完：{exc}")

    def _publish_detached(self, view: dict[str, Any] | None, export_ids: list[str]) -> None:
        if not view or not export_ids:
            step("导出", "会话已更换，没有可写入的商品")
            return
        note = ""
        for product_id in export_ids:
            product = (view.get("products") or {}).get(product_id)
            if product and apply_last_mile(product) is not None:
                note = "尾程运费已按包装和最低售价写入 FBA 运费。"
        try:
            self._write_export_files("", export_ids, view, note)
        except Exception as exc:
            step("导出", f"原会话写入失败：{exc}")
            return
        step("导出", "会话已更换，原商品已写入表格")

    def _market_quotes_inline(self, export_ids: list[str]) -> str:
        with self._state_lock:
            state = self.state
            if state is None:
                return ""
            jobs = []
            for product_id in export_ids:
                product = (state.get("products") or {}).get(product_id) or {}
                links = quote_links(product)
                if not links:
                    continue
                jobs.append(
                    {
                        "product_id": product_id,
                        "links": list(links),
                        "package": copy.deepcopy(product.get("package_size")),
                    }
                )
        if not jobs:
            return ""
        try:
            return self._run_market_quotes(jobs)
        except LingdongLoginSkipped as exc:
            text = str(exc)
            step("蓝鲸", text)
            self._set_pending_notice(text)
            return text
        except Exception as exc:
            step("蓝鲸", f"查询失败：{exc}")
            log(traceback.format_exc())
            return "蓝鲸选品没有查成，单量这次没写。"

    def _write_export_files(
        self,
        market_note: str,
        export_ids: list[str],
        view: dict[str, Any],
        freight_note: str,
    ) -> tuple[str, str, str]:
        payload = build_export(view)
        directory = seller_dir(self.output_dir, _storage_id(view))
        path = directory / export_filename()
        atomic_write_json(path, payload)
        workbook_path = self.workbook_path
        products = [view["products"][product_id] for product_id in export_ids if product_id in view["products"]]
        written = write_product_workbook(workbook_path, products)
        removed = self._discard_product_list(products)
        if written:
            if view.get("single_product"):
                message = f"这一条商品的结果已追加到 {workbook_path}。不要再调用 next_amazon_product。"
            elif view.get("amazon_done"):
                message = f"这一条商品的结果已追加到 {workbook_path}。店铺列表已遍历完。"
            else:
                message = f"这一条商品的结果已追加到 {workbook_path}。请继续 next_amazon_product，直到返回 end_of_store。"
        elif view.get("single_product"):
            message = "这一条没有同款，没有写入表格。不要再调用 next_amazon_product。"
        elif view.get("amazon_done"):
            message = "店铺列表已遍历完，没有同款，没有写入表格。"
        else:
            message = "这一条没有同款，没有写入表格。请继续 next_amazon_product，直到返回 end_of_store。"
        if removed:
            message = f"{message} 已删除过程图片 {removed} 个。"
        if market_note:
            message = f"{message} {market_note}"
        if freight_note:
            message = f"{message} {freight_note}"
        return message, str(path), str(workbook_path)

    def _record_last_mile(self, export_ids: list[str]) -> str:
        recorded = False
        with self._state_lock:
            state = self.state
            if state is None:
                return ""
            for product_id in export_ids:
                product = state["products"].get(product_id)
                if product and apply_last_mile(product) is not None:
                    recorded = True
            if recorded:
                self._commit_locked()
        if recorded:
            return "尾程运费已按包装和最低售价写入 FBA 运费。"
        return ""

    def _discard_product_list(self, products: list[dict[str, Any]]) -> int:
        removed = 0
        for product in products:
            try:
                removed += discard_product_images(product)
            except Exception as exc:
                log(f"清理过程图片失败：{exc}")
        if removed:
            step("清理", f"已删除过程图片 {removed} 个")
        return removed

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
                message = f"已跳过{reason}类商品。这一条已结束，不要再翻店铺。"
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
                "这一条筛完后服务会自己写表并打开下一条，不要调用 export_results。"
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
        with self._state_lock:
            self._enrich_events[product_id] = event
        step("资料", f"{product_id} 的卖家精灵和货源查询已在后台开始，美客多比对可以同时进行")
        self._enrich_worker.submit(self._run_enrichment, product_id, image_url, event)

    def _wait_enrichment(self, product_ids: list[str] | None = None) -> None:
        with self._state_lock:
            if product_ids is None:
                events = list(self._enrich_events.values())
            else:
                events = [self._enrich_events[product_id] for product_id in product_ids if product_id in self._enrich_events]
        if not events:
            return
        pending = [event for event in events if not event.is_set()]
        if pending:
            step("资料", "导出前等待卖家精灵和货源查询结束")
        for event in events:
            event.wait()

    def _run_market_quotes(self, jobs: list[dict[str, Any]]) -> str:
        try:
            browser = self._ensure_enrich_browser()
            quotes = collect_market_quotes(
                browser,
                [(job["links"], job.get("package")) for job in jobs],
            )
        except Exception as exc:
            if not browser_closed_error(exc):
                raise
            step("蓝鲸", "资料浏览器已断开，重开后再查")
            self._reset_enrich_browser()
            browser = self._ensure_enrich_browser()
            quotes = collect_market_quotes(
                browser,
                [(job["links"], job.get("package")) for job in jobs],
            )
        wrote = False
        with self._state_lock:
            state = self.state
            if state is not None:
                for job, quote in zip(jobs, quotes):
                    product = (state.get("products") or {}).get(job["product_id"])
                    if not product or quote.get("orders") is None:
                        continue
                    product["market_orders"] = quote["orders"]
                    wrote = True
                if wrote:
                    self._commit_locked()
        if wrote:
            return "蓝鲸选品已把 30 天销量写入名称。"
        return "蓝鲸选品没有查到这些链接，单量保持原样。"

    def _ensure_enrich_browser(self) -> ArgentinaBrowser:
        if self.enrich_browser is None:
            enrich_dir = Path(str(self.profile_dir) + "-enrich")
            self.enrich_browser = ArgentinaBrowser(
                self.settings,
                enrich_dir,
                headless=self.headless,
                locale="zh-CN",
                accept_language="zh-CN,zh;q=0.9",
                timezone_id="Asia/Shanghai",
                geolocation={"latitude": 31.2304, "longitude": 121.4737},
            )
        try:
            self.enrich_browser.start()
        except Exception as exc:
            if not browser_closed_error(exc):
                raise
            step("资料", "资料浏览器启动失败，丢掉后重开")
            self._reset_enrich_browser()
            if self.enrich_browser is None:
                enrich_dir = Path(str(self.profile_dir) + "-enrich")
                self.enrich_browser = ArgentinaBrowser(
                    self.settings,
                    enrich_dir,
                    headless=self.headless,
                    locale="zh-CN",
                    accept_language="zh-CN,zh;q=0.9",
                    timezone_id="Asia/Shanghai",
                    geolocation={"latitude": 31.2304, "longitude": 121.4737},
                )
            self.enrich_browser.start()
        assert self.enrich_browser is not None
        return self.enrich_browser

    def _reset_enrich_browser(self) -> None:
        current = self.enrich_browser
        if current is None:
            return
        try:
            current.close()
        except Exception:
            log("关闭资料查询浏览器失败")
        self.enrich_browser = None

    def _enrich_once(self, product_id: str) -> tuple[dict[str, str] | None, dict[str, Any] | None]:
        browser = self._ensure_enrich_browser()
        package, page = self._open_competitor(browser, product_id)
        source = None
        if page is not None:
            source = self._read_1688_from_row(browser, page, product_id)
        return package, source

    def _run_enrichment(self, product_id: str, image_url: str, event: threading.Event) -> None:
        _ = image_url
        package = None
        source = None
        try:
            try:
                package, source = self._enrich_once(product_id)
            except Exception as exc:
                if not browser_closed_error(exc):
                    raise
                step("资料", f"{product_id} 资料浏览器已断开，重开后再查")
                self._reset_enrich_browser()
                package, source = self._enrich_once(product_id)
            with self._state_lock:
                if self.state and product_id in self.state.get("products", {}):
                    self.state["products"][product_id]["package_size"] = package
                    self.state["products"][product_id]["source_price"] = source
                    apply_last_mile(self.state["products"][product_id])
                    self._commit_locked()
            if package and source:
                step("资料", f"{product_id} 包装和货源已写回")
            elif package:
                step("资料", f"{product_id} 包装已写回，没有读到货源均价")
            elif source:
                step("资料", f"{product_id} 货源已写回，没有读到包装")
            else:
                step("资料", f"{product_id} 没有读到包装和货源")
        except Exception as exc:
            step("资料", f"{product_id} 后台查询失败：{exc}")
            log(traceback.format_exc())
        finally:
            try:
                if self.enrich_browser is not None:
                    self.enrich_browser.close_detail()
            except Exception:
                log("关闭资料页失败")
            event.set()

    def _open_competitor(self, browser: ArgentinaBrowser, asin: str) -> tuple[dict[str, str] | None, Any]:
        try:
            return open_competitor(browser, asin)
        except Exception as exc:
            if browser_closed_error(exc):
                raise
            step("卖家精灵", f"没有读到包装尺寸：{exc}")
            log(traceback.format_exc())
            return None, None

    def _read_1688_from_row(self, browser: ArgentinaBrowser, page: Any, asin: str) -> dict[str, Any] | None:
        try:
            opened = open_1688_from_row(browser, page, asin)
            if opened is None:
                return None
            return read_page_source(browser, opened)
        except Exception as exc:
            if browser_closed_error(exc):
                raise
            step("1688", f"没有读到货源均价：{exc}")
            log(traceback.format_exc())
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
        with self._state_lock:
            pending = list(
                _unjudged_delivered(
                    product["listing"]["candidates"],
                    product["listing"].get("next_index"),
                    product.get("judged_ids"),
                )
            )
        if pending:
            self._ensure_candidate_files(pending, page_url=product["ml_search_url"])
            self._save()
            return self._candidate_payload(product, pending, "listing_batch")
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
        with self._state_lock:
            listing["page2_done"] = True
            self._commit_locked()
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
                try:
                    with self._state_lock:
                        listing["page2_prefetching"] = False
                        self._commit_locked()
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

    def _accept_matches(self, matches: list[Any], model_name: str, source: str) -> ToolPayload:
        try:
            with self._state_lock:
                product = self._current_product()
                if self._matches_already_judged(product, matches):
                    snapshot = copy.deepcopy(self._published or self.state)
                    return self._payload(
                        {
                            "ok": True,
                            "status": "already_submitted",
                            "product_id": product.get("product_id"),
                            "source": source,
                            "message": "这一批已经提交过。不要重复提交。手上的下一批图还在就直接判断。",
                        },
                        state=snapshot,
                    )
                self._apply_match_decisions(product, matches, model_name)
                needs_open = self._has_pending_sales(product)
                product_id = str(product.get("product_id") or "")
                self._commit_locked()
            note = (
                "判断已记录，同款详情在后台打开。下面是下一批列表图。"
                if needs_open
                else "判断已记录。下面是下一批列表图。"
            )
            if needs_open:
                self._queue_sales(prefetch_listing=not self._current_product()["listing"].get("page2_done"))
            ready = self._ready_listing_batch(note)
            if ready is not None:
                return ready
            if needs_open or self._current_product().get("sales_pending"):
                return self._payload(
                    {
                        "ok": True,
                        "status": "sales_started",
                        "product_id": product_id,
                        "source": source,
                        "message": "判断已记录，同款详情还在打开。请只调用 session_status，下一批图或下一张主图好了会直接带上。不要重复提交。",
                    }
                )
            recorded = self._payload(
                {
                    "ok": True,
                    "status": "matches_recorded",
                    "product_id": product_id,
                    "source": source,
                    "message": "判断已记录。",
                }
            )
            return self._worker.call(self._attach_followup, recorded)
        except (UrlError, MatchInputError) as exc:
            return ToolPayload({"ok": False, "status": "error", "message": str(exc)})

    def _matches_already_judged(self, product: dict[str, Any], matches: list[Any]) -> bool:
        judged = set(product.get("judged_ids") or [])
        ids = []
        for raw in matches:
            if hasattr(raw, "model_dump"):
                data = raw.model_dump()
            elif isinstance(raw, dict):
                data = raw
            else:
                return False
            candidate_id = str(data.get("candidate_id") or "").strip()
            if not candidate_id:
                return False
            ids.append(candidate_id)
        return bool(ids) and all(candidate_id in judged for candidate_id in ids)

    def _queue_sales(self, prefetch_listing: bool) -> None:
        with self._state_lock:
            product = self._current_product()
            if product.get("sales_running"):
                product["sales_pending"] = True
                self._commit_locked()
                return
            product["sales_pending"] = True
            product["sales_error"] = ""
            product["sales_running"] = True
            self._commit_locked()

        def run() -> None:
            self._drain_sales()
            if not prefetch_listing:
                return
            product = (self.state or {}).get("products", {}).get((self.state or {}).get("current_product_id") or "")
            listing = (product or {}).get("listing") or {}
            if product and not listing.get("page2_done") and product.get("ml_search_url"):
                self._schedule_listing_prefetch(product)

        self._worker.submit(run)

    def _sales_wait_payload(self, product_id: str, source: str) -> ToolPayload | None:
        state = self._require_state()
        product = state["products"].get(product_id) or {}
        if state.get("challenge_pending"):
            return self._payload(
                {
                    "ok": False,
                    "status": "needs_human_verification",
                    "product_id": product_id,
                    "source": source,
                    "message": "需要手动验证。在前台完成后再次调用 session_status，不要重复提交。",
                }
            )
        if product.get("sales_pending"):
            detail = str(product.get("sales_error") or "")
            message = f"打开详情没有完成：{detail}。请再次 session_status，不要重复提交。" if detail else (
                "同款详情还在打开。请再次 session_status，不要重复提交。"
            )
            return self._payload(
                {
                    "ok": True,
                    "status": "sales_started",
                    "product_id": product_id,
                    "source": source,
                    "message": message,
                }
            )
        return None

    def _ready_listing_batch(self, lead: str) -> ToolPayload | None:
        """内存里已经有的下一批列表图。不翻页，方便和详情打开并行。"""

        product = self._current_product()
        if not product.get("ml_search_url"):
            return None
        listing = product["listing"]
        with self._state_lock:
            pending = list(
                _unjudged_delivered(
                    listing["candidates"],
                    listing.get("next_index"),
                    product.get("judged_ids"),
                )
            )
        if pending:
            self._ensure_candidate_files(pending, page_url=product["ml_search_url"])
            self._save()
            return self._candidate_payload(product, pending, "listing_batch", lead)
        if int(listing.get("next_index") or 0) >= len(listing.get("candidates") or []):
            return None
        batch = self._take_candidates(product)
        if not batch:
            return None
        return self._candidate_payload(product, batch, "listing_batch", lead)

    def _apply_match_decisions(self, product: dict[str, Any], matches: list[Any], model_name: str) -> None:
        kept, dropped = filter_matches(matches, self.settings.similarity_threshold)
        known = self._candidate_index(product)
        for item in kept + dropped:
            candidate = known.get(item["candidate_id"])
            if candidate is None:
                continue
            candidate["similarity"] = item["confidence"]
            candidate["similar"] = item["similar"]
            candidate["reason"] = item["reason"]
            candidate["model_name"] = model_name
            self._remember_judged(product, item["candidate_id"])

    def _has_pending_sales(self, product: dict[str, Any]) -> bool:
        return bool(self._pending_sale_items(product, "search") or self._pending_sale_items(product, "recommendation"))

    def _pending_sale_items(self, product: dict[str, Any], source: str) -> list[dict[str, Any]]:
        threshold = self.settings.similarity_threshold
        checked = set(product.get("checked_keys") or [])
        pool: list[dict[str, Any]] = []
        if source == "search":
            pool = list(product.get("listing", {}).get("candidates") or [])
        else:
            for match in product.get("matches") or []:
                if match.get("source") != "search":
                    continue
                pool.extend(match.get("ad_candidates") or [])
        items = []
        for candidate in pool:
            if not candidate.get("similar"):
                continue
            try:
                confidence = float(candidate.get("similarity") or 0)
            except (TypeError, ValueError):
                confidence = 0
            if confidence < threshold:
                continue
            if candidate.get("key") in checked:
                continue
            items.append(
                {
                    "candidate_id": candidate["candidate_id"],
                    "similar": True,
                    "confidence": confidence,
                    "reason": candidate.get("reason") or "",
                }
            )
        return items

    def _kick_sales_if_idle(self) -> None:
        with self._state_lock:
            state = self.state
            if not state:
                return
            product = (state.get("products") or {}).get(state.get("current_product_id") or "")
            if not product or not product.get("sales_pending") or product.get("sales_running"):
                return
            if not self._has_pending_sales(product):
                product["sales_pending"] = False
                product["sales_error"] = ""
                self._commit_locked()
                return
            product["sales_running"] = True
            product["sales_error"] = ""
            self._commit_locked()
        self._worker.submit(self._drain_sales)

    def _drain_sales(self) -> None:
        product_id = ""
        try:
            while True:
                with self._state_lock:
                    if self.state is None:
                        return
                    product = self._current_product()
                    product_id = str(product.get("product_id") or "")
                    listing_items = self._pending_sale_items(product, "search")
                    ad_items = self._pending_sale_items(product, "recommendation")
                    model_name = ""
                    for item in listing_items + ad_items:
                        candidate = self._candidate_index(product).get(item["candidate_id"]) or {}
                        model_name = str(candidate.get("model_name") or model_name)
                if not listing_items and not ad_items:
                    with self._state_lock:
                        if self.state and product_id in self.state.get("products", {}):
                            current = self.state["products"][product_id]
                            current["sales_pending"] = False
                            current["sales_error"] = ""
                            self._commit_locked()
                    return
                if listing_items:
                    result = self._open_judged_matches(product, listing_items, model_name, "search", None)
                    if result.data.get("status") == "needs_human_verification":
                        return
                if ad_items:
                    result = self._open_judged_matches(product, ad_items, model_name, "recommendation", "")
                    if result.data.get("status") == "needs_human_verification":
                        return
        except Exception as exc:
            log(traceback.format_exc())
            with self._state_lock:
                if self.state and product_id in self.state.get("products", {}):
                    self.state["products"][product_id]["sales_error"] = str(exc)
                    self._commit_locked()
            step("销量", f"打开详情中断：{exc}")
        finally:
            with self._state_lock:
                if self.state and product_id in self.state.get("products", {}):
                    current = self.state["products"][product_id]
                    current["sales_running"] = False
                    if not self._has_pending_sales(current):
                        current["sales_pending"] = False
                        current["sales_error"] = ""
                    self._commit_locked()

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
        with self._state_lock:
            replay = _replay_ad_batch(product)
        if replay is not None:
            match, batch = replay
            self._ensure_candidate_files(batch, page_url=match["url"])
            self._save()
            images = [(f"参照图 美客多详情主图 parent_url={match['url']}", match.get("image_path") or "")]
            payload_items = [self._candidate_summary(candidate) for candidate in batch]
            for candidate in batch:
                images.append((f"广告图 candidate_id={candidate['candidate_id']} url={candidate['url']}", candidate.get("image_path") or ""))
            return self._payload(
                {
                    "ok": True,
                    "status": "ad_batch",
                    "product_id": product["product_id"],
                    "parent_url": match["url"],
                    "amazon_title": product.get("title") or "",
                    "candidates": payload_items,
                    "title_dropped": [],
                    "message": "下面是还没判断的同一批广告图。" + self._compare_message("ad", []),
                },
                images,
            )
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
        built = self._ad_candidates(product, match, cards)
        with self._state_lock:
            match["ad_candidates"] = built
            match["ad_next_index"] = 0
            match["ads_scraped"] = True
            self._commit_locked()
        browser.close_detail()
        return None

    def _fill_listing_ads(self, product: dict[str, Any], match: dict[str, Any], page: Any) -> None:
        """列表同款详情里抓全量广告。广告商品详情不走这里。"""

        if match.get("ads_scraped"):
            return
        browser = self._ensure_browser()
        page = browser.live_page(page)
        cards = collect_ad_cards(page, browser.pause_ml_scroll)
        log(f"广告位主图 {len(cards)} 张：{public_url(match['url'])}")
        built = self._ad_candidates(product, match, cards)
        with self._state_lock:
            match["ad_candidates"] = built
            match["ad_next_index"] = 0
            match["ads_scraped"] = True
            self._commit_locked()

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

    def _fits_top(self, product: dict[str, Any], sales_text: str, item: dict[str, Any]) -> bool:
        try:
            confidence = float(item.get("confidence"))
        except (TypeError, ValueError):
            confidence = None
        return can_enter_top(
            product,
            sales_text,
            confidence=confidence,
            candidate_id=str(item.get("candidate_id") or ""),
        )

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
        with self._state_lock:
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
            self._commit_locked()
        for item in kept:
            candidate = known.get(item["candidate_id"])
            if candidate is None:
                continue
            record_from_card = False
            card_text = str(candidate.get("sales_text") or "")
            with self._state_lock:
                if candidate["key"] in product["checked_keys"]:
                    skipped.append(candidate["candidate_id"])
                    continue
                if has_sales(card_text) and not self._fits_top(product, card_text, item):
                    product["checked_keys"].append(candidate["key"])
                    skipped.append(candidate["candidate_id"])
                    self._commit_locked()
                    step("销量", f"卡片销量挤不进前10，跳过 {card_text} {public_url(candidate['url'])}")
                    continue
                record_from_card = source == "recommendation" and has_sales(card_text)
            if record_from_card:
                if not candidate.get("image_path"):
                    self._ensure_candidate_files([candidate], page_url=str(candidate.get("url") or ""))
                with self._state_lock:
                    self._remember_sale(
                        product,
                        candidate,
                        item,
                        source,
                        parent_url,
                        model_name,
                        card_text,
                        image_path=str(candidate.get("image_path") or ""),
                        price=candidate.get("price"),
                    )
                    recorded.append(product["matches"][-1])
                    self._commit_locked()
                step("销量", f"广告卡片已有销量，不打开详情 {card_text} {public_url(candidate['url'])}")
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
            price = read_price(page) or candidate.get("price")
            with self._state_lock:
                product["checked_keys"].append(candidate["key"])
                if not sales["has_sales"]:
                    skipped.append(candidate["candidate_id"])
                    self._commit_locked()
                    keep_open = False
                    record = False
                elif not self._fits_top(product, sales["sales_text"], item):
                    skipped.append(candidate["candidate_id"])
                    self._commit_locked()
                    keep_open = False
                    record = False
                else:
                    keep_open = True
                    record = True
            if not sales["has_sales"]:
                step("销量", f"没有 vendido，不记录 {public_url(candidate['url'])}")
            elif not record:
                step("销量", f"详情销量挤不进前10，跳过 {sales['sales_text']} {public_url(candidate['url'])}")
            if not keep_open:
                browser.close_detail()
                continue
            image_path = self._download(hero_image_url(page), self._image_stem(candidate["key"]), hero_locator(page), page.url)
            with self._state_lock:
                self._remember_sale(
                    product,
                    candidate,
                    item,
                    source,
                    parent_url,
                    model_name,
                    sales["sales_text"],
                    image_path,
                    price,
                )
                recorded.append(product["matches"][-1])
                saved_match = product["matches"][-1]
                self._commit_locked()
            if source == "search":
                self._fill_listing_ads(product, saved_match, page)
            step("销量", f"有销量，已记录 {sales['sales_text']} {public_url(candidate['url'])}")
            browser.close_detail()
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
            image_path = str(candidate.get("image_path") or "")
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
        """把下一步要看的图或下一张主图放进同一次返回。"""

        data = payload.data
        step_name = data.get("next_step")
        product_id = str(data.get("product_id") or "")
        if step_name in {"next_listing_batch", "submit_listing_matches"}:
            follow = self._next_listing_batch()
            if follow.data.get("status") == "listing_batch":
                follow.data["message"] = (
                    "下面是下一批列表图。"
                    + self._compare_message("listing", follow.data.get("title_dropped") or [])
                )
                return follow
            if follow.data.get("status") == "needs_human_verification":
                return follow
            payload = follow
            data = payload.data
            step_name = data.get("next_step")
            product_id = str(data.get("product_id") or product_id)
        if step_name in {"next_ad_batch", "submit_ad_matches"} and product_id:
            follow = self._next_ad_batch(product_id)
            if follow.data.get("status") == "ad_batch":
                follow.data["message"] = (
                    "下面是下一批广告图。"
                    + self._compare_message("ad", follow.data.get("title_dropped") or [])
                )
                return follow
            if follow.data.get("status") == "needs_human_verification":
                return follow
            payload = follow
            data = payload.data
            step_name = data.get("next_step")
            product_id = str(data.get("product_id") or product_id)
        if step_name == "export_results":
            return self._handoff_finished(product_id)
        data["message"] = self._step_message(str(data.get("message") or ""), str(data.get("next_step") or ""))
        return payload

    def _handoff_finished(self, product_id: str) -> ToolPayload:
        """列表和广告都结束后自己写表。整店接着打开下一条。"""

        waiting = self._sales_wait_payload(product_id, "")
        if waiting is not None:
            return waiting
        self._start_export()
        state = self._require_state()
        if state.get("single_product") or state.get("amazon_done"):
            return self._payload(
                {
                    "ok": True,
                    "status": "finished",
                    "product_id": product_id,
                    "next_step": "done",
                    "message": "这一条已经筛完，表格在后台写入。不要再调用 export_results 或 next_amazon_product。",
                }
            )
        opened = self._next_amazon_product()
        if opened.data.get("status") == "amazon_product":
            opened.data["message"] = (
                "下一条亚马逊商品已打开。请直接 set_keywords。"
                "上一条的表格在后台写入，不要调用 export_results。"
                "卖家精灵和货源查询已在后台开始，不要等它们。"
            )
        elif opened.data.get("status") == "end_of_store":
            opened.data["message"] = "店铺列表已经读完。表格在后台写入，不要再调用 export_results 或 next_amazon_product。"
        return opened

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
        with self._state_lock:
            self._add_candidates_locked(product, cards, page_no)

    def _add_candidates_locked(self, product: dict[str, Any], cards: list[dict[str, str]], page_no: int) -> None:
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
        with self._state_lock:
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
            f"{skipped}由当前模型看完这一批，一次提交。"
            "先对照亚马逊标题。标题已经能明显看出不是同一商品的，直接记 similar=false、confidence=1，不要再看图。"
            "标题对得上的，看本批已经返回的图。包装、折叠、带脚整机、生活场景、只拍局部、装套演示等构图已经明显不同的，记 similar=false。"
            "颜色不同也可以算同款。外形、结构和整体构图高度相似则为 true，外形明显不同则为 false。"
            f"判断完调用 {submit}，model_name 填当前模型名。"
        )

    def _candidate_payload(
        self,
        product: dict[str, Any],
        batch: list[dict[str, Any]],
        status: str,
        lead: str = "",
    ) -> ToolPayload:
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
                "message": f"{lead}{self._compare_message('listing', dropped)}",
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
        for candidate in candidates:
            if candidate.get("image_path"):
                continue
            url = str(candidate.get("image_url") or "")
            stem = self._image_stem(candidate["candidate_id"])
            path = self._fetch_candidate_image(url, stem, page_url)
            if not path:
                continue
            with self._state_lock:
                if not candidate.get("image_path"):
                    candidate["image_path"] = path

    def _fetch_candidate_image(self, url: str, stem: Path, page_url: str) -> str:
        if url:
            try:
                return str(save_original(_http_image(url, page_url), stem))
            except Exception as exc:
                log(f"直接下载图片失败：{exc}")
        if self._browser_busy():
            return ""
        if threading.current_thread() is self._worker._thread:
            return self._download(url, stem, None, page_url)
        return self._worker.call(self._download, url, stem, None, page_url)

    def _browser_busy(self) -> bool:
        state = self.state or {}
        product = (state.get("products") or {}).get(state.get("current_product_id") or "") or {}
        listing = product.get("listing") or {}
        return bool(product.get("sales_running") or product.get("sales_pending") or listing.get("page2_prefetching") or product.get("ad_prefetching"))

    def _fast_listing_replay(self) -> ToolPayload | None:
        """已经发给模型、但还没提交的列表图。不占用正在开详情的浏览器。"""

        with self._state_lock:
            if self.state is None:
                return None
            try:
                product = self._current_product()
            except UrlError:
                return None
            listing = product.get("listing") or {}
            if not product.get("ml_search_url"):
                return None
            pending = list(
                _unjudged_delivered(listing.get("candidates") or [], listing.get("next_index"), product.get("judged_ids"))
            )
            page_url = str(product.get("ml_search_url") or "")
        if not pending:
            return None
        self._ensure_candidate_files(pending, page_url=page_url)
        self._save()
        payload = self._candidate_payload(product, pending, "listing_batch")
        payload.data["message"] = "下面是还没判断的同一批列表图。" + self._compare_message(
            "listing", payload.data.get("title_dropped") or []
        )
        return payload

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


def _http_image(url: str, referer: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "Referer": referer or url,
            "User-Agent": "Mozilla/5.0",
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        body = response.read()
    if not body:
        raise RuntimeError(f"图片下载失败：空响应 {url}")
    return body


class _Blocked(Exception):
    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(NEEDS_HUMAN)
        self.payload = payload


def _unjudged_delivered(candidates: list[dict[str, Any]], cursor: Any, judged_ids: Any) -> list[dict[str, Any]]:
    judged = set(judged_ids or [])
    sent = list(candidates or [])[: int(cursor or 0)]
    return [item for item in sent if item.get("candidate_id") not in judged]


def _replay_ad_batch(product: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    judged = set(product.get("judged_ids") or [])
    for match in product.get("matches") or []:
        if match.get("source") != "search":
            continue
        candidates = list(match.get("ad_candidates") or [])
        cursor = len(candidates) if match.get("ads_complete") else int(match.get("ad_next_index") or 0)
        pending = [item for item in candidates[:cursor] if item.get("candidate_id") not in judged]
        if pending:
            return match, pending[:BATCH_SIZE]
    return None


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
