import unittest

from seller_auto.urls import (
    UrlError,
    canonical_amazon_url,
    choose_amazon_image_url,
    extract_asin,
    looks_like_verification_url,
    ml_item_key,
    ml_page2_url,
    ml_search_url,
    parse_brand,
    parse_seller_id,
    slugify_keywords,
)


STORE = "https://www.amazon.com.mx/s?me=A1A10HFD4KEZW5&marketplaceID=A1A10HFD4KEZW5"


class UrlTests(unittest.TestCase):
    def test_parse_seller_id(self) -> None:
        self.assertEqual(parse_seller_id(STORE), "A1A10HFD4KEZW5")

    def test_parse_seller_id_rejects_non_amazon(self) -> None:
        with self.assertRaises(UrlError):
            parse_seller_id("https://listado.mercadolibre.com.ar/s?me=A1A10HFD4KEZW5")

    def test_search_url_matches_the_sample_shape(self) -> None:
        keywords = "Funda para Burro de Planchar Grande"
        self.assertEqual(slugify_keywords(keywords), "funda-para-burro-de-planchar-grande")
        self.assertEqual(
            ml_search_url(keywords),
            "https://listado.mercadolibre.com.ar/funda-para-burro-de-planchar-grande"
            "?sb=all_mercadolibre#D[A:Funda%20para%20Burro%20de%20Planchar%20Grande]",
        )

    def test_slug_strips_accents(self) -> None:
        self.assertEqual(slugify_keywords("Sofá Niño"), "sofa-nino")

    def test_page2_inserts_desde_and_keeps_query(self) -> None:
        first = ml_search_url("funda grande")
        second = ml_page2_url(first)
        self.assertIn("/funda-grande_Desde_49?", second)
        self.assertIn("sb=all_mercadolibre", second)
        self.assertIn("#D[A:", second)
        self.assertEqual(ml_page2_url(second), second)

    def test_empty_keywords_rejected(self) -> None:
        with self.assertRaises(UrlError):
            ml_search_url("   ")

    def test_asin_and_canonical_product_url(self) -> None:
        raw = "https://www.amazon.com.mx/algun-nombre/dp/B0C1234567/ref=sr_1_1?keywords=x"
        self.assertEqual(extract_asin(raw), "B0C1234567")
        self.assertEqual(canonical_amazon_url(raw), "https://www.amazon.com.mx/dp/B0C1234567")

    def test_ml_item_key_ignores_tracking(self) -> None:
        left = "https://www.mercadolibre.com.ar/funda/p/MLA12345?tracking=1#reco"
        right = "https://articulo.mercadolibre.com.ar/MLA-12345-funda"
        self.assertEqual(ml_item_key(left), "MLA12345")
        self.assertEqual(ml_item_key(left), ml_item_key(right))

    def test_brand_patterns(self) -> None:
        self.assertEqual(parse_brand("Visita la tienda de ACER"), "ACER")
        self.assertEqual(parse_brand("Marca: Sony"), "Sony")

    def test_largest_dynamic_image_is_used_when_hires_missing(self) -> None:
        raw = '{"https://img/small.jpg": [100, 100], "https://img/big.jpg": [1000, 1000]}'
        self.assertEqual(choose_amazon_image_url(None, raw, "https://img/src.jpg"), "https://img/big.jpg")
        self.assertEqual(choose_amazon_image_url("https://img/hi.jpg", raw, None), "https://img/hi.jpg")

    def test_verification_urls(self) -> None:
        self.assertTrue(looks_like_verification_url("https://www.amazon.com.mx/errors/validateCaptcha"))
        self.assertTrue(looks_like_verification_url("https://www.mercadolibre.com.ar/gz/account-verification"))
        self.assertFalse(looks_like_verification_url("https://www.amazon.com.mx/dp/B0C1234567"))

    def test_access_error_page_counts_as_blocked(self) -> None:
        from seller_auto.urls import text_looks_blocked

        self.assertTrue(text_looks_blocked("Hubo un error accediendo a esta pagina... Ir a la página principal"))
        self.assertFalse(text_looks_blocked("Funda para burro de planchar"))


if __name__ == "__main__":
    unittest.main()
