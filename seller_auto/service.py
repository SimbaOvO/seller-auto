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
from seller_auto.matches import MatchInputError, filter_matches
from seller_auto.mercadolibre import (
    collect_ad_cards,
    collect_grid_cards,
    hero_image_url,
    hero_locator,
    open_next_results_page,
    read_sales_text,
    sales_from_text,
)
from seller_auto.pacing import DelaySettings
from seller_auto.storage import (
    atomic_write_json,
    build_export,
    export_filename,
    find_state_by_session,
    load_json,
    seller_dir,
    state_path,
)
from seller_auto.urls import UrlError, clean_keywords, ml_search_url, parse_seller_id

BATCH_SIZE = 8


@dataclass
class ToolPayload:
    data: dict[str, Any]
    images: list[tuple[str, bytes]] = field(default_factory=list)


class _Worker:
    def __init__(self) -> None:
        self._queue: queue.Queue[Any] = queue.Queue()
        self._thread = threading.Thread(target=self._loop, name="seller-auto-browser", daemon=True)
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

    def call(self, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        slot: queue.Queue[Any] = queue.Queue(maxsize=1)
        self._queue.put((function, args, kwargs, slot))
        ok, value = slot.get()
        if not ok:
            raise value
        return value


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
        headless: bool | None = None,
        worker: _Worker | None = None,
    ) -> None:
        self.settings = settings.validate()
        self.output_dir = Path(output_dir or settings.output_dir)
        self.profile_dir = Path(profile_dir or settings.profile_dir)
        self.headless = settings.headless if headless is None else headless
        self.browser: ArgentinaBrowser | None = None
        self.state: dict[str, Any] | None = None
        self._worker = worker or _Worker()

    def open_store(self, url: str) -> ToolPayload:
        return self._guard(self._open_store, url)

    def next_amazon_product(self) -> ToolPayload:
        return self._guard(self._next_amazon_product)

    def set_keywords(self, product_id: str, keywords: str) -> ToolPayload:
        return self._guard(self._set_keywords, product_id, keywords)

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
        state = self._require_state()
        if self.browser is not None:
            state["challenge_pending"] = self.browser.challenge_pending
            state["challenge_site"] = self.browser.challenge_site
        state["updated_at"] = _now()
        atomic_write_json(state_path(self.output_dir, state["seller_id"]), state)

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
        }

    def _payload(self, data: dict[str, Any], images: list[tuple[str, str]] | None = None) -> ToolPayload:
        if self.state is not None:
            data = {**self._status(), **data}
        encoded: list[tuple[str, bytes]] = []
        for caption, path in images or []:
            jpeg = _jpeg_file(path)
            if jpeg is not None:
                encoded.append((caption, jpeg))
        return ToolPayload(data, encoded)

    def _blocked(self, blocked: dict[str, Any]) -> ToolPayload:
        self._save()
        return self._payload(blocked)

    def _open_store(self, url: str) -> ToolPayload:
        seller_id = parse_seller_id(url)
        browser = self._ensure_browser()
        path = state_path(self.output_dir, seller_id)
        resumed = path.exists()
        if resumed:
            self.state = load_json(path)
        else:
            self.state = _new_state(str(uuid.uuid4()), seller_id, url)
        self._apply_challenge_flag()
        browser.amazon_cooldown.acquire()
        target = self.state.get("list_url") or url
        browser.goto(browser.list_page, target)
        blocked = browser.ensure_clear(browser.list_page, "amazon")
        if blocked:
            return self._blocked(blocked)
        browser.pause_amazon_read()
        self.state["list_url"] = browser.list_page.url
        self._save()
        return self._payload(
            {
                "ok": True,
                "status": "store_ready",
                "resumed": resumed,
                "message": "店铺已打开。请调用 next_amazon_product 读取下一条。",
            }
        )

    def _resume_session(self, session_id: str) -> ToolPayload:
        path = find_state_by_session(self.output_dir, session_id.strip())
        if path is None:
            return ToolPayload({"ok": False, "status": "error", "message": "没有找到这个会话"})
        self.state = load_json(path)
        browser = self._ensure_browser()
        self._apply_challenge_flag()
        target = self.state.get("list_url") or self.state["store_url"]
        browser.amazon_cooldown.acquire()
        browser.close_detail()
        browser.goto(browser.list_page, target)
        blocked = browser.ensure_clear(browser.list_page, "amazon")
        if blocked:
            return self._blocked(blocked)
        browser.pause_amazon_read()
        self._save()
        return self._payload({"ok": True, "status": "resumed", "message": "已从本地进度恢复"})

    def _session_status(self) -> ToolPayload:
        self._require_state()
        return self._payload({"ok": True, "status": "status"})

    def _export_results(self) -> ToolPayload:
        state = self._require_state()
        payload = build_export(state)
        path = seller_dir(self.output_dir, state["seller_id"]) / export_filename()
        atomic_write_json(path, payload)
        return self._payload({"ok": True, "status": "exported", "path": str(path)})

    def _next_amazon_product(self) -> ToolPayload:
        state = self._require_state()
        if state["amazon_done"]:
            return self._payload({"ok": True, "status": "end_of_store", "message": "店铺列表已经读完"})
        if self.settings.max_products and len(state["product_order"]) >= self.settings.max_products:
            return self._payload({"ok": True, "status": "limit_reached", "message": "已达到 max_products"})
        browser = self._ensure_browser()
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
        browser.pause_amazon_read()
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
            browser.finish_detail(opened_new_tab, state["list_url"])
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
            browser.finish_detail(opened_new_tab, state["list_url"])
            self._save()
            return self._payload(
                {
                    "ok": True,
                    "status": "excluded",
                    "reason": reason,
                    "product_id": key,
                    "title": product["title"] or entry["text"],
                    "url": product["url"] or entry["href"],
                    "message": f"已跳过{reason}类商品。请继续调用 next_amazon_product。",
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
            "keywords": "",
            "ml_search_url": "",
            "listing": _empty_listing(),
            "judged_ids": [],
            "checked_keys": [],
            "matches": [],
        }
        state["product_order"].append(key)
        state["current_product_id"] = key
        browser.finish_detail(opened_new_tab, state["list_url"])
        self._save()
        return self._payload(
            {
                "ok": True,
                "status": "amazon_product",
                "product_id": key,
                "title": state["products"][key]["title"],
                "brand": product["brand"],
                "url": state["products"][key]["url"],
                "message": "请根据主图给出西班牙语搜索关键词，然后调用 set_keywords。颜色可以忽略，保留品名。",
            },
            [(f"亚马逊主图 product_id={key}", image_path)],
        )

    def _set_keywords(self, product_id: str, keywords: str) -> ToolPayload:
        state = self._require_state()
        product = self._product(product_id)
        text = clean_keywords(keywords)
        search_url = ml_search_url(text)
        if product.get("keywords") != text:
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
        return self._payload(
            {
                "ok": True,
                "status": "keywords_ready",
                "product_id": product_id,
                "keywords": text,
                "ml_search_url": search_url,
                "message": "搜索页已打开。请调用 next_listing_batch 获取列表主图。",
            }
        )

    def _next_listing_batch(self) -> ToolPayload:
        state = self._require_state()
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
            return self._candidate_payload(product, batch, "listing_batch")
        if listing["page2_done"]:
            message = "前两页已看完" if listing["candidates"] else "前两页没有找到商品主图"
            return self._payload(
                {"ok": True, "status": "listing_complete", "product_id": product["product_id"], "message": message}
            )
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
        batch = self._take_candidates(product)
        if not batch:
            message = "前两页已看完" if product["listing"]["candidates"] else "前两页没有找到商品主图"
            return self._payload(
                {"ok": True, "status": "listing_complete", "product_id": product["product_id"], "message": message}
            )
        return self._candidate_payload(product, batch, "listing_batch")

    def _submit_listing_matches(self, matches: list[Any], model_name: str) -> ToolPayload:
        product = self._current_product()
        return self._open_judged_matches(product, matches, model_name, source="search", parent_url=None)

    def _next_ad_batch(self, product_id: str) -> ToolPayload:
        product = self._product(product_id)
        browser = self._ensure_browser()
        self._require_state()["current_product_id"] = product_id
        for match in product["matches"]:
            if match.get("source") != "search" or match.get("ads_complete"):
                continue
            if not match.get("ads_scraped"):
                browser.ml_cooldown.acquire()
                page = browser.open_detail(match["url"])
                blocked = browser.ensure_clear(page, "ml")
                if blocked:
                    return self._blocked(blocked)
                browser.pause_ml_read()
                cards = collect_ad_cards(page, browser.pause_ml_scroll)
                match["ad_candidates"] = self._ad_candidates(product, match, cards)
                match["ad_next_index"] = 0
                match["ads_scraped"] = True
                browser.close_detail()
                self._save()
            start = int(match.get("ad_next_index") or 0)
            batch = match["ad_candidates"][start : start + BATCH_SIZE]
            if not batch:
                match["ads_complete"] = True
                self._save()
                continue
            self._ensure_candidate_files(batch, page_url=match["url"])
            match["ad_next_index"] = start + len(batch)
            if match["ad_next_index"] >= len(match["ad_candidates"]):
                match["ads_complete"] = True
            self._save()
            images = [(f"参照图 美客多详情主图 parent_url={match['url']}", match.get("image_path") or "")]
            payload_items = []
            for candidate in batch:
                payload_items.append(self._candidate_summary(candidate))
                images.append((f"广告图 candidate_id={candidate['candidate_id']} url={candidate['url']}", candidate.get("image_path") or ""))
            return self._payload(
                {
                    "ok": True,
                    "status": "ad_batch",
                    "product_id": product["product_id"],
                    "parent_url": match["url"],
                    "candidates": payload_items,
                    "message": "请把广告图和参照图对比。颜色可以不同，外形和整体构图要高度相似。然后调用 submit_ad_matches。",
                },
                images,
            )
        return self._payload(
            {
                "ok": True,
                "status": "ads_complete",
                "product_id": product["product_id"],
                "message": "这个亚马逊商品的广告位已经看完。可以继续 next_amazon_product，或 export_results。",
            }
        )

    def _submit_ad_matches(self, matches: list[Any], model_name: str) -> ToolPayload:
        product = self._current_product()
        if not matches:
            return self._payload({"ok": True, "status": "no_matches", "message": "没有提交广告判断"})
        return self._open_judged_matches(product, matches, model_name, source="recommendation", parent_url="")

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
        for item in dropped:
            if item["candidate_id"] in known:
                self._remember_judged(product, item["candidate_id"])
            else:
                unknown.append(item["candidate_id"])
        for item in kept:
            candidate = known.get(item["candidate_id"])
            if candidate is None:
                unknown.append(item["candidate_id"])
                continue
            self._remember_judged(product, candidate["candidate_id"])
            if candidate["key"] in product["checked_keys"]:
                skipped.append(candidate["candidate_id"])
                continue
            browser = self._ensure_browser()
            browser.ml_cooldown.acquire()
            page = browser.open_detail(candidate["url"])
            blocked = browser.ensure_clear(page, "ml")
            if blocked:
                self._save()
                return self._blocked(blocked)
            browser.pause_ml_read()
            sales = sales_from_text(read_sales_text(page))
            product["checked_keys"].append(candidate["key"])
            if not sales["has_sales"]:
                browser.close_detail()
                self._save()
                skipped.append(candidate["candidate_id"])
                continue
            image_path = self._download(hero_image_url(page), self._image_stem(candidate["key"]), hero_locator(page), page.url)
            parent = candidate.get("parent_url") if source == "recommendation" else parent_url
            match = {
                "url": candidate["url"],
                "key": candidate["key"],
                "title": candidate.get("title") or "",
                "sales_text": sales["sales_text"],
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
            product["matches"].append(match)
            recorded.append(match)
            browser.close_detail()
            self._save()
        images = [
            (f"美客多详情主图 candidate_id={item['candidate_id']} url={item['url']}", item.get("image_path") or "")
            for item in recorded
        ]
        self._save()
        return self._payload(
            {
                "ok": True,
                "status": "matches_recorded" if recorded else "no_sales_or_not_similar",
                "product_id": product["product_id"],
                "source": source,
                "recorded": [self._match_summary(item) for item in recorded],
                "skipped": skipped,
                "unknown_ids": unknown,
                "message": "有销量的链接已记录。没有 vendido 的链接不记录。",
            },
            images,
        )

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
                    "key": card["key"],
                    "page": page_no,
                    "parent_url": "",
                }
            )
            existing.add(card["key"])

    def _take_candidates(self, product: dict[str, Any]) -> list[dict[str, Any]]:
        listing = product["listing"]
        start = int(listing["next_index"])
        batch = listing["candidates"][start : start + BATCH_SIZE]
        if not batch:
            return []
        self._ensure_candidate_files(batch, page_url=product["ml_search_url"])
        listing["next_index"] = start + len(batch)
        self._save()
        return batch

    def _candidate_payload(self, product: dict[str, Any], batch: list[dict[str, Any]], status: str) -> ToolPayload:
        reference = product.get("image_path") or ""
        images = [(f"参照图 亚马逊主图 product_id={product['product_id']}", reference)]
        summaries = []
        for candidate in batch:
            summaries.append(self._candidate_summary(candidate))
            images.append(
                (f"列表图 candidate_id={candidate['candidate_id']} url={candidate['url']}", candidate.get("image_path") or "")
            )
        return self._payload(
            {
                "ok": True,
                "status": status,
                "product_id": product["product_id"],
                "candidates": summaries,
                "message": "请对比列表图和亚马逊参照图。颜色不同也可以，整体外形和构图要高度相似。然后调用 submit_listing_matches。未判断完不要把它们当成同款。",
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
        return seller_dir(self.output_dir, state["seller_id"]) / "images" / safe

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
