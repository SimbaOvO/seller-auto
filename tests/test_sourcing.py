import os
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from seller_auto.sourcing import (
    READ_PRICES_JS,
    amazon_search_image,
    average_price,
    is_login_url,
    sourcing_url,
)


def _chrome_for_testing() -> str:
    from seller_auto.browser_launch import chrome_for_testing_binary, default_playwright_browsers_dir

    binary = chrome_for_testing_binary(default_playwright_browsers_dir())
    if binary is None:
        raise unittest.SkipTest("本机没有 Chrome for Testing，跳过货源价格测试")
    return str(binary)


def _image_list() -> str:
    cards = []
    for index, price in enumerate(("14.00", "16.50", "9.80"), start=1):
        whole, fraction = price.split(".")
        cards.append(
            "<div class='productCell'><div class='card'>"
            f"<img src='https://cbu01.alicdn.com/img/{index}.jpg'>"
            "<div class='productPrice'>"
            "<span class='priceSymbol'>￥</span>"
            f"<span class='priceInteger'>{whole}</span>"
            f"<span class='priceDecimal'>.{fraction}</span>"
            "</div></div></div>"
        )
    return "<div id='list'>" + "".join(cards) + "</div>"


def _offers(count: int) -> str:
    items = []
    for index in range(1, count + 1):
        strike = '<span class="line-through">99.00</span>' if index == 1 else ""
        items.append(
            "<div class='item'>"
            f"<a href='https://detail.1688.com/offer/{index}.html'>图</a>"
            f"{strike}<span>{index}.00</span>"
            "</div>"
        )
    return "<div id='list'>" + "".join(items) + "</div>"


class SourcingTests(unittest.TestCase):
    def test_image_url_is_rewritten_like_sellersprite(self) -> None:
        raw = "https://images-na.ssl-images-amazon.com/images/I/41DfVLl1EXL._AC_SL1500_.jpg"
        self.assertEqual(amazon_search_image(raw), "https://m.media-amazon.com/images/I/41DfVLl1EXL._AC_US200_.jpg")

    def test_sourcing_url_uses_the_picture_search_query(self) -> None:
        url = sourcing_url("https://m.media-amazon.com/images/I/41DfVLl1EXL._AC_SL1500_.jpg")
        self.assertTrue(url.startswith("https://aibuy.1688.com/landingpage/new-home/find-products.html?"))
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query["bizType"], ["selectionTool"])
        self.assertEqual(query["customerId"], ["sellerspriteLP"])
        self.assertEqual(query["lang"], ["zh"])
        self.assertEqual(query["currency"], ["CNY"])
        self.assertEqual(
            query["outImageAddress"],
            ["https://m.media-amazon.com/images/I/41DfVLl1EXL._AC_US200_.jpg"],
        )
        self.assertEqual(
            url,
            "https://aibuy.1688.com/landingpage/new-home/find-products.html?"
            "bizType=selectionTool&customerId=sellerspriteLP"
            "&outImageAddress=https%3A%2F%2Fm.media-amazon.com%2Fimages%2FI%2F41DfVLl1EXL._AC_US200_.jpg"
            "&lang=zh&currency=CNY",
        )

    def test_average_uses_only_the_first_ten(self) -> None:
        prices = [f"{index}.00" for index in range(1, 13)]
        self.assertEqual(average_price(prices), "5.50")
        self.assertIsNone(average_price([]))

    def test_login_wall_is_recognized(self) -> None:
        self.assertTrue(is_login_url("https://login.1688.com/member/signin.htm"))
        self.assertFalse(is_login_url("https://s.1688.com/youyuan/index.htm"))

    def test_first_ten_offer_prices_skip_strikethrough(self) -> None:
        if os.environ.get("SELLER_AUTO_BROWSER_TESTS") != "1":
            self.skipTest("默认不启动 Chrome for Testing，避免无窗口会话里弹出崩溃框")
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(executable_path=_chrome_for_testing(), headless=True)
            except PlaywrightError as exc:
                message = str(exc)
                if "Executable doesn't exist" in message or "has been closed" in message:
                    self.skipTest("当前环境无法启动 Chrome for Testing")
                raise
            try:
                page = browser.new_page()
                page.set_content(_offers(12))
                prices = page.evaluate(READ_PRICES_JS)
                self.assertEqual(prices, [f"{index}.00" for index in range(1, 11)])
                self.assertNotIn("99.00", prices)
                page.set_content(_image_list())
                self.assertEqual(page.evaluate(READ_PRICES_JS), ["14.00", "16.50", "9.80"])
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
