import unittest
from io import BytesIO

from PIL import Image

from app import (
    _face_cascades,
    canonical_image_url,
    create_product_folder,
    image_url,
    parse_product_urls,
    process_image,
    product_title,
    valid_product_url,
)


class ProductUrlTests(unittest.TestCase):
    def test_adds_https_to_supported_taobao_links(self):
        self.assertEqual(
            valid_product_url("item.taobao.com/item.htm?id=123"),
            "https://item.taobao.com/item.htm?id=123",
        )
        self.assertEqual(valid_product_url("https://detail.tmall.com/item.htm?id=123"), "https://detail.tmall.com/item.htm?id=123")

    def test_rejects_non_taobao_hosts_and_unsafe_schemes(self):
        for url in (
            "http://item.taobao.com/item.htm?id=1",
            "https://taobao.com.attacker.example/item.htm",
            "https://user@item.taobao.com/item.htm",
            "https://example.com/item.htm",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                valid_product_url(url)

    def test_parses_and_validates_multiple_links(self):
        self.assertEqual(
            parse_product_urls("item.taobao.com/item.htm?id=1\nhttps://detail.tmall.com/item.htm?id=2"),
            ["https://item.taobao.com/item.htm?id=1", "https://detail.tmall.com/item.htm?id=2"],
        )
        with self.assertRaises(ValueError):
            parse_product_urls("https://item.taobao.com/item.htm?id=1\nhttps://example.com/")


class ImageHelpersTests(unittest.TestCase):
    def test_accepts_only_known_image_hosts(self):
        self.assertEqual(image_url("//img.alicdn.com/item.jpg"), "https://img.alicdn.com/item.jpg")
        self.assertIsNone(image_url("https://example.com/item.jpg"))
        self.assertIsNone(image_url("https://img.alicdn.com/logo.png"))

    def test_canonical_url_deduplicates_size_variants(self):
        first = "https://img.alicdn.com/item.jpg_200x200q90.jpg_.webp?token=a"
        second = "https://img.alicdn.com/item.jpg_800x800q90.jpg_.webp?token=b"
        self.assertEqual(canonical_image_url(first), canonical_image_url(second))

    def test_face_cascade_files_load_and_are_reused(self):
        cascades = _face_cascades()
        frontal, profile = cascades
        self.assertFalse(frontal.empty())
        self.assertFalse(profile.empty())
        self.assertIs(_face_cascades(), cascades)

    def test_mirror_preserves_png_and_flips_pixels(self):
        source = Image.new("RGB", (2, 1))
        source.putpixel((0, 0), (255, 0, 0))
        source.putpixel((1, 0), (0, 0, 255))
        data = BytesIO()
        source.save(data, format="PNG")

        processed, extension, faces = process_image(data.getvalue(), mirror=True, mosaic=False)
        result = Image.open(BytesIO(processed))

        self.assertEqual(extension, ".png")
        self.assertEqual(faces, 0)
        self.assertEqual(result.getpixel((0, 0)), (0, 0, 255))
        self.assertEqual(result.getpixel((1, 0)), (255, 0, 0))


class ProductFolderTests(unittest.TestCase):
    class FakeLocator:
        first = None

        def __init__(self, value):
            self.value = value
            self.first = self

        def get_attribute(self, _name, timeout):
            raise TimeoutError()

        def inner_text(self, timeout):
            return self.value

    class FakePage:
        def locator(self, _selector):
            return ProductFolderTests.FakeLocator("衬衫/新品:轻薄 | 淘宝网")

        def title(self):
            return "备用标题 - 淘宝网"

    def test_product_title_is_sanitized_for_windows(self):
        self.assertEqual(product_title(self.FakePage(), "https://item.taobao.com/item.htm?id=123"), "衬衫_新品_轻薄")

    def test_duplicate_title_gets_new_folder_without_overwriting(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path

        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = create_product_folder(root, "商品标题")
            (first / "keep.txt").write_text("保留", encoding="utf-8")
            second = create_product_folder(root, "商品标题")
            self.assertEqual(second.name, "商品标题 (2)")
            self.assertEqual((first / "keep.txt").read_text(encoding="utf-8"), "保留")


if __name__ == "__main__":
    unittest.main()
