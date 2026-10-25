import unittest

from seller_auto.categories import category_reason, classify_breadcrumb


class CategoryTests(unittest.TestCase):
    def test_clothing_and_food_breadcrumbs(self) -> None:
        self.assertEqual(classify_breadcrumb("Ropa, Zapatos y Accesorios"), "clothing")
        self.assertEqual(classify_breadcrumb("Alimentos y Bebidas"), "food")
        self.assertEqual(classify_breadcrumb("Grocery & Gourmet Food"), "food")
        self.assertEqual(classify_breadcrumb("Moda"), "clothing")

    def test_unrelated_breadcrumb_is_kept(self) -> None:
        self.assertIsNone(classify_breadcrumb("Hogar y Cocina > Organizadores"))
        self.assertIsNone(classify_breadcrumb("comodidad"))

    def test_reason_labels(self) -> None:
        self.assertEqual(category_reason("clothing"), "服装")
        self.assertEqual(category_reason("food"), "食品")
        self.assertIsNone(category_reason(None))


if __name__ == "__main__":
    unittest.main()
