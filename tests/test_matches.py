import unittest

from seller_auto.matches import MatchInputError, filter_matches, normalize_match


class MatchTests(unittest.TestCase):
    def test_threshold_keeps_only_similar_high_confidence(self) -> None:
        kept, dropped = filter_matches(
            [
                {"candidate_id": "a", "similar": True, "confidence": 0.8, "reason": "同外形"},
                {"candidate_id": "b", "similar": True, "confidence": 0.79, "reason": "偏低"},
                {"candidate_id": "c", "similar": False, "confidence": 1, "reason": "只是同类"},
                {"candidate_id": "d", "similar": "true", "confidence": "0.91", "reason": ""},
            ],
            0.8,
        )
        self.assertEqual([item["candidate_id"] for item in kept], ["a", "d"])
        self.assertEqual([item["candidate_id"] for item in dropped], ["b", "c"])

    def test_missing_candidate_id_is_rejected(self) -> None:
        with self.assertRaises(MatchInputError):
            normalize_match({"similar": True, "confidence": 1})

    def test_threshold_out_of_range(self) -> None:
        with self.assertRaises(MatchInputError):
            filter_matches([], 1.2)


if __name__ == "__main__":
    unittest.main()
