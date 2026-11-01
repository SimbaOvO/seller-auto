import unittest

from seller_auto.sales import has_sales, pick_sales_line, sales_action, sales_count


class SalesTests(unittest.TestCase):
    def test_plural_and_singular_count_as_sales(self) -> None:
        self.assertTrue(has_sales("Nuevo | +1000 vendidos"))
        self.assertTrue(has_sales("1 vendido"))
        self.assertTrue(has_sales("VENDIDOS"))

    def test_missing_word_means_no_sales(self) -> None:
        self.assertFalse(has_sales("Nuevo"))
        self.assertFalse(has_sales("4.8 (123)"))
        self.assertFalse(has_sales(""))

    def test_sales_count_reads_argentine_thousands(self) -> None:
        self.assertEqual(sales_count("+500 vendidos"), 500)
        self.assertEqual(sales_count("Nuevo | +1.000 vendidos"), 1000)
        self.assertEqual(sales_count("1 vendido"), 1)
        self.assertEqual(sales_count("Nuevo"), 0)

    def test_subtitle_with_one_sale_is_kept(self) -> None:
        self.assertEqual(pick_sales_line("", "Nuevo", "Nuevo | 1 vendido"), "Nuevo | 1 vendido")
        self.assertEqual(pick_sales_line("MercadoLíder | +1000 ventas"), "")
        long = (
            "Modelo De Desarrollo Del Feto Tamaño Medio. "
            "Seis etapas sobre base blanca. Nuevo | 1 vendido. Precio sin impuestos nacionales."
        )
        self.assertGreater(len(long), 80)
        self.assertEqual(pick_sales_line(long), "1 vendido")
        self.assertEqual(sales_count(pick_sales_line("Nuevo | 1 vendido")), 1)

    def test_card_text_decides_whether_to_open_the_detail(self) -> None:
        self.assertEqual(sales_action("search", "+5 vendidos"), "record")
        self.assertEqual(sales_action("search", "Nuevo"), "open")
        self.assertEqual(sales_action("search", ""), "open")
        self.assertEqual(sales_action("recommendation", "1 vendido"), "record")
        self.assertEqual(sales_action("recommendation", ""), "open")
        self.assertEqual(sales_action("search", None), "open")


if __name__ == "__main__":
    unittest.main()
