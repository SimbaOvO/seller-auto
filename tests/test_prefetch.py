import tempfile
import threading
import unittest
from types import SimpleNamespace

from seller_auto.pacing import DelaySettings
from seller_auto.progress import next_required_step
from seller_auto.service import SellerAutoService, _new_state


def _state() -> dict:
    state = _new_state("sess", "A1A10HFD4KEZW5", "https://www.amazon.com.mx/s?me=A1A10HFD4KEZW5")
    state["single_product"] = "B07LCLQ47D"
    state["run_id"] = "run-1"
    state["current_product_id"] = "B07LCLQ47D"
    state["product_order"] = ["B07LCLQ47D"]
    state["products"]["B07LCLQ47D"] = {
        "product_id": "B07LCLQ47D",
        "url": "https://www.amazon.com.mx/dp/B07LCLQ47D",
        "title": "Cubierta de Hendidura de Estufa de Silicona",
        "brand": "Yolispa",
        "keywords": "cubiertas para estufa",
        "chinese_name": "",
        "ml_search_url": "https://listado.mercadolibre.com.ar/cubiertas-para-estufa",
        "image_path": "",
        "package_size": None,
        "source_price": None,
        "listing": {
            "page1_done": True,
            "page2_done": False,
            "candidates": [
                {
                    "candidate_id": "c1",
                    "url": "https://articulo.mercadolibre.com.ar/MLA-1",
                    "title": "Cubierta para estufa",
                    "image_path": "kept.jpg",
                    "key": "MLA1",
                }
            ],
            "next_index": 0,
        },
        "judged_ids": [],
        "checked_keys": [],
        "matches": [],
    }
    return state


class _Browser:
    def __init__(self) -> None:
        self.list_page = SimpleNamespace(url="")
        self.ml_cooldown = SimpleNamespace(acquire=lambda: 0)

    def close_detail(self) -> None:
        return None

    def goto(self, page: SimpleNamespace, url: str) -> None:
        page.url = url

    def ensure_clear(self, page: SimpleNamespace, site: str) -> None:
        return None

    def pause_ml_scroll(self) -> None:
        return None


class PrefetchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.service = SellerAutoService(DelaySettings(), output_dir=self.directory.name)
        self.service.state = _state()
        self.service._ensure_browser = lambda: _Browser()

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_set_keywords_returns_the_first_listing_batch(self) -> None:
        product = self.service.state["products"]["B07LCLQ47D"]
        product["keywords"] = ""
        product["ml_search_url"] = ""

        def fake_batch() -> object:
            return self.service._payload(
                {
                    "ok": True,
                    "status": "listing_batch",
                    "product_id": "B07LCLQ47D",
                    "candidates": [{"candidate_id": "c1", "url": "https://example.com", "title": "Cubierta para estufa"}],
                    "message": "old",
                }
            )

        self.service._next_listing_batch = fake_batch
        payload = self.service._set_keywords("B07LCLQ47D", "cubiertas para estufa", "灶台缝隙盖")
        self.assertEqual(payload.data["status"], "listing_batch")
        self.assertIn("第一批", payload.data["message"])
        self.assertIn("submit_listing_matches", payload.data["message"])
        self.assertEqual(product["chinese_name"], "灶台缝隙盖")
        self.assertIn("cubiertas-para-estufa", payload.data["ml_search_url"])

    def test_listing_prefetch_runs_once_while_the_batch_stays_unjudged(self) -> None:
        product = self.service.state["products"]["B07LCLQ47D"]
        calls: list[str] = []
        started = threading.Event()
        release = threading.Event()

        def fake_load(current: dict) -> None:
            calls.append(current["product_id"])
            started.set()
            self.assertTrue(release.wait(timeout=2))
            current["listing"]["candidates"].append(
                {
                    "candidate_id": "c2",
                    "url": "https://articulo.mercadolibre.com.ar/MLA-2",
                    "title": "Otra cubierta de silicona",
                    "image_path": "kept.jpg",
                    "key": "MLA2",
                }
            )
            current["listing"]["page2_done"] = True
            return None

        self.service._load_listing_page2 = fake_load
        payload = self.service._next_listing_batch()
        self.assertEqual(payload.data["status"], "listing_batch")
        self.assertEqual(payload.data["candidates"][0]["candidate_id"], "c1")
        self.assertEqual(payload.data["next_step"], "submit_listing_matches")
        self.assertTrue(started.wait(timeout=2))
        self.service._schedule_listing_prefetch(product)
        release.set()
        self._wait_worker()
        self.assertFalse(product["listing"].get("page2_prefetching"))
        self.assertEqual(calls, ["B07LCLQ47D"])
        self.assertTrue(product["listing"]["page2_done"])
        self.assertEqual(next_required_step(self.service.state), "submit_listing_matches")

    def test_ready_page2_does_not_navigate_again(self) -> None:
        listing = self.service.state["products"]["B07LCLQ47D"]["listing"]
        listing["page2_done"] = True
        listing["next_index"] = 1
        listing["candidates"].append(
            {
                "candidate_id": "c2",
                "url": "https://articulo.mercadolibre.com.ar/MLA-2",
                "title": "Otra cubierta de silicona",
                "image_path": "kept.jpg",
                "key": "MLA2",
            }
        )

        def fail_load(product: dict) -> None:
            raise AssertionError("第二页已经在手里，不应再打开")

        self.service._load_listing_page2 = fail_load
        payload = self.service._next_listing_batch()
        self.assertEqual(payload.data["candidates"][0]["candidate_id"], "c2")

    def test_exhausted_page1_loads_page2_before_returning(self) -> None:
        listing = self.service.state["products"]["B07LCLQ47D"]["listing"]
        listing["next_index"] = 1

        def fake_load(product: dict) -> None:
            product["listing"]["candidates"].append(
                {
                    "candidate_id": "c2",
                    "url": "https://articulo.mercadolibre.com.ar/MLA-2",
                    "title": "Otra cubierta de silicona",
                    "image_path": "kept.jpg",
                    "key": "MLA2",
                }
            )
            product["listing"]["page2_done"] = True
            return None

        self.service._load_listing_page2 = fake_load
        payload = self.service._next_listing_batch()
        self.assertEqual(payload.data["status"], "listing_batch")
        self.assertEqual(payload.data["candidates"][0]["candidate_id"], "c2")

    def test_ad_prefetch_leaves_the_batch_for_the_next_call(self) -> None:
        product = self.service.state["products"]["B07LCLQ47D"]
        product["listing"]["page2_done"] = True
        product["listing"]["next_index"] = 1
        product["matches"] = [
            {
                "source": "search",
                "url": "https://articulo.mercadolibre.com.ar/MLA-1",
                "key": "MLA1",
                "ads_scraped": True,
                "ads_complete": True,
                "ad_candidates": [],
                "ad_next_index": 0,
                "image_path": "kept.jpg",
            },
            {
                "source": "search",
                "url": "https://articulo.mercadolibre.com.ar/MLA-9",
                "key": "MLA9",
                "ads_scraped": False,
                "ads_complete": False,
                "ad_candidates": [],
                "ad_next_index": 0,
                "image_path": "kept.jpg",
            },
        ]
        calls: list[str] = []
        started = threading.Event()
        release = threading.Event()

        def fake_scrape(current: dict, match: dict) -> None:
            calls.append(match["url"])
            started.set()
            self.assertTrue(release.wait(timeout=2))
            match["ad_candidates"] = [
                {
                    "candidate_id": "ad1",
                    "url": "https://articulo.mercadolibre.com.ar/MLA-8",
                    "title": "Ad",
                    "image_path": "kept.jpg",
                    "key": "MLA8",
                }
            ]
            match["ads_scraped"] = True
            match["ad_next_index"] = 0
            return None

        self.service._scrape_ad_match = fake_scrape
        self.service._schedule_ad_prefetch(product)
        self.assertTrue(started.wait(timeout=2))
        self.service._schedule_ad_prefetch(product)
        release.set()
        self._wait_worker()
        self.assertFalse(product.get("ad_prefetching"))
        self.assertEqual(calls, ["https://articulo.mercadolibre.com.ar/MLA-9"])
        pending = product["matches"][1]
        self.assertEqual(pending["ad_next_index"], 0)
        self.assertTrue(pending["ads_scraped"])

        def fail_scrape(current: dict, match: dict) -> None:
            raise AssertionError("广告图已经预取，不应再打开详情")

        self.service._scrape_ad_match = fail_scrape
        payload = self.service._next_ad_batch("B07LCLQ47D")
        self.assertEqual(payload.data["status"], "ad_batch")
        self.assertEqual(payload.data["candidates"][0]["candidate_id"], "ad1")

    def test_listing_skips_obvious_title_mismatch_before_images(self) -> None:
        listing = self.service.state["products"]["B07LCLQ47D"]["listing"]
        listing["page2_done"] = True
        listing["candidates"] = [
            {
                "candidate_id": "c-hood",
                "url": "https://articulo.mercadolibre.com.ar/MLA-hood",
                "title": "Campana Purif. D.c Cristal 60cm Motor Acero C/luz",
                "image_path": "",
                "key": "MLAHOOD",
            },
            {
                "candidate_id": "c-keep",
                "url": "https://articulo.mercadolibre.com.ar/MLA-keep",
                "title": "Cubierta flexible para estufa",
                "image_path": "kept.jpg",
                "key": "MLAKEEP",
            },
        ]
        downloaded: list[str] = []

        def fake_files(candidates: list, page_url: str) -> None:
            downloaded.extend(item["candidate_id"] for item in candidates)

        self.service._ensure_candidate_files = fake_files
        payload = self.service._next_listing_batch()
        product = self.service.state["products"]["B07LCLQ47D"]
        self.assertEqual([item["candidate_id"] for item in payload.data["candidates"]], ["c-keep"])
        self.assertEqual(payload.data["amazon_title"], "Cubierta de Hendidura de Estufa de Silicona")
        self.assertEqual([item["candidate_id"] for item in payload.data["title_dropped"]], ["c-hood"])
        self.assertEqual(downloaded, ["c-keep"])
        self.assertIn("c-hood", product["judged_ids"])
        self.assertFalse(product["listing"]["candidates"][0]["similar"])
        self.assertEqual(product["listing"]["candidates"][0]["reason"], "标题明显不是同一商品")

    def test_ad_skips_obvious_title_mismatch_before_images(self) -> None:
        product = self.service.state["products"]["B07LCLQ47D"]
        product["listing"]["page2_done"] = True
        product["listing"]["next_index"] = 1
        product["judged_ids"] = ["c1"]
        product["matches"] = [
            {
                "source": "search",
                "url": "https://articulo.mercadolibre.com.ar/MLA-1",
                "key": "MLA1",
                "ads_scraped": True,
                "ads_complete": False,
                "ad_next_index": 0,
                "image_path": "kept.jpg",
                "ad_candidates": [
                    {
                        "candidate_id": "ad-phone",
                        "url": "https://articulo.mercadolibre.com.ar/MLA-phone",
                        "title": "Soporte para celular plegable",
                        "image_path": "",
                        "key": "MLAPHONE",
                    },
                    {
                        "candidate_id": "ad-keep",
                        "url": "https://articulo.mercadolibre.com.ar/MLA-keep",
                        "title": "Cubierta de silicona para estufa",
                        "image_path": "kept.jpg",
                        "key": "MLAKEEP",
                    },
                ],
            }
        ]
        downloaded: list[str] = []

        def fake_files(candidates: list, page_url: str) -> None:
            downloaded.extend(item["candidate_id"] for item in candidates)

        self.service._ensure_candidate_files = fake_files
        payload = self.service._next_ad_batch("B07LCLQ47D")
        self.assertEqual(payload.data["status"], "ad_batch")
        self.assertEqual([item["candidate_id"] for item in payload.data["candidates"]], ["ad-keep"])
        self.assertEqual([item["candidate_id"] for item in payload.data["title_dropped"]], ["ad-phone"])
        self.assertEqual(downloaded, ["ad-keep"])
        self.assertIn("ad-phone", product["judged_ids"])
        self.assertTrue(product["matches"][0]["ads_complete"])

    def test_clear_prefetch_flags(self) -> None:
        product = self.service.state["products"]["B07LCLQ47D"]
        product["listing"]["page2_prefetching"] = True
        product["ad_prefetching"] = True
        self.service._clear_prefetch_flags()
        self.assertNotIn("page2_prefetching", product["listing"])
        self.assertNotIn("ad_prefetching", product)

    def _wait_worker(self) -> None:
        done = threading.Event()

        def mark() -> None:
            done.set()

        self.service._worker.submit(mark)
        self.assertTrue(done.wait(timeout=2))


if __name__ == "__main__":
    unittest.main()
