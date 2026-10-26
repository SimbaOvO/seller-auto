import unittest

from seller_auto.matches import (
    MatchInputError,
    filter_matches,
    normalize_match,
    title_obviously_different,
)


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

    def test_obvious_title_mismatch_is_dropped(self) -> None:
        amazon = "Cubierta de Hendidura de Estufa de Silicona"
        self.assertTrue(title_obviously_different(amazon, "Campana Purif. D.c Cristal 60cm Motor Acero C/luz"))
        self.assertTrue(title_obviously_different(amazon, "Soporte para celular plegable"))

    def test_related_or_uncertain_titles_stay_for_images(self) -> None:
        amazon = "Cubierta de Hendidura de Estufa de Silicona"
        self.assertFalse(title_obviously_different(amazon, "2 Cubiertas Flexibles Para Huecos Para Estufas De Cocina"))
        self.assertFalse(title_obviously_different(amazon, "Cubre ranuras para estufa"))
        self.assertFalse(title_obviously_different(amazon, "Tapa de silicona para cocina"))
        self.assertFalse(title_obviously_different(amazon, "Tapa"))
        self.assertFalse(title_obviously_different(amazon, ""))
        self.assertFalse(title_obviously_different("", "Campana extractora"))


if __name__ == "__main__":
    unittest.main()
