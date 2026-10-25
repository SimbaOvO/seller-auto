import io
import unittest

from PIL import Image

from seller_auto.images import jpeg_size, save_original, to_client_jpeg


class ImageTests(unittest.TestCase):
    def test_client_jpeg_shrinks_the_long_edge_to_768(self) -> None:
        buffer = io.BytesIO()
        Image.new("RGB", (2000, 1000), "red").save(buffer, format="PNG")
        resized = to_client_jpeg(buffer.getvalue())
        width, height = jpeg_size(resized)
        self.assertLessEqual(max(width, height), 768)
        self.assertEqual(max(width, height), 768)

    def test_small_image_is_not_enlarged(self) -> None:
        buffer = io.BytesIO()
        Image.new("RGB", (100, 40), "blue").save(buffer, format="PNG")
        width, height = jpeg_size(to_client_jpeg(buffer.getvalue()))
        self.assertEqual((width, height), (100, 40))

    def test_original_png_keeps_its_suffix(self) -> None:
        from pathlib import Path
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            path = save_original(b"\x89PNG\r\n\x1a\nnot-a-real-body", Path(directory) / "shot")
            self.assertTrue(str(path).endswith(".png"))


if __name__ == "__main__":
    unittest.main()
