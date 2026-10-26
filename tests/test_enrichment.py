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

            finished: list = []

            def do_export() -> None:
                finished.append(service.export_results())

            thread = threading.Thread(target=do_export)
            thread.start()
            time.sleep(0.15)
            self.assertFalse(finished)
            release.set()
            thread.join(timeout=5)
            self.assertTrue(thread.is_alive() is False)
            self.assertEqual(len(finished), 1)
            payload = finished[0]
            self.assertTrue(payload.data["ok"])
            self.assertTrue(Path(payload.data["workbook"]).is_file())
            self.assertEqual(service.state["products"]["B07LCLQ47D"]["package_size"]["weight"], "454")


if __name__ == "__main__":
    unittest.main()
