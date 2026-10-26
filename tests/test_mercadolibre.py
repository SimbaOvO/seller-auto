import unittest

from seller_auto.mercadolibre import parse_price


class PriceTests(unittest.TestCase):
    def test_argentine_thousands_and_cents(self) -> None:
        self.assertEqual(parse_price("12.345"), 12345)
        self.assertEqual(parse_price("$ 8.990"), 8990)
        self.assertEqual(parse_price("12.345,50"), 12345.5)
        self.assertEqual(parse_price("8990"), 8990)
        self.assertIsNone(parse_price(""))


if __name__ == "__main__":
    unittest.main()
