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

RENAMED_COLUMN_HTML = """
<div class="el-table">
  <div class="el-table__header-wrapper">
    <table class="el-table__header"><thead><tr>
      <th class="el-table_1_column_1"><div class="cell">商品</div></th>
      <th class="el-table_1_column_2"><div class="cell">包装重量<br>包装尺寸</div></th>
      <th class="el-table_1_column_3"><div class="cell">商品重量<br>商品尺寸</div></th>
    </tr></thead></table>
  </div>
  <div class="el-table__body-wrapper">
    <table class="el-table__body"><tbody>
      <tr class="el-table__row">
        <td class="el-table_1_column_1"><div class="cell">B0CF1W16JG 黑胶唱片保护套</div></td>
        <td class="el-table_1_column_2"><div class="cell">-</div></td>
        <td class="el-table_1_column_3"><div class="cell">760 g<br>37.1 × 33.4 × 1.3 cm</div></td>
      </tr>
    </tbody></table>
  </div>
</div>
"""

PACKAGE_WINS_HTML = """
<div class="el-table">
  <div class="el-table__header-wrapper">
    <table class="el-table__header"><thead><tr>
      <th class="el-table_3_column_1"><div class="cell">商品</div></th>
      <th class="el-table_3_column_2"><div class="cell">商品重量<br>商品尺寸</div></th>
      <th class="el-table_3_column_3"><div class="cell">包装重量<br>包装尺寸</div></th>
    </tr></thead></table>
  </div>
  <div class="el-table__body-wrapper">
    <table class="el-table__body"><tbody>
      <tr class="el-table__row">
        <td class="el-table_3_column_1"><div class="cell">B0CF1W16JG 999 g<br>9 x 9 x 9 cm</div></td>
        <td class="el-table_3_column_2"><div class="cell">760 g<br>37.1 x 33.4 x 1.3 cm</div></td>
        <td class="el-table_3_column_3"><div class="cell">204 g<br>11.2 x 9.7 x 6.1 cm</div></td>
      </tr>
    </tbody></table>
  </div>
</div>
"""

PARTIAL_PACKAGE_HTML = """
<div class="el-table">
  <div class="el-table__header-wrapper">
    <table class="el-table__header"><thead><tr>
      <th class="el-table_5_column_2"><div class="cell">包装重量<br>包装尺寸</div></th>
      <th class="el-table_5_column_3"><div class="cell">商品重量<br>商品尺寸</div></th>
    </tr></thead></table>
  </div>
  <div class="el-table__body-wrapper">
    <table class="el-table__body"><tbody>
      <tr class="el-table__row">
        <td class="el-table_5_column_2"><div class="cell">204 g</div></td>
        <td class="el-table_5_column_3"><div class="cell">760 g<br>37.1 x 33.4 x 1.3 cm</div></td>
      </tr>
    </tbody></table>
  </div>
</div>
"""

EMPTY_COLUMNS_HTML = """
<div class="el-table">
  <div class="el-table__header-wrapper">
    <table class="el-table__header"><thead><tr>
      <th class="el-table_4_column_1"><div class="cell">商品</div></th>
      <th class="el-table_4_column_2"><div class="cell">包装重量<br>包装尺寸</div></th>
      <th class="el-table_4_column_3"><div class="cell">商品重量<br>商品尺寸</div></th>
    </tr></thead></table>
  </div>
  <div class="el-table__body-wrapper">
    <table class="el-table__body"><tbody>
      <tr class="el-table__row">
        <td class="el-table_4_column_1"><div class="cell">B0CF1W16JG 999 g<br>9 x 9 x 9 cm</div></td>
        <td class="el-table_4_column_2"><div class="cell">-</div></td>
        <td class="el-table_4_column_3"><div class="cell">-</div></td>
      </tr>
    </tbody></table>
  </div>
</div>
"""

