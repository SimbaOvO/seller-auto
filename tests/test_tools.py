import json
import tempfile
import unittest
from pathlib import Path

from mcp.server.mcpserver.utilities.func_metadata import _convert_to_content

from seller_auto.browser import pages_to_close
from seller_auto.mcp_server import INSTRUCTIONS, _blocks, build_server
from seller_auto.pacing import DelaySettings
from seller_auto.service import SellerAutoService, ToolPayload
from seller_auto.storage import atomic_write_json, build_export, find_state_by_session, load_json


class ToolContractTests(unittest.TestCase):
    def test_registered_tools_and_match_input(self) -> None:
        server = build_server(SellerAutoService(DelaySettings(), output_dir=tempfile.mkdtemp()))
        tools = {tool.name: tool for tool in server._tool_manager.list_tools()}
        self.assertEqual(
            set(tools),
            {
                "open_store",
                "next_amazon_product",
                "set_keywords",
                "next_listing_batch",
                "submit_listing_matches",
                "next_ad_batch",
                "submit_ad_matches",
                "session_status",
                "resume_session",
                "export_results",
            },
        )
        schema = tools["submit_listing_matches"].parameters
        self.assertIn("matches", schema["properties"])
        self.assertIn("model_name", schema["properties"])
        self.assertIn("matches", schema["required"])
        self.assertNotIn("model_name", schema["required"])
        open_store = tools["open_store"].parameters
        self.assertIn("seller_id", open_store["properties"])
        self.assertNotIn("url", open_store["properties"])
        self.assertIn("seller_id", open_store["required"])
        self.assertIn("open_store(seller_id, product)", INSTRUCTIONS)
        self.assertIn("颜色", INSTRUCTIONS)
        self.assertIn("高度相似", INSTRUCTIONS)
        self.assertIn(INSTRUCTIONS, server.instructions or "")

    def test_image_blocks_stay_images(self) -> None:
        from PIL import Image
        import io

        buffer = io.BytesIO()
        Image.new("RGB", (1200, 10), "white").save(buffer, format="JPEG")
        content = _convert_to_content(
            _blocks(ToolPayload({"ok": True, "status": "listing_batch"}, [("列表图 candidate_id=a", buffer.getvalue())]))
        )
        # _blocks already wraps jpeg bytes in Image; convert walks that list.
        kinds = [item.type for item in content]
        self.assertEqual(kinds[0], "text")
        self.assertIn("image", kinds)
        payload = json.loads(content[0].text)
        self.assertEqual(payload["status"], "listing_batch")

    def test_missing_session_is_an_error_payload(self) -> None:
        service = SellerAutoService(DelaySettings(), output_dir=tempfile.mkdtemp())
        payload = service.session_status()
        self.assertFalse(payload.data["ok"])
        self.assertEqual(payload.data["status"], "error")


class StorageTests(unittest.TestCase):
    def test_state_roundtrip_and_export_shape(self) -> None:
        state = {
            "session_id": "sess-1",
            "seller_id": "A1A10HFD4KEZW5",
            "store_url": "https://www.amazon.com.mx/s?me=A1A10HFD4KEZW5&marketplaceID=A1A10HFD4KEZW5",
            "product_order": ["B0C1234567"],
            "excluded": [{"product_id": "B0FOOD1234", "reason": "食品", "url": "https://www.amazon.com.mx/dp/B0FOOD1234", "title": "Yerba"}],
            "products": {
                "B0C1234567": {
                    "url": "https://www.amazon.com.mx/dp/B0C1234567",
                    "title": "Funda",
                    "brand": "ACER",
                    "keywords": "funda para burro de planchar",
                    "image_path": "images/B0C1234567.jpg",
                    "ml_search_url": "https://listado.mercadolibre.com.ar/funda",
                    "listing": {
                        "candidates": [
                            {
                                "candidate_id": "low",
                                "url": "https://www.mercadolibre.com.ar/p/MLA0",
                                "title": "Otra",
                                "similarity": 0.81,
                                "similar": True,
                            },
                            {
                                "candidate_id": "high",
                                "url": "https://www.mercadolibre.com.ar/p/MLA9?tracking=1#x",
                                "title": "Mejor",
                                "similarity": 0.97,
                                "similar": True,
                            },
                        ]
                    },
                    "matches": [
                        {
                            "candidate_id": "sold-low",
                            "url": "https://www.mercadolibre.com.ar/p/MLA1?tracking=1#x",
                            "sales_text": "+25 vendidos",
                            "confidence": 0.91,
                            "reason": "外形一致",
                            "source": "search",
                            "parent_url": None,
                            "image_path": "images/MLA1.jpg",
                            "model_name": "client-model",
                            "title": "Funda grande",
                        },
                        {
                            "candidate_id": "sold-high",
                            "url": "https://www.mercadolibre.com.ar/p/MLA2",
                            "sales_text": "+100 vendidos",
                            "confidence": 0.96,
                            "reason": "外形更接近",
                            "source": "search",
                            "parent_url": None,
                            "image_path": "images/MLA2.jpg",
                            "model_name": "client-model",
                            "title": "Funda mejor",
                        },
                    ],
                }
            },
        }
        exported = build_export(state)
        self.assertEqual(exported["model_names"], ["client-model"])
        self.assertEqual(exported["products"][0]["mercadolibre_matches"][0]["source"], "search")
        self.assertEqual(exported["products"][0]["mercadolibre_matches"][0]["candidate_id"], "sold-high")
        self.assertEqual(exported["products"][0]["mercadolibre_matches"][0]["similarity"], 0.96)
        self.assertEqual(exported["products"][0]["top_links"][0]["candidate_id"], "sold-high")
        self.assertEqual([item["candidate_id"] for item in exported["products"][0]["top_links"]], ["sold-high", "sold-low"])
        self.assertNotIn("tracking", exported["products"][0]["top_links"][1]["url"])
        self.assertNotIn("high", [item["candidate_id"] for item in exported["products"][0]["top_links"]])
        self.assertEqual(exported["excluded"][0]["reason"], "食品")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "A1A10HFD4KEZW5" / "state.json"
            atomic_write_json(path, state)
            self.assertEqual(load_json(path)["session_id"], "sess-1")
            self.assertEqual(find_state_by_session(root, "sess-1"), path)
            self.assertIsNone(find_state_by_session(root, "missing"))


class TabTests(unittest.TestCase):
    def test_only_list_and_detail_are_kept(self) -> None:
        self.assertEqual(pages_to_close(["list", "detail", "extra", "another"], ["list", "detail"]), ["extra", "another"])
        self.assertEqual(pages_to_close(["list"], ["list"]), [])


if __name__ == "__main__":
    unittest.main()
