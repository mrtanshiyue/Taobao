import unittest
from io import BytesIO

from PIL import Image

from app import _face_cascades, canonical_image_url, image_url, process_image, valid_product_url


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


if __name__ == "__main__":
    unittest.main()
