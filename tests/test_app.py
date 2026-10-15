import unittest
from io import BytesIO
from threading import Lock
from unittest.mock import patch

import numpy as np
from PIL import Image

from app import (
    _face_detector,
    BrowserWorker,
    best_image_url,
    canonical_image_url,
    create_product_folder,
    face_boxes,
    image_url,
    normalize_image,
    parse_product_urls,
    process_image,
    ProductImage,
    product_title,
    record_worker_failure,
    request_image,
    require_taobao_login,
    valid_product_url,
)
from web_app import LocalApp, local_request_allowed


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


class TaobaoSessionTests(unittest.TestCase):
    class FakeContext:
        def cookies(self, _urls):
            return [{"name": "unb", "value": "present"}]

    class FakePage:
        def __init__(self, url):
            self.url = url

    def test_requires_secure_taobao_or_tmall_page_and_login_cookie(self):
        context = self.FakeContext()
        for url in (
            "https://item.taobao.com/item.htm?id=1",
            "https://detail.tmall.com/item.htm?id=2",
        ):
            with self.subTest(url=url):
                require_taobao_login(context, self.FakePage(url))

        for url in (
            "https://login.taobao.com/member/login.jhtml",
            "http://item.taobao.com/item.htm?id=1",
            "https://item.taobao.com.attacker.example/item.htm?id=1",
            "https://example.com/item.htm?id=1",
        ):
            with self.subTest(url=url), self.assertRaises(RuntimeError):
                require_taobao_login(context, self.FakePage(url))

    def test_rejects_a_missing_taobao_login_cookie(self):
        context = type("Context", (), {"cookies": lambda _self, _urls: []})()
        with self.assertRaisesRegex(RuntimeError, "未检测到淘宝登录信息"):
            require_taobao_login(context, self.FakePage("https://item.taobao.com/item.htm?id=1"))


