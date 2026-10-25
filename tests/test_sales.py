import unittest

from seller_auto.sales import has_sales, sales_action


class SalesTests(unittest.TestCase):
    def test_plural_and_singular_count_as_sales(self) -> None:
        self.assertTrue(has_sales("Nuevo | +1000 vendidos"))
        self.assertTrue(has_sales("1 vendido"))
        self.assertTrue(has_sales("VENDIDOS"))

    def test_missing_word_means_no_sales(self) -> None:
        self.assertFalse(has_sales("Nuevo"))
        self.assertFalse(has_sales("4.8 (123)"))
        self.assertFalse(has_sales(""))

    def test_card_text_decides_whether_to_open_the_detail(self) -> None:
        self.assertEqual(sales_action("search", "+5 vendidos"), "record")
        self.assertEqual(sales_action("search", "Nuevo"), "open")
        self.assertEqual(sales_action("search", ""), "open")
        self.assertEqual(sales_action("recommendation", "1 vendido"), "record")
        self.assertEqual(sales_action("recommendation", ""), "open")
        self.assertEqual(sales_action("search", None), "open")


if __name__ == "__main__":
    unittest.main()
