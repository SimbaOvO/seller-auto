import tempfile
import unittest
from pathlib import Path
from unittest import mock

from seller_auto.package_backfill import backfill_packages, missing_package_ids
from seller_auto.pacing import DelaySettings


def _state() -> dict:
    return {
        "product_order": ["B0OLD", "B0PARTIAL", "B0D2228M83", "B0SKIP", "B0GWHDBJFL"],
        "products": {
            "B0OLD": {
                "product_id": "B0OLD",
                "chinese_name": "旧商品",
                "keywords": "old",
                "package_size": {"length": "10", "width": "8", "height": "6", "weight": "100", "weight_unit": "g"},
                "matches": [{"url": "https://www.mercadolibre.com.ar/p/MLA1", "sales_text": "1 vendido"}],
            },
            "B0PARTIAL": {
                "product_id": "B0PARTIAL",
                "chinese_name": "尾椎痔疮坐垫",
                "keywords": "cojin para coxis",
                "package_size": {"weight": "940", "weight_unit": "g"},
                "source_price": {"average": "28.78"},
                "workbook_written": True,
                "matches": [{"url": "https://www.mercadolibre.com.ar/p/MLA4", "sales_text": "1 vendido", "price": 8000}],
            },
            "B0D2228M83": {
                "product_id": "B0D2228M83",
                "chinese_name": "亚克力阶梯展架",
                "keywords": "elevadores acrilicos para exhibicion",
                "package_size": None,
                "workbook_written": True,
                "matches": [{"url": "https://www.mercadolibre.com.ar/p/MLA2", "sales_text": "10 vendidos", "price": 12000}],
            },
            "B0SKIP": {
                "product_id": "B0SKIP",
                "chinese_name": "打印机置物架",
                "keywords": "estante para impresora",
                "package_size": None,
                "matches": [],
            },
            "B0GWHDBJFL": {
                "product_id": "B0GWHDBJFL",
                "chinese_name": "迷你台锯套装",
                "keywords": "sierra de mesa mini",
                "package_size": {"length": "10", "width": "8", "height": "6", "weight": "200", "weight_unit": "g"},
                "matches": [{"url": "https://www.mercadolibre.com.ar/p/MLA3", "sales_text": "2 vendidos"}],
            },
        },
    }


class PackageBackfillTests(unittest.TestCase):
    def test_missing_ids_include_partial_packages(self) -> None:
        self.assertEqual(missing_package_ids(_state()), ["B0PARTIAL", "B0D2228M83"])

    def test_backfill_writes_package_and_orders(self) -> None:
        state = _state()
        written = []

        def query(product_id: str):
            self.assertIn(product_id, {"B0PARTIAL", "B0D2228M83"})
            return (
                {"length": "21", "width": "15", "height": "8", "unit": "cm", "weight": "400", "weight_unit": "g"},
                {"average": "9.50"},
            )

        def quote(jobs):
            self.assertEqual([job["product_id"] for job in jobs], ["B0PARTIAL", "B0D2228M83"])
            return [{"orders": 4}, {"orders": 19}]

        def write_rows(path, products):
            written.append((path, [item["product_id"] for item in products]))
            return len(products)

        with tempfile.TemporaryDirectory() as directory:
            workbook = Path(directory) / "test.xlsx"
            workbook.write_bytes(b"")
            result = backfill_packages(
                state,
                workbook_path=workbook,
                settings=DelaySettings(),
                profile_dir=Path(directory) / "profile",
                query=query,
                quote=quote,
                write_rows=write_rows,
            )
        self.assertEqual(result["ids"], ["B0PARTIAL", "B0D2228M83"])
        self.assertEqual(result["updated"], 2)
        self.assertEqual(result["quoted"], 2)
        partial = state["products"]["B0PARTIAL"]["package_size"]
        self.assertEqual(partial["weight"], "940")
        self.assertEqual(partial["length"], "21")
        self.assertEqual(state["products"]["B0PARTIAL"]["source_price"]["average"], "28.78")
        self.assertEqual(state["products"]["B0PARTIAL"]["market_orders"], 4)
        self.assertEqual(state["products"]["B0D2228M83"]["package_size"]["length"], "21")
        self.assertEqual(state["products"]["B0D2228M83"]["source_price"]["average"], "9.50")
        self.assertEqual(state["products"]["B0D2228M83"]["market_orders"], 19)
        self.assertEqual(written[0][1], ["B0PARTIAL", "B0D2228M83"])

    def test_backfill_browser_does_not_use_the_live_enrich_profile(self) -> None:
        state = _state()
        opened: list[Path] = []

        class FakeBrowser:
            def __init__(self, settings, profile_dir, **kwargs):
                opened.append(Path(profile_dir))

            def start(self) -> None:
                return None

            def close(self) -> None:
                return None

            def close_detail(self) -> None:
                return None

        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "profile"
            enrich = Path(str(profile) + "-enrich")
            (enrich / "Default").mkdir(parents=True)
            (enrich / "Default" / "Cookies").write_bytes(b"cookie")
            (enrich / "SingletonLock").symlink_to(f"host-{__import__('os').getpid()}")
            with (
                mock.patch("seller_auto.package_backfill.ArgentinaBrowser", FakeBrowser),
                mock.patch("seller_auto.package_backfill.open_competitor", return_value=(None, None)),
                mock.patch("seller_auto.package_backfill.collect_market_quotes", return_value=[]),
            ):
                backfill_packages(
                    state,
                    workbook_path=Path(directory) / "test.xlsx",
                    settings=DelaySettings(),
                    profile_dir=profile,
                    ids=["B0D2228M83"],
                    quote=lambda jobs: [{"orders": None}],
                )
            self.assertEqual(len(opened), 1)
            self.assertEqual(opened[0].name, "profile-enrich-backfill")
            self.assertNotEqual(opened[0], enrich)
            self.assertTrue((enrich / "SingletonLock").is_symlink())
            self.assertFalse((opened[0] / "SingletonLock").exists())
