import unittest

from seller_auto.mercadolibre import parse_price, read_sales_text


class PriceTests(unittest.TestCase):
    def test_argentine_thousands_and_cents(self) -> None:
        self.assertEqual(parse_price("12.345"), 12345)
        self.assertEqual(parse_price("$ 8.990"), 8990)
        self.assertEqual(parse_price("12.345,50"), 12345.5)
        self.assertEqual(parse_price("8990"), 8990)
        self.assertIsNone(parse_price(""))


class _Locator:
    def __init__(self, texts: list[str]) -> None:
        self._texts = texts

    def count(self) -> int:
        return len(self._texts)

    def nth(self, index: int) -> "_Locator":
        return _Locator(self._texts[index:index + 1])

    def inner_text(self, timeout: int = 0) -> str:
        _ = timeout
        return self._texts[0]


class SalesReadTests(unittest.TestCase):
    def test_detail_subtitle_counts_even_when_the_old_selector_is_empty(self) -> None:
        class Page:
            def locator(self, selector: str) -> _Locator:
                _ = selector
                return _Locator([])

            def evaluate(self, script: str) -> list[str]:
                _ = script
                return [
                    "Nuevo | 1 vendido",
                    "Modelo De Desarrollo Del Feto Tamaño Medio",
                    "MercadoLíder | +1000 ventas",
                ]

        self.assertEqual(read_sales_text(Page()), "Nuevo | 1 vendido")


if __name__ == "__main__":
    unittest.main()
