import unittest

from seller_auto.mercadolibre import is_wanted_ad_title, note_ad_block_count


class AdBlockTests(unittest.TestCase):
    def test_blocks_settle_only_after_two_unchanged_counts(self) -> None:
        stable, settled = note_ad_block_count(-1, 0, 0)
        self.assertEqual((stable, settled), (0, False))
        stable, settled = note_ad_block_count(0, 3, stable)
        self.assertEqual((stable, settled), (0, False))
        stable, settled = note_ad_block_count(3, 3, stable)
        self.assertEqual((stable, settled), (1, False))
        stable, settled = note_ad_block_count(3, 3, stable)
        self.assertEqual((stable, settled), (2, True))

    def test_only_the_three_named_carousels_match(self) -> None:
        self.assertTrue(is_wanted_ad_title("Productos relacionados"))
        self.assertTrue(is_wanted_ad_title("  Productos   relacionados "))
        self.assertTrue(is_wanted_ad_title("Quienes vieron este producto también compraron"))
        self.assertFalse(is_wanted_ad_title("Ofertas del día"))
        self.assertFalse(is_wanted_ad_title("Productos promocionados"))

    def test_new_block_resets_the_stable_count(self) -> None:
        stable, settled = note_ad_block_count(2, 2, 1)
        self.assertEqual(stable, 2)
        stable, settled = note_ad_block_count(2, 4, stable)
        self.assertEqual((stable, settled), (0, False))


if __name__ == "__main__":
    unittest.main()