SHIFTED_HEADER_HTML = """
<div class="el-table">
  <div class="el-table__header-wrapper">
    <table class="el-table__header"><thead><tr>
      <th class="gutter"></th>
      <th class="el-table_2_column_8"><div class="cell">商品重量<br>商品尺寸</div></th>
    </tr></thead></table>
  </div>
  <div class="el-table__body-wrapper">
    <table class="el-table__body"><tbody>
      <tr class="el-table__row">
        <td class="el-table_2_column_8"><div class="cell">760 g<br>37.1 x 33.4 x 1.3 cm</div></td>
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
        spanish = parse_package_cell("- 1,5prof. x 30an. x 71,5alt. centimeters")
        self.assertEqual(spanish["length"], "1.5")
        self.assertEqual(spanish["width"], "30")
        self.assertEqual(spanish["height"], "71.5")
        self.assertEqual(spanish["unit"], "cm")
        self.assertNotIn("weight", spanish)
        kilos = parse_package_cell("5,6 Kilograms 43,7 x 39,2 x 22,1 cm")
        self.assertEqual(kilos["weight"], "5.6")
        self.assertEqual(kilos["weight_unit"], "kg")
        self.assertEqual(kilos["length"], "43.7")
        self.assertEqual(kilos["height"], "22.1")
        flat = parse_package_cell("940 g 20l. x 20an. centimeters")
        self.assertEqual(flat["weight"], "940")
        self.assertEqual(flat["weight_unit"], "g")
        self.assertEqual(flat["length"], "20")
        self.assertEqual(flat["width"], "20")
        self.assertEqual(flat["unit"], "cm")
        self.assertNotIn("height", flat)
        canvas = parse_package_cell("1,07 Kilograms 40l. x 30an. centimeters")
        self.assertEqual(canvas["weight"], "1.07")
        self.assertEqual(canvas["weight_unit"], "kg")
        self.assertEqual(canvas["length"], "40")
        self.assertEqual(canvas["width"], "30")
        cell = parse_package_cell("454 g\n23.6 x 16.3 x 7.1 cm\n标准")
        self.assertEqual(cell["weight"], "454")
        self.assertEqual(cell["weight_unit"], "g")
        self.assertEqual(cell["length"], "23.6")
        self.assertEqual(parse_package_cell("1,200 g"), {"weight": "1200", "weight_unit": "g"})
        self.assertEqual(parse_package_cell("1.25kg"), {"weight": "1.25", "weight_unit": "kg"})
        shown = parse_package_cell("商品重量\n商品尺寸\n760 g\n37.1 × 33.4 × 1.3 cm")
        self.assertEqual(shown["weight"], "760")
        self.assertEqual(shown["weight_unit"], "g")
        self.assertEqual(shown["length"], "37.1")
        self.assertEqual(shown["width"], "33.4")
        self.assertEqual(shown["height"], "1.3")
        self.assertEqual(shown["unit"], "cm")
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

                page.set_content(RENAMED_COLUMN_HTML)
                renamed = page.wait_for_function(LOOKUP_STATE_JS, arg="B0CF1W16JG", timeout=3000).json_value()
                self.assertEqual(renamed["status"], "table")
                self.assertIn("760 g", renamed["text"])
                self.assertIn("37.1 × 33.4 × 1.3 cm", renamed["text"])
                self.assertNotIn("黑胶", renamed["text"])

                page.set_content(PACKAGE_WINS_HTML)
                package_first = page.wait_for_function(LOOKUP_STATE_JS, arg="B0CF1W16JG", timeout=3000).json_value()
                self.assertEqual(package_first["status"], "table")
                self.assertIn("204 g", package_first["text"])
                self.assertIn("11.2 x 9.7 x 6.1 cm", package_first["text"])
                self.assertNotIn("760 g", package_first["text"])
                self.assertNotIn("999 g", package_first["text"])

                page.set_content(PARTIAL_PACKAGE_HTML)
                partial = page.wait_for_function(LOOKUP_STATE_JS, arg="B0CF1W16JG", timeout=3000).json_value()
                self.assertEqual(partial["status"], "table")
                parsed = parse_package_cell(partial["text"])
                self.assertEqual(parsed["weight"], "204")
                self.assertEqual(parsed["weight_unit"], "g")
                self.assertEqual(parsed["length"], "37.1")
                self.assertEqual(parsed["width"], "33.4")
                self.assertEqual(parsed["height"], "1.3")

                page.set_content(EMPTY_COLUMNS_HTML)
                blank = page.wait_for_function(LOOKUP_STATE_JS, arg="B0CF1W16JG", timeout=3000).json_value()
                self.assertEqual(blank["status"], "table")
                self.assertEqual(blank["text"], "")

                page.set_content(SHIFTED_HEADER_HTML)
                shifted = page.wait_for_function(LOOKUP_STATE_JS, arg="B0CF1W16JG", timeout=3000).json_value()
                self.assertEqual(shifted["status"], "table")
                self.assertIn("760 g", shifted["text"])
                self.assertIn("37.1 x 33.4 x 1.3 cm", shifted["text"])

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
