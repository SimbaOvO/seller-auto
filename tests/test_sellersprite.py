import os
import tempfile
import unittest
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright


def _chrome_for_testing() -> str:
    matches = sorted(
        (Path.home() / "Library/Caches/ms-playwright").glob(
            "chromium-*/chrome-mac-*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
        )
    )
    if not matches:
        raise unittest.SkipTest("本机没有 Chrome for Testing，跳过表头读取测试")
    return str(matches[-1])

from seller_auto.sellersprite import (
    LOOKUP_STATE_JS,
    SellerSpriteError,
    competitor_lookup_url,
    load_login,
    parse_package_cell,
    parse_package_dimensions,
)

REFERENCE = (
    "https://www.sellersprite.com/v3/competitor-lookup?market=MX&monthName=bsr_sales_nearly"
    "&asins=%5B%22B0H4PD2Z7G%22%5D&page=1&nodeIdPaths=%5B%5D&symbolFlag=true&size=60"
    "&order%5Bfield%5D=amz_unit&order%5Bdesc%5D=true&lowPrice=N&salesGrowthType=30d"
)

TABLE_HTML = """
<div class="el-table">
  <div class="el-table__fixed">
    <div class="el-table__header-wrapper">
      <table class="el-table__header"><thead><tr>
        <th><div class="cell">商品</div></th>
      </tr></thead></table>
    </div>
  </div>
  <div class="el-table__header-wrapper">
    <table class="el-table__header"><thead><tr>
      <th><div class="cell">商品</div></th>
      <th><div class="cell">包装重量<br>包装尺寸</div></th>
    </tr></thead></table>
  </div>
  <div class="el-table__body-wrapper">
    <table class="el-table__body"><tbody>
      <tr class="el-table__row"><td><div class="cell">B0OTHER123</div></td><td><div class="cell">10 x 1 x 1 cm</div></td></tr>
      <tr class="el-table__row">
        <td><div class="cell">B0H4PD2Z7G</div></td>
        <td><div class="cell"><div>454 g</div><div>23.6 x 16.3 x 7.1 cm</div><div>标准</div></div></td>
      </tr>
    </tbody></table>
  </div>
</div>
"""


class SellerSpriteTests(unittest.TestCase):
    def test_lookup_url_uses_the_opened_asin(self) -> None:
        self.assertEqual(competitor_lookup_url("b0h4pd2z7g"), REFERENCE)

    def test_dimensions_come_from_the_size_line(self) -> None:
        parsed = parse_package_dimensions("454 g\n23.6 x 16.3 x 7.1 cm\n标准")
        self.assertEqual(parsed, {"length": "23.6", "width": "16.3", "height": "7.1", "unit": "cm"})
        self.assertEqual(
            parse_package_dimensions("6.0 × 4.1 × 1.3 inches"),
            {"length": "6.0", "width": "4.1", "height": "1.3", "unit": "in"},
        )
        self.assertEqual(
            parse_package_dimensions("23,6 x 16,3 x 7,1 cm"),
            {"length": "23.6", "width": "16.3", "height": "7.1", "unit": "cm"},
        )
        self.assertIsNone(parse_package_dimensions("454 g"))
        self.assertIsNone(parse_package_dimensions("-\n-"))
        cell = parse_package_cell("454 g\n23.6 x 16.3 x 7.1 cm\n标准")
        self.assertEqual(cell["weight"], "454")
        self.assertEqual(cell["weight_unit"], "g")
        self.assertEqual(cell["length"], "23.6")
        self.assertEqual(parse_package_cell("1,200 g"), {"weight": "1200", "weight_unit": "g"})
        self.assertEqual(parse_package_cell("1.25kg"), {"weight": "1.25", "weight_unit": "kg"})
        self.assertIsNone(parse_package_cell("-\n-"))

    def test_login_reads_env_local(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env.local"
            path.write_text("E2E_USERNAME=13100000000\nE2E_PASSWORD=secret\n", encoding="utf-8")
            self.assertEqual(load_login(path), ("13100000000", "secret"))
            path.write_text("E2E_USERNAME=\n", encoding="utf-8")
            with self.assertRaises(SellerSpriteError):
                load_login(path)

    def test_header_cell_returns_the_matching_row(self) -> None:
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
                page.set_content(TABLE_HTML)
                found = page.wait_for_function(LOOKUP_STATE_JS, arg="B0H4PD2Z7G", timeout=3000).json_value()
                self.assertEqual(found["status"], "table")
                self.assertIn("23.6 x 16.3 x 7.1 cm", found["text"])
                self.assertNotIn("10 x 1 x 1", found["text"])

                page.set_content("<body><p>您是游客身份，建议立即登录</p></body>")
                guest = page.wait_for_function(LOOKUP_STATE_JS, arg="B0H4PD2Z7G", timeout=3000).json_value()
                self.assertEqual(guest["status"], "login")

                page.set_content('<form id="form_signin_passW" style="display:none"></form>' + TABLE_HTML)
                hidden = page.wait_for_function(LOOKUP_STATE_JS, arg="B0H4PD2Z7G", timeout=3000).json_value()
                self.assertEqual(hidden["status"], "table")
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
