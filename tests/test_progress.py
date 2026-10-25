import unittest

from seller_auto.progress import next_required_step


def _state(**overrides):
    state = {
        "amazon_done": False,
        "current_product_id": None,
        "products": {},
    }
    state.update(overrides)
    return state


class ProgressTests(unittest.TestCase):
    def test_store_starts_at_the_next_amazon_product(self) -> None:
        self.assertEqual(next_required_step(_state()), "next_amazon_product")

    def test_one_finished_product_does_not_end_the_store(self) -> None:
        product = {
            "keywords": "funda",
            "listing": {"page2_done": True, "next_index": 1, "candidates": [{"candidate_id": "c1"}]},
            "judged_ids": ["c1"],
            "matches": [{"source": "search", "ads_complete": True, "ad_candidates": []}],
        }
        state = _state(current_product_id="B0", products={"B0": product})
        self.assertEqual(next_required_step(state), "next_amazon_product")

    def test_single_product_exports_after_its_own_pipeline(self) -> None:
        product = {
            "keywords": "funda",
            "listing": {"page2_done": True, "next_index": 1, "candidates": [{"candidate_id": "c1"}]},
            "judged_ids": ["c1"],
            "matches": [{"source": "search", "ads_complete": True, "ad_candidates": []}],
        }
        state = _state(current_product_id="B0D25PTTTQ", single_product="B0D25PTTTQ", products={"B0D25PTTTQ": product})
        self.assertEqual(next_required_step(state), "export_results")

    def test_delivered_ad_images_are_judged_before_the_next_batch(self) -> None:
        product = {
            "keywords": "funda",
            "listing": {"page2_done": True, "next_index": 1, "candidates": [{"candidate_id": "c1"}]},
            "judged_ids": ["c1"],
            "matches": [
                {
                    "source": "search",
                    "ads_scraped": True,
                    "ads_complete": False,
                    "ad_next_index": 1,
                    "ad_candidates": [{"candidate_id": "ad1"}, {"candidate_id": "ad2"}],
                }
            ],
        }
        state = _state(current_product_id="B0", products={"B0": product})
        self.assertEqual(next_required_step(state), "submit_ad_matches")

    def test_finished_store_exports(self) -> None:
        self.assertEqual(next_required_step(_state(amazon_done=True)), "export_results")


if __name__ == "__main__":
    unittest.main()
