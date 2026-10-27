import tempfile
import threading
import unittest

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
            payload = service.submit_listing_matches(
                [{"candidate_id": "c1", "similar": True, "confidence": 0.91, "reason": "同款"}],
                "cursor-grok-4.5-low-fast",
            )
            self.assertEqual(payload.data["status"], "sales_started")
            self.assertIn("session_status", payload.data["message"])
            duplicate = service.submit_listing_matches(
                [{"candidate_id": "c1", "similar": True, "confidence": 0.91, "reason": "同款"}],
                "cursor-grok-4.5-low-fast",
            )
            self.assertEqual(duplicate.data["status"], "already_submitted")
            service._worker.call(lambda: None)
            self.assertTrue(drained.is_set())
            finished = service.session_status()
            self.assertTrue(exported.is_set())
            self.assertEqual(finished.data["status"], "finished")
            self.assertEqual(finished.data["next_step"], "done")
            self.assertIn("不要再调用 export_results", finished.data["message"])

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

            def fake_drain() -> None:
                with service._state_lock:
                    product = service.state["products"]["B07LCLQ47D"]
                    product["sales_running"] = False
                    product["sales_pending"] = False
                    service._commit_locked()

            service._drain_sales = fake_drain
            service._start_export = lambda: None
            service._next_amazon_product = lambda: service._payload(
                {"ok": True, "status": "amazon_product", "product_id": "B0NEXT", "message": "旧提示"}
            )
            payload = service.submit_listing_matches(
                [{"candidate_id": "c1", "similar": True, "confidence": 0.91, "reason": "同款"}],
                "cursor-grok-4.5-low-fast",
            )
            self.assertEqual(payload.data["status"], "sales_started")
            service._worker.call(lambda: None)
            opened = service.session_status()
            self.assertEqual(opened.data["status"], "amazon_product")
            self.assertEqual(opened.data["product_id"], "B0NEXT")
            self.assertIn("不要调用 export_results", opened.data["message"])
            self.assertIn("set_keywords", opened.data["message"])

    def test_lost_listing_batch_is_sent_again(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = SellerAutoService(DelaySettings(), output_dir=directory)
            service.state = _state()
            service._ensure_browser = lambda: None
            payload = service._next_listing_batch()
            self.assertEqual(payload.data["candidates"][0]["candidate_id"], "c1")
            self.assertEqual(service.state["products"]["B07LCLQ47D"]["listing"]["next_index"], 1)

    def test_lost_ad_batch_is_sent_again(self) -> None:
        product = _state()["products"]["B07LCLQ47D"]
        match, batch = _replay_ad_batch(product)
        self.assertEqual(match["url"].endswith("MLA-1"), True)
        self.assertEqual(batch[0]["candidate_id"], "ad1")


if __name__ == "__main__":
    unittest.main()
