import threading
import tempfile
import time
import unittest
from pathlib import Path

from seller_auto.pacing import DelaySettings
from seller_auto.service import SellerAutoService, _new_state
from openpyxl import Workbook

from seller_auto.workbook import TEMPLATE_SHEET


def _product_state() -> dict:
    state = _new_state("sess", "A1A10HFD4KEZW5", "https://www.amazon.com.mx/s?me=A1A10HFD4KEZW5")
    state["single_product"] = "B07LCLQ47D"
    state["run_id"] = "run-1"
    state["product_order"] = ["B07LCLQ47D"]
    state["products"]["B07LCLQ47D"] = {
        "product_id": "B07LCLQ47D",
        "url": "https://www.amazon.com.mx/dp/B07LCLQ47D",
        "title": "Cubierta de Hendidura de Estufa de Silicona",
        "brand": "Yolispa",
        "keywords": "cubiertas hendidura para estufa",
        "chinese_name": "硅胶灶台缝隙盖",
        "package_size": None,
        "source_price": None,
        "listing": {"page1_done": True, "page2_done": True, "candidates": [], "next_index": 0},
        "matches": [],
    }
    return state


class EnrichmentTests(unittest.TestCase):
    def test_schedule_returns_before_lookup_finishes(self) -> None:
        release = threading.Event()
        with tempfile.TemporaryDirectory() as directory:
            service = SellerAutoService(DelaySettings(), output_dir=directory)
            service.state = _product_state()

            def fake_run(product_id: str, image_url: str, event: threading.Event) -> None:
                release.wait(timeout=2)
                event.set()

            service._run_enrichment = fake_run
            started = time.monotonic()
            service._schedule_enrichment("B07LCLQ47D", "https://example.com/img.jpg")
            self.assertLess(time.monotonic() - started, 0.2)
            release.set()
            service._wait_enrichment()

    def test_export_waits_for_background_sellersprite(self) -> None:
        started = threading.Event()
        release = threading.Event()
        with tempfile.TemporaryDirectory() as directory:
            workbook_path = Path(directory) / "test.xlsx"
            workbook = Workbook()
            sheet = workbook.active
            sheet.title = TEMPLATE_SHEET
            sheet.cell(1, 1, "名称")
            sheet.cell(1, 2, "SKU")
            workbook.save(workbook_path)
            service = SellerAutoService(DelaySettings(), output_dir=directory, workbook_path=str(workbook_path))
            service.state = _product_state()
            service._save()

            def fake_run(product_id: str, image_url: str, event: threading.Event) -> None:
                started.set()
                self.assertTrue(release.wait(timeout=2))
                with service._state_lock:
                    service.state["products"][product_id]["package_size"] = {
                        "length": "23.6",
                        "width": "16.3",
                        "height": "7.1",
                        "unit": "cm",
                        "weight": "454",
                        "weight_unit": "g",
                    }
                    service.state["products"][product_id]["source_price"] = {"average": "12.35"}
                    service._write_state()
                event.set()

            service._run_enrichment = fake_run
            service._schedule_enrichment("B07LCLQ47D", "https://example.com/img.jpg")
            self.assertTrue(started.wait(timeout=2))

            payload = service.export_results()
            self.assertEqual(payload.data["status"], "exporting")
            release.set()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not (service.state.get("export_result") or {}).get("ok"):
                time.sleep(0.05)
            result = service.state.get("export_result") or {}
            self.assertTrue(result.get("ok"))
            self.assertTrue(Path(result["workbook"]).is_file())
            self.assertEqual(service.state["products"]["B07LCLQ47D"]["package_size"]["weight"], "454")

    def test_source_price_comes_from_the_sellersprite_row(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = SellerAutoService(DelaySettings(), output_dir=directory)
            service.state = _product_state()

            def fake_open(browser: object, asin: str) -> tuple[dict[str, str], str]:
                self.assertEqual(asin, "B07LCLQ47D")
                return {"weight": "260", "weight_unit": "g"}, "lookup-page"

            def fake_source(browser: object, page: object, asin: str) -> dict[str, str]:
                self.assertEqual(page, "lookup-page")
                self.assertEqual(asin, "B07LCLQ47D")
                return {"average": "8.00", "currency": "CNY", "count": "1"}

            service._ensure_enrich_browser = lambda: object()
            service._open_competitor = fake_open
            service._read_1688_from_row = fake_source
            event = threading.Event()
            service._run_enrichment("B07LCLQ47D", "https://example.com/img.jpg", event)
            self.assertTrue(event.is_set())
            product = service.state["products"]["B07LCLQ47D"]
            self.assertEqual(product["package_size"]["weight"], "260")
            self.assertEqual(product["source_price"]["average"], "8.00")


if __name__ == "__main__":
    unittest.main()