class FailureLogTests(unittest.TestCase):
    def test_worker_failure_is_saved_with_operation_and_reason(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory, patch("app.APP_DIR", Path(directory)):
            log_path = Path(record_worker_failure("扫描", RuntimeError("页面需要登录")))
            self.assertEqual(log_path.name, "运行日志.txt")
            self.assertIn("扫描\tRuntimeError\t页面需要登录", log_path.read_text(encoding="utf-8"))


class ImageHelpersTests(unittest.TestCase):
    def test_accepts_only_known_image_hosts(self):
        self.assertEqual(image_url("//img.alicdn.com/item.jpg"), "https://img.alicdn.com/item.jpg")
        self.assertEqual(image_url("http://img.alicdn.com/item.jpg"), "https://img.alicdn.com/item.jpg")
        self.assertIsNone(image_url("https://example.com/item.jpg"))
        self.assertIsNone(image_url("https://img.alicdn.com/logo.png"))
        self.assertIsNone(image_url("https://img.alicdn.com:444/item.jpg"))
        self.assertIsNone(image_url("https://img.alicdn.com@127.0.0.1/item.jpg"))
        self.assertIsNone(image_url("https://[broken/item.jpg"))

    def test_prefers_largest_taobao_cdn_image_variant(self):
        small = "https://img.alicdn.com/item.jpg_60x60q90.jpg_.webp"
        large = "https://img.alicdn.com/item.jpg_1200x1200q90.jpg_.webp"
        self.assertEqual(best_image_url([small, large]), large)

    def test_canonical_url_deduplicates_size_variants(self):
        first = "https://img.alicdn.com/item.jpg_200x200q90.jpg_.webp?token=a"
        second = "https://img.alicdn.com/item.jpg_800x800q90.jpg_.webp?token=b"
        self.assertEqual(canonical_image_url(first), canonical_image_url(second))

    def test_image_request_follows_only_allowlisted_redirects(self):
        class FakeResponse:
            def __init__(self, status, headers=None):
                self.status = status
                self.headers = headers or {}
                self.disposed = False

            def dispose(self):
                self.disposed = True

        class FakeRequest:
            def __init__(self, responses):
                self.responses = iter(responses)
                self.calls = []

            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return next(self.responses)

        redirect = FakeResponse(302, {"location": "https://gw.alicdn.com/image.jpg"})
        final = FakeResponse(200)
        request = FakeRequest([redirect, final])
        response = request_image(
            type("Context", (), {"request": request})(),
            "https://img.alicdn.com/image.jpg",
            "https://item.taobao.com/item.htm?id=1",
        )
        self.assertIs(response, final)
        self.assertTrue(redirect.disposed)
        self.assertEqual(len(request.calls), 2)
        self.assertTrue(all(call[1]["max_redirects"] == 0 for call in request.calls))

        request = FakeRequest([FakeResponse(302, {"location": "http://127.0.0.1/admin"})])
        with self.assertRaisesRegex(RuntimeError, "不受支持"):
            request_image(
                type("Context", (), {"request": request})(),
                "https://img.alicdn.com/image.jpg",
                "https://item.taobao.com/item.htm?id=1",
            )
        self.assertEqual(len(request.calls), 1)

    def test_download_keeps_processing_after_bad_body_and_releases_responses(self):
        class FakeResponse:
            def __init__(self, data, content_type):
                self.status = 200
                self.ok = True
                self.headers = {"content-type": content_type}
                self.data = data
                self.disposed = False

            def body(self):
                return self.data

            def dispose(self):
                self.disposed = True

        class FakeRequest:
            def __init__(self, responses):
                self.responses = iter(responses)

            def get(self, _url, **_kwargs):
                return next(self.responses)

        source = Image.new("RGB", (3, 2), (180, 90, 30))
        data = BytesIO()
        source.save(data, format="PNG")
        bad = FakeResponse(b"not an image", "text/html")
        valid = FakeResponse(data.getvalue(), "application/octet-stream")
        context = type("Context", (), {"request": FakeRequest([bad, valid])})()

        from tempfile import TemporaryDirectory
        from pathlib import Path

        with TemporaryDirectory() as directory:
            result = BrowserWorker().download(
                context,
                "https://item.taobao.com/item.htm?id=1",
                [ProductImage("https://img.alicdn.com/bad.jpg", "主图"), ProductImage("https://img.alicdn.com/good.png", "主图")],
                Path(directory),
                mirror=False,
                mosaic=False,
            )
            self.assertEqual(result[:2], (1, 1))
            self.assertTrue(Path(directory, "下载原图", "主图_001.png").exists())
            self.assertTrue(Path(result[4]).exists())

        self.assertTrue(bad.disposed)
        self.assertTrue(valid.disposed)

    def test_yunet_model_loads_and_detector_is_reused(self):
        detector = _face_detector()
        self.assertIsNotNone(detector)
        self.assertIs(_face_detector(), detector)

    def test_face_detection_uses_yunet_and_clips_boxes_to_image(self):
        class FakeDetector:
            def __init__(self):
                self.input_size = None
                self.image = None

            def setInputSize(self, size):
                self.input_size = size

            def detect(self, image):
                self.image = image
                return 1, np.array([[-5, 10, 30, 40, 0.9]], dtype=np.float32)

        detector = FakeDetector()
        rgb = np.zeros((80, 100, 3), dtype=np.uint8)
        rgb[:, :, 0] = 255
        with patch("app._face_detector", return_value=detector):
            self.assertEqual(face_boxes(rgb), [(0, 10, 25, 40)])

        self.assertEqual(detector.input_size, (100, 80))
        self.assertEqual(detector.image[0, 0].tolist(), [0, 0, 255])

    def test_mosaic_uses_large_blocks_and_expands_face_area(self):
        source = Image.new("RGB", (120, 120))
        pixels = source.load()
        for y in range(120):
            for x in range(120):
                value = 255 if (x + y) % 2 else 0
                pixels[x, y] = (value, value, value)
        data = BytesIO()
        source.save(data, format="PNG")

        with patch("app.face_boxes", return_value=[(20, 20, 80, 80)]):
            processed, _extension, faces = process_image(data.getvalue(), mirror=False, mosaic=True)

        result = np.asarray(Image.open(BytesIO(processed)))
        self.assertEqual(faces, 1)
        self.assertTrue(np.all(result[8:60, 8:60] == result[8, 8]))

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

    def test_webp_is_saved_as_jpeg_with_matching_extension(self):
        source = Image.new("RGB", (2, 1), (120, 80, 40))
        data = BytesIO()
        source.save(data, format="WEBP")

        normalized, extension = normalize_image(data.getvalue())
        processed, processed_extension, _faces = process_image(data.getvalue(), mirror=False, mosaic=False)

        self.assertEqual(extension, ".jpg")
        self.assertEqual(processed_extension, ".jpg")
        self.assertEqual(Image.open(BytesIO(normalized)).format, "JPEG")
        self.assertEqual(Image.open(BytesIO(processed)).format, "JPEG")


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


class LocalAppStatusTests(unittest.TestCase):
    def test_local_api_rejects_remote_hosts_and_cross_origin_posts(self):
        self.assertTrue(local_request_allowed("127.0.0.1", "127.0.0.1:4321", "http://127.0.0.1:4321", True))
        self.assertTrue(local_request_allowed("127.0.0.1", "localhost:4321", None, True))
        self.assertFalse(local_request_allowed("192.0.2.4", "127.0.0.1:4321", None))
        self.assertFalse(local_request_allowed("127.0.0.1", "127.0.0.1:4321", "http://attacker.example", True))

    def test_request_errors_remain_visible_in_state(self):
        app = LocalApp.__new__(LocalApp)
        app.lock = Lock()
        app.status = "旧状态"
        app.status_kind = "ready"

        app.set_request_error("商品链接无效")

        self.assertEqual(app.status, "商品链接无效")
        self.assertEqual(app.status_kind, "error")

    def test_page_diagnosis_event_finishes_operation(self):
        from queue import Queue

        app = LocalApp.__new__(LocalApp)
        app.lock = Lock()
        app.worker = type("Worker", (), {"events": Queue()})()
        app.worker.events.put(("diagnosed", "诊断文件.json"))
        app.busy = True
        app.status = "正在导出"

        app.receive_worker_events()

        self.assertFalse(app.busy)
        self.assertEqual(app.status_kind, "done")
        self.assertIn("诊断文件.json", app.status)

    def test_window_close_waits_for_other_tabs_and_running_jobs(self):
        app = LocalApp.__new__(LocalApp)
        app.lock = Lock()
        app.ui_sessions = {}
        app.closed_ui_sessions = set()
        app.ui_disconnect_at = None
        app.busy = True

        app.touch_ui("first", 100.0)
        app.touch_ui("second", 100.0)
        app.disconnect_ui("first", 101.0)
        self.assertFalse(app.should_stop_after_window_close(110.0))
        app.disconnect_ui("second", 102.0)
        app.touch_ui("second", 103.0)
        self.assertFalse(app.should_stop_after_window_close(108.0))
        self.assertFalse(app.should_stop_after_window_close(110.0))
        app.busy = False
        self.assertTrue(app.should_stop_after_window_close(110.0))

    def test_stale_ui_session_gets_a_graceful_shutdown_deadline(self):
        app = LocalApp.__new__(LocalApp)
        app.lock = Lock()
        app.ui_sessions = {"abandoned": 10.0}
        app.closed_ui_sessions = set()
        app.ui_disconnect_at = None
        app.busy = False

        self.assertFalse(app.should_stop_after_window_close(1210.0))
        self.assertTrue(app.should_stop_after_window_close(1215.0))


class BrowserWorkerTests(unittest.TestCase):
    def test_submit_restarts_dead_worker_and_preserves_pending_events(self):
        from queue import Queue

        class FakeWorker:
            def __init__(self):
                self.events = Queue()
                self.commands = Queue()
                self.ident = 1
                self.started = False

            def is_alive(self):
                return False

            def start(self):
                self.started = True

        previous = FakeWorker()
        replacement = FakeWorker()
        submit = BrowserWorker.submit
        with patch("app.BrowserWorker", return_value=replacement):
            result = submit(previous, ("scan",))

        self.assertIs(result, replacement)
        self.assertIs(replacement.events, previous.events)
        self.assertTrue(replacement.started)
        self.assertEqual(replacement.commands.get_nowait(), ("scan",))


if __name__ == "__main__":
    unittest.main()
