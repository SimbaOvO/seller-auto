import tempfile
import threading
import time
import unittest
from pathlib import Path

from seller_auto.pacing import DelaySettings
from seller_auto.service import SellerAutoService, _new_state, _replay_ad_batch


def _state() -> dict:
    state = _new_state("sess", "A1A10HFD4KEZW5", "https://www.amazon.com.mx/s?me=A1A10HFD4KEZW5")
    state["single_product"] = "B07LCLQ47D"
    state["run_id"] = "run-1"
    state["current_product_id"] = "B07LCLQ47D"
    state["product_order"] = ["B07LCLQ47D"]
    state["products"]["B07LCLQ47D"] = {
        "product_id": "B07LCLQ47D",
        "url": "https://www.amazon.com.mx/dp/B07LCLQ47D",
        "title": "Cubierta",
        "keywords": "cubiertas para estufa",
        "ml_search_url": "https://listado.mercadolibre.com.ar/cubiertas-para-estufa",
        "listing": {
            "page1_done": True,
            "page2_done": True,
            "next_index": 1,
            "candidates": [
                {
                    "candidate_id": "c1",
                    "url": "https://articulo.mercadolibre.com.ar/MLA-1",
                    "title": "Cubierta para estufa",
                    "image_path": "kept.jpg",
                    "key": "MLA1",
                }
            ],
        },
        "judged_ids": [],
        "checked_keys": [],
        "matches": [
            {
                "source": "search",
                "url": "https://articulo.mercadolibre.com.ar/MLA-1",
                "key": "MLA1",
                "ads_scraped": True,
                "ads_complete": True,
                "ad_next_index": 1,
                "image_path": "kept.jpg",
                "ad_candidates": [
                    {
                        "candidate_id": "ad1",
                        "url": "https://articulo.mercadolibre.com.ar/MLA-8",
                        "title": "Ad",
                        "image_path": "kept.jpg",
                        "key": "MLA8",
                    }
                ],
            }
        ],
    }
    return state


def _mark_needs_detail(state: dict) -> None:
    candidate = state["products"]["B07LCLQ47D"]["listing"]["candidates"][0]
    candidate["similar"] = True
    candidate["similarity"] = 0.91
    candidate["relation"] = "same"


class DispatchTests(unittest.TestCase):
    def test_submit_without_more_listings_exports_and_finishes(self) -> None:
        drained = threading.Event()
        exported = threading.Event()
        with tempfile.TemporaryDirectory() as directory:
            service = SellerAutoService(DelaySettings(), output_dir=directory)
            state = _state()
            state["products"]["B07LCLQ47D"]["judged_ids"] = ["ad1"]
            service.state = state
            service._save()

            def fake_drain() -> None:
                drained.set()
                with service._state_lock:
                    product = service.state["products"]["B07LCLQ47D"]
                    product["checked_keys"].append("MLA1")
                    product["sales_running"] = False
                    product["sales_pending"] = False
                    service._commit_locked()

            def fake_export() -> None:
                exported.set()

            service._drain_sales = fake_drain
            service._start_export = fake_export
            service.background_wait_seconds = 3
            submitted = service.submit_listing_matches(
                [{"candidate_id": "c1", "similar": True, "confidence": 0.91, "reason": "同款"}],
                "cursor-grok-4.5-low-fast",
            )
            if submitted.data["status"] != "finished":
                self.assertEqual(submitted.data["status"], "sales_started")
                self.assertIn("不要重复提交", submitted.data["message"])
                service._worker.call(lambda: None)
                payload = service.session_status()
            else:
                payload = submitted
            self.assertEqual(payload.data["status"], "finished")
            self.assertEqual(payload.data["next_step"], "done")
            self.assertIn("不要再调用 export_results", payload.data["message"])
            self.assertNotIn("还在打开", payload.data["message"])
            self.assertTrue(drained.is_set())
            self.assertTrue(exported.is_set())
            duplicate = service.submit_listing_matches(
                [{"candidate_id": "c1", "similar": True, "confidence": 0.91, "reason": "同款"}],
                "cursor-grok-4.5-low-fast",
            )
            self.assertEqual(duplicate.data["status"], "already_submitted")

    def test_submit_returns_buffered_listing_while_details_open(self) -> None:
        started = threading.Event()
        release = threading.Event()
        with tempfile.TemporaryDirectory() as directory:
            service = SellerAutoService(DelaySettings(), output_dir=directory)
            state = _state()
            listing = state["products"]["B07LCLQ47D"]["listing"]
            listing["candidates"].append(
                {
                    "candidate_id": "c2",
                    "url": "https://articulo.mercadolibre.com.ar/MLA-2",
                    "title": "Cubierta para estufa",
                    "image_path": "kept.jpg",
                    "key": "MLA2",
                }
            )
            service.state = state
            service._save()
            service._ensure_candidate_files = lambda candidates, page_url="": None

            def fake_drain() -> None:
                started.set()
                self.assertTrue(release.wait(timeout=2))
                with service._state_lock:
                    product = service.state["products"]["B07LCLQ47D"]
                    product["sales_running"] = False
                    product["sales_pending"] = False
                    service._commit_locked()

            service._drain_sales = fake_drain
            service.background_wait_seconds = 2
            payload = service.submit_listing_matches(
                [{"candidate_id": "c1", "similar": True, "confidence": 0.91, "reason": "同款"}],
                "cursor-grok-4.5-low-fast",
            )
            self.assertEqual(payload.data["status"], "listing_batch")
            self.assertEqual(payload.data["candidates"][0]["candidate_id"], "c2")
            self.assertIn("后台打开", payload.data["message"])
            self.assertTrue(started.wait(timeout=2))
            release.set()
            service._worker.call(lambda: None)

    def test_store_submit_opens_next_amazon_product(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = SellerAutoService(DelaySettings(), output_dir=directory)
            state = _state()
            state["single_product"] = ""
            state["products"]["B07LCLQ47D"]["judged_ids"] = ["ad1"]
            service.state = state
            service._save()

            release = threading.Event()

            def fake_drain() -> None:
                self.assertTrue(release.wait(timeout=2))
                with service._state_lock:
                    product = service.state["products"]["B07LCLQ47D"]
                    product["sales_running"] = False
                    product["sales_pending"] = False
                    service._commit_locked()

            service._drain_sales = fake_drain
            service._start_export = lambda: None
            service.background_wait_seconds = 3
            service._next_amazon_product = lambda: service._payload(
                {"ok": True, "status": "amazon_product", "product_id": "B0NEXT", "message": "旧提示"}
            )
            submitted = service.submit_listing_matches(
                [{"candidate_id": "c1", "similar": True, "confidence": 0.91, "reason": "同款"}],
                "cursor-grok-4.5-low-fast",
            )
            self.assertEqual(submitted.data["status"], "sales_started")
            self.assertIn("不要重复提交", submitted.data["message"])
            release.set()
            service._worker.call(lambda: None)
            payload = service.session_status()
            self.assertEqual(payload.data["status"], "amazon_product")
            self.assertEqual(payload.data["product_id"], "B0NEXT")
            self.assertIn("不要调用 export_results", payload.data["message"])
            self.assertIn("set_keywords", payload.data["message"])
            self.assertNotIn("还在打开", payload.data["message"])

    def test_lost_listing_batch_is_sent_again(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = SellerAutoService(DelaySettings(), output_dir=directory)
            service.state = _state()
            service._ensure_browser = lambda: None
            payload = service._next_listing_batch()
            self.assertEqual(payload.data["candidates"][0]["candidate_id"], "c1")
            self.assertEqual(service.state["products"]["B07LCLQ47D"]["listing"]["next_index"], 1)

    def test_browser_image_download_stays_on_the_browser_thread(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = SellerAutoService(DelaySettings(), output_dir=directory)
            seen: dict[str, str] = {}

            def download(url: str, stem: Path, locator: object, referer: str) -> str:
                seen["thread"] = threading.current_thread().name
                return "saved.jpg"

            service._download = download
            saved = service._fetch_candidate_image("", Path(directory) / "x", "")
            self.assertEqual(saved, "saved.jpg")
            self.assertEqual(seen["thread"], "seller-auto-browser")

    def test_lost_ad_batch_is_sent_again(self) -> None:
        product = _state()["products"]["B07LCLQ47D"]
        match, batch = _replay_ad_batch(product)
        self.assertEqual(match["url"].endswith("MLA-1"), True)
        self.assertEqual(batch[0]["candidate_id"], "ad1")

    def test_submit_returns_while_details_are_still_opening(self) -> None:
        release = threading.Event()
        started = threading.Event()
        with tempfile.TemporaryDirectory() as directory:
            service = SellerAutoService(DelaySettings(), output_dir=directory)
            state = _state()
            state["products"]["B07LCLQ47D"]["judged_ids"] = ["ad1"]
            service.state = state
            service._save()
            service.background_wait_seconds = 5
            service._start_export = lambda: None

            def fake_drain() -> None:
                started.set()
                self.assertTrue(release.wait(timeout=3))
                with service._state_lock:
                    product = service.state["products"]["B07LCLQ47D"]
                    product["sales_running"] = False
                    product["sales_pending"] = False
                    service._commit_locked()

            service._drain_sales = fake_drain
            started_at = time.monotonic()
            payload = service.submit_listing_matches(
                [{"candidate_id": "c1", "similar": True, "confidence": 0.91, "reason": "同款"}],
                "cursor-grok-4.5-low-fast",
            )
            self.assertLess(time.monotonic() - started_at, 1)
            self.assertTrue(started.wait(timeout=2))
            self.assertEqual(payload.data["status"], "sales_started")
            self.assertIn("session_status", payload.data["message"])
            self.assertIn("不要重复提交", payload.data["message"])
            again = service.submit_listing_matches(
                [{"candidate_id": "c1", "similar": True, "confidence": 0.91, "reason": "同款"}],
                "cursor-grok-4.5-low-fast",
            )
            self.assertLess(time.monotonic() - started_at, 1)
            self.assertEqual(again.data["status"], "sales_started")
            self.assertIn("不要重复提交", again.data["message"])
            release.set()
            service._worker.call(lambda: None)
            finished = service.session_status()
            self.assertEqual(finished.data["status"], "finished")

    def test_session_status_waits_until_details_finish(self) -> None:
        release = threading.Event()
        started = threading.Event()
        with tempfile.TemporaryDirectory() as directory:
            service = SellerAutoService(DelaySettings(), output_dir=directory)
            state = _state()
            product = state["products"]["B07LCLQ47D"]
            product["judged_ids"] = ["c1", "ad1"]
            product["chinese_name"] = "气管插管训练头模"
            product["sales_pending"] = True
            _mark_needs_detail(state)
            service.state = state
            service._save()
            service.background_wait_seconds = 5
            service._start_export = lambda: None

            def fake_drain() -> None:
                started.set()
                self.assertTrue(release.wait(timeout=3))
                with service._state_lock:
                    current = service.state["products"]["B07LCLQ47D"]
                    current["sales_running"] = False
                    current["sales_pending"] = False
                    current["checked_keys"].append("MLA1")
                    service._commit_locked()

            service._drain_sales = fake_drain
            result: dict[str, object] = {}

            def call() -> None:
                result["payload"] = service.session_status()

            thread = threading.Thread(target=call)
            thread.start()
            self.assertTrue(started.wait(timeout=2))
            time.sleep(0.3)
            self.assertTrue(thread.is_alive())
            release.set()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            payload = result["payload"]
            assert hasattr(payload, "data")
            self.assertEqual(payload.data["status"], "finished")
            self.assertNotIn("还在打开", payload.data["message"])

    def test_detail_wait_reports_progress_instead_of_spinning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = SellerAutoService(DelaySettings(), output_dir=directory)
            state = _state()
            product = state["products"]["B07LCLQ47D"]
            product["sales_pending"] = True
            product["sales_running"] = True
            product["chinese_name"] = "气管插管训练头模"
            _mark_needs_detail(state)
            service.state = state
            service._save()
            service.background_wait_seconds = 0.35
            started = time.monotonic()
            payload = service.session_status()
            elapsed = time.monotonic() - started
            self.assertGreater(elapsed, 0.2)
            self.assertLess(elapsed, 2)
            self.assertEqual(payload.data["status"], "sales_started")
            self.assertIn("还剩 1 条详情", payload.data["message"])
            self.assertIn("气管插管训练头模", payload.data["message"])
            self.assertIn("这一次已经等了", payload.data["message"])
            self.assertIn("请再调用一次 session_status", payload.data["message"])
            self.assertIn("不要连续查询", payload.data["message"])

    def test_detail_error_returns_without_the_long_wait(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = SellerAutoService(DelaySettings(), output_dir=directory)
            state = _state()
            product = state["products"]["B07LCLQ47D"]
            product["sales_pending"] = True
            product["sales_error"] = "net"
            _mark_needs_detail(state)
            service.state = state
            service._save()
            service.background_wait_seconds = 30

            calls: list[int] = []

            def fake_drain() -> None:
                calls.append(1)
                with service._state_lock:
                    current = service.state["products"]["B07LCLQ47D"]
                    current["sales_error"] = "net"
                    current["sales_running"] = False
                    current["sales_pending"] = True
                    service._commit_locked()

            service._drain_sales = fake_drain
            started = time.monotonic()
            payload = service.session_status()
            self.assertLess(time.monotonic() - started, 2)
            self.assertEqual(len(calls), 2)
            self.assertIn("没有完成", payload.data["message"])
            self.assertIn("net", payload.data["message"])
            self.assertIn("已经重试过", payload.data["message"])
            self.assertNotIn("请再调用一次 session_status", payload.data["message"])
            self.assertIn("不要重复提交", payload.data["message"])

    def test_verification_returns_without_the_long_wait(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = SellerAutoService(DelaySettings(), output_dir=directory)
            state = _state()
            product = state["products"]["B07LCLQ47D"]
            product["sales_pending"] = True
            state["challenge_pending"] = True
            _mark_needs_detail(state)
            service.state = state
            service._save()
            service.background_wait_seconds = 30
            calls: list[int] = []
            service._drain_sales = lambda: calls.append(1)
            started = time.monotonic()
            payload = service.session_status()
            self.assertLess(time.monotonic() - started, 1)
            self.assertEqual(calls, [])
            self.assertFalse(service.state["products"]["B07LCLQ47D"].get("sales_running"))
            self.assertIn("手动验证", payload.data["message"])
            self.assertNotIn("还在打开", payload.data["message"])
            self.assertNotIn("请再调用一次 session_status", payload.data["message"])

    def test_overlapping_status_calls_follow_through_once(self) -> None:
        release = threading.Event()
        started = threading.Event()
        exports: list[int] = []
        with tempfile.TemporaryDirectory() as directory:
            service = SellerAutoService(DelaySettings(), output_dir=directory)
            state = _state()
            product = state["products"]["B07LCLQ47D"]
            product["judged_ids"] = ["c1", "ad1"]
            product["sales_pending"] = True
            _mark_needs_detail(state)
            service.state = state
            service._save()
            service.background_wait_seconds = 5

            def fake_drain() -> None:
                started.set()
                self.assertTrue(release.wait(timeout=3))
                with service._state_lock:
                    current = service.state["products"]["B07LCLQ47D"]
                    current["sales_running"] = False
                    current["sales_pending"] = False
                    service._commit_locked()

            def fake_export() -> None:
                exports.append(1)

            service._drain_sales = fake_drain
            service._start_export = fake_export
            results: list[str] = []

            def call() -> None:
                results.append(service.session_status().data["status"])

            first = threading.Thread(target=call)
            second = threading.Thread(target=call)
            first.start()
            self.assertTrue(started.wait(timeout=2))
            second.start()
            time.sleep(0.2)
            release.set()
            first.join(timeout=3)
            second.join(timeout=3)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(exports, [1])
            self.assertEqual(results, ["finished", "finished"])

    def test_status_returns_the_open_amazon_image_while_keywords_are_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = SellerAutoService(DelaySettings(), output_dir=directory)
            state = _state()
            state["single_product"] = ""
            state["export_pending"] = True
            state["export_running"] = True
            product = state["products"]["B07LCLQ47D"]
            product["keywords"] = ""
            product["ml_search_url"] = ""
            product["judged_ids"] = []
            service.state = state
            service._save()
            service.background_wait_seconds = 5
            payload = service.session_status()
            self.assertEqual(payload.data["status"], "amazon_product")
            self.assertEqual(payload.data["product_id"], "B07LCLQ47D")
            self.assertIn("set_keywords", payload.data["message"])
            self.assertNotIn("蓝鲸", payload.data["message"])

    def test_status_returns_ads_while_previous_export_is_still_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = SellerAutoService(DelaySettings(), output_dir=directory)
            state = _state()
            state["products"]["B07LCLQ47D"]["judged_ids"] = ["c1"]
            state["export_pending"] = True
            state["export_running"] = True
            service.state = state
            service._save()
            service._ensure_candidate_files = lambda candidates, page_url="": None
            service.background_wait_seconds = 5
            payload = service.session_status()
            self.assertEqual(payload.data["status"], "ad_batch")
            self.assertEqual(payload.data["candidates"][0]["candidate_id"], "ad1")
            self.assertNotIn("蓝鲸", payload.data["message"])

    def test_session_status_waits_out_export_when_nothing_is_left_to_judge(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = SellerAutoService(DelaySettings(), output_dir=directory)
            state = _state()
            state["single_product"] = ""
            product = state["products"]["B07LCLQ47D"]
            product["judged_ids"] = ["c1", "ad1"]
            state["export_pending"] = True
            state["export_running"] = True
            service.state = state
            service._save()
            service.background_wait_seconds = 5
            service._start_export = lambda: (_ for _ in ()).throw(AssertionError("写表还没结束就又导出了"))
            result: dict[str, object] = {}

            def call() -> None:
                result["payload"] = service.session_status()

            thread = threading.Thread(target=call)
            thread.start()
            time.sleep(0.3)
            self.assertTrue(thread.is_alive())
            with service._state_lock:
                service.state["export_pending"] = False
                service.state["export_running"] = False
                service.state["products"]["B07LCLQ47D"]["workbook_written"] = True
                service.state["export_result"] = {
                    "message": "这一条商品的结果已追加到 book。请继续 next_amazon_product，直到返回 end_of_store。",
                    "product_id": "B07LCLQ47D",
                }
                service._commit_locked()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            payload = result["payload"]
            assert hasattr(payload, "data")
            self.assertIn("next_amazon_product", payload.data["message"])

    def test_retry_submit_does_not_join_the_detail_wait(self) -> None:
        release = threading.Event()
        started = threading.Event()
        exports: list[int] = []
        with tempfile.TemporaryDirectory() as directory:
            service = SellerAutoService(DelaySettings(), output_dir=directory)
            state = _state()
            state["products"]["B07LCLQ47D"]["judged_ids"] = ["ad1"]
            service.state = state
            service._save()
            service.background_wait_seconds = 5

            def fake_drain() -> None:
                started.set()
                self.assertTrue(release.wait(timeout=3))
                with service._state_lock:
                    product = service.state["products"]["B07LCLQ47D"]
                    product["sales_running"] = False
                    product["sales_pending"] = False
                    service._commit_locked()

            service._drain_sales = fake_drain
            service._start_export = lambda: exports.append(1)
            matches = [{"candidate_id": "c1", "similar": True, "confidence": 0.91, "reason": "同款"}]
            first = service.submit_listing_matches(matches, "cursor-grok-4.5-low-fast")
            self.assertTrue(started.wait(timeout=2))
            second = service.submit_listing_matches(matches, "cursor-grok-4.5-low-fast")
            self.assertEqual(first.data["status"], "sales_started")
            self.assertEqual(second.data["status"], "sales_started")
            self.assertEqual(exports, [])
            release.set()
            service._worker.call(lambda: None)
            finished = service.session_status()
            self.assertEqual(exports, [1])
            self.assertEqual(finished.data["status"], "finished")

    def test_submit_does_not_wait_for_the_sheet(self) -> None:
        release = threading.Event()
        with tempfile.TemporaryDirectory() as directory:
            service = SellerAutoService(DelaySettings(), output_dir=directory)
            state = _state()
            state["products"]["B07LCLQ47D"]["judged_ids"] = ["ad1"]
            service.state = state
            service._save()
            service.background_wait_seconds = 5

            def fake_drain() -> None:
                self.assertTrue(release.wait(timeout=2))
                with service._state_lock:
                    product = service.state["products"]["B07LCLQ47D"]
                    product["sales_running"] = False
                    product["sales_pending"] = False
                    service._commit_locked()

            service._drain_sales = fake_drain
            service._start_export = lambda: (_ for _ in ()).throw(AssertionError("提交不该等写表"))
            started_at = time.monotonic()
            payload = service.submit_listing_matches(
                [{"candidate_id": "c1", "similar": True, "confidence": 0.91, "reason": "同款"}],
                "cursor-grok-4.5-low-fast",
            )
            self.assertLess(time.monotonic() - started_at, 1)
            self.assertEqual(payload.data["status"], "sales_started")
            self.assertIn("不要重复提交", payload.data["message"])
            release.set()
            service._worker.call(lambda: None)


if __name__ == "__main__":
    unittest.main()
