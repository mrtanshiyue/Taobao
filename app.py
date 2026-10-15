from __future__ import annotations

import html
import csv
import json
import queue
import re
import sys
import threading
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from io import BytesIO
from pathlib import Path
try:
    from playwright.sync_api import sync_playwright
    from PIL import Image, ImageOps
    import cv2
    import numpy as np
except ImportError as exc:
    raise SystemExit(f"缺少依赖：{exc.name}。请先运行 setup.bat。") from exc


APP_DIR = Path.home() / ".taobao_image_helper"
IMAGE_HOSTS = ("alicdn.com", "tbcdn.cn", "taobao.com", "taobaocdn.com")
PRODUCT_HOSTS = ("taobao.com", "tmall.com", "tmall.hk", "tb.cn")
MAIN_IMAGE_CONTAINERS = (
    "#picGalleryEle", "#tb-gallery", ".tb-gallery", "#J_UlThumb", ".J_UlThumb",
    ".tb-thumb", "#mainPicImageEl",
)
DETAIL_IMAGE_CONTAINERS = (
    "#imageTextInfo-content", "#J_DivItemDesc", "#description",
    ".descV8-container", ".desc-root",
)


@dataclass
class ProductImage:
    url: str
    category: str


def is_product_host(host: str) -> bool:
    return any(host == domain or host.endswith("." + domain) for domain in PRODUCT_HOSTS)


def valid_product_url(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("请先粘贴淘宝商品链接。")
    if "://" not in value:
        value = "https://" + value
    try:
        parsed = urllib.parse.urlparse(value)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError as exc:
        raise ValueError("请输入有效的淘宝、天猫或淘宝短链接。") from exc
    if (
        parsed.scheme != "https"
        or port not in (None, 443)
        or parsed.username
        or parsed.password
        or not is_product_host(host)
    ):
        raise ValueError("请输入 HTTPS 的淘宝、天猫或淘宝短链接。")
    return value


def parse_product_urls(value: str) -> list[str]:
    urls = [part.strip() for part in (value or "").splitlines() if part.strip()]
    if not urls:
        raise ValueError("请至少粘贴一个淘宝商品链接，每行一个。")
    return [valid_product_url(url) for url in urls]


def product_title(page, url: str) -> str:
    title = ""
    for selector, attribute in (("meta[property='og:title']", "content"), ("h1", None)):
        try:
            locator = page.locator(selector).first
            title = locator.get_attribute(attribute, timeout=1200) if attribute else locator.inner_text(timeout=1200)
        except Exception:
            continue
        if title and title.strip():
            break
    if not title:
        try:
            title = page.title()
        except Exception:
            title = ""
    title = re.sub(r"\s*[-_|]\s*(淘宝网|天猫|淘宝|tmall\.com).*$", "", title or "", flags=re.IGNORECASE).strip()
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).strip(" .")[:80].rstrip(" .")
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if not title or title.split(".", 1)[0].upper() in reserved:
        item_id = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("id", [""])[0]
        title = f"商品-{item_id}" if item_id.isdigit() else "淘宝商品"
    return title


def create_product_folder(output_dir: Path, title: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    candidate = output_dir / title
    suffix = 2
    while True:
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            candidate = output_dir / f"{title} ({suffix})"
            suffix += 1


def image_url(value: str) -> str | None:
    value = html.unescape(value or "").strip()
    if value.startswith("//"):
        value = "https:" + value
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    if scheme == "http":
        parsed = parsed._replace(scheme="https")
        value = urllib.parse.urlunsplit(parsed)
        scheme = "https"
    host = (parsed.hostname or "").lower()
    if scheme != "https" or port not in (None, 443) or parsed.username or parsed.password:
        return None
    if not any(host == suffix or host.endswith("." + suffix) for suffix in IMAGE_HOSTS):
        return None
    if any(word in parsed.path.lower() for word in ("icon", "logo", "qrcode")):
        return None
    return value


def best_image_url(values: list[str]) -> str | None:
    candidates = [(index, url) for index, value in enumerate(values) if (url := image_url(value))]
    if not candidates:
        return None

    def rank(candidate):
        index, url = candidate
        path = urllib.parse.urlsplit(url).path.lower()
        sizes = re.findall(r"_(\d{2,5})x(\d{2,5})(?:q\d+)?(?:\.[^./]+)?", path)
        query = urllib.parse.urlsplit(url).query.lower()
        resize_widths = re.findall(r"(?:^|[,/&])w[=_-](\d{2,5})", query)
        if sizes:
            quality = (0, max(int(width) * int(height) for width, height in sizes))
        elif resize_widths or "resize" in query:
            quality = (0, max((int(width) ** 2 for width in resize_widths), default=0))
        else:
            quality = (1, 0)
        return (*quality, -index)

    return max(candidates, key=rank)[1]


def canonical_image_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    path = parsed.path
    extension = re.search(r"\.(?:jpe?g|png|webp|gif|bmp)(?=$|[_@])", path, re.IGNORECASE)
    if extension:
        path = path[:extension.end()]
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def request_image(context, value: str, referer: str):
    url = image_url(value)
    if not url:
        raise ValueError("图片地址不是受支持的淘宝图片链接。")
    for _ in range(6):
        response = context.request.get(
            url, timeout=30000, headers={"Referer": referer}, max_redirects=0
        )
        if response.status not in (301, 302, 303, 307, 308):
            return response
        location = response.headers.get("location", "")
        try:
            response.dispose()
        except Exception:
            pass
        redirected = image_url(urllib.parse.urljoin(url, location)) if location else None
        if not redirected:
            raise RuntimeError("图片跳转到不受支持的地址，已停止请求。")
        url = redirected
    raise RuntimeError("图片跳转次数过多，已停止请求。")


def taobao_login_present(context) -> bool:
    cookies = context.cookies([
        "https://www.taobao.com", "https://item.taobao.com",
        "https://www.tmall.com", "https://detail.tmall.com", "https://www.tmall.hk",
    ])
    names = {cookie["name"] for cookie in cookies if cookie.get("value")}
    return bool(names.intersection({"unb", "cookie17"}))


def require_taobao_login(context, page):
    try:
        parsed = urllib.parse.urlparse(page.url)
        host = (parsed.hostname or "").lower()
        if (
            parsed.scheme != "https"
            or parsed.port not in (None, 443)
            or not is_product_host(host)
        ):
            raise ValueError
    except ValueError:
        raise RuntimeError("当前页面已离开淘宝或天猫，请重新打开商品链接后再操作。") from None
    if host.split(".", 1)[0] in {"login", "passport"}:
        raise RuntimeError("当前仍在淘宝登录页，请在 Edge 完成登录并确认商品页已打开。")
    if not taobao_login_present(context):
        raise RuntimeError("未检测到淘宝登录信息。请先在弹出的 Edge 商品页登录，再重新点击扫描或下载。")


def record_worker_failure(operation: str, error: Exception) -> str:
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        path = APP_DIR / "运行日志.txt"
        detail = " ".join(str(error).replace("\t", " ").splitlines())
        with path.open("a", encoding="utf-8") as log_file:
            log_file.write(
                f"{datetime.now():%Y-%m-%d %H:%M:%S}\t{operation}\t"
                f"{type(error).__name__}\t{detail}\n"
            )
        return str(path)
    except OSError:
        return ""


def export_page_structure(page) -> Path:
    script = r"""() => {
        const selectorGroups = {
            mainGallery: ['#picGalleryEle', '#J_UlThumb', '.J_UlThumb', '.tb-thumb', '.tb-main-pic', '.tb-gallery',
                '[class*="mainPic"]', '[class*="main-pic"]', '[class*="gallery"]'],
            detail: ['#imageTextInfo-content', '.desc-root', '.descV8-container', '#description',
                '#J_DivItemDesc', '[id*="description"]', '[id*="Description"]',
                '[class*="desc-root"]', '[class*="descV8"]', '[class*="ItemDesc"]']
        };
        const describe = (element) => {
            const rect = element.getBoundingClientRect();
            const style = getComputedStyle(element);
            return {
                tag: element.tagName.toLowerCase(), id: element.id || "",
                className: typeof element.className === "string" ? element.className.slice(0, 180) : "",
                role: element.getAttribute("role") || "",
                controls: element.getAttribute("aria-controls") || "",
                targetId: (element.getAttribute("href") || "").startsWith("#")
                    ? element.getAttribute("href").slice(1) : "",
                imageCount: element.querySelectorAll("img").length,
                visibleImageCount: Array.from(element.querySelectorAll("img")).filter((img) => {
                    const r = img.getBoundingClientRect();
                    return r.width > 0 && r.height > 0 && getComputedStyle(img).visibility !== "hidden";
                }).length,
                width: Math.round(rect.width), height: Math.round(rect.height),
                display: style.display, visibility: style.visibility
            };
        };
        const candidates = {};
        for (const [group, selectors] of Object.entries(selectorGroups)) {
            const seen = new Set();
            candidates[group] = selectors.flatMap((selector) => {
                try {
                    return Array.from(document.querySelectorAll(selector)).filter((element) => {
                        if (seen.has(element)) return false;
                        seen.add(element);
                        return true;
                    }).map((element) => ({selector, ...describe(element)}));
                } catch (_) { return []; }
            }).slice(0, 80);
        }
        const labels = new Set(["商品详情", "宝贝详情", "图文详情", "商品参数", "主图"]);
        const labelElements = Array.from(document.querySelectorAll("button,a,[role=tab],h1,h2,h3,h4"))
            .filter((element) => labels.has((element.innerText || element.textContent || "").replace(/\s+/g, "").trim()))
            .slice(0, 40).map((element) => ({label: (element.innerText || element.textContent || "").trim(), ...describe(element),
                ancestors: Array.from((function* () { let node = element.parentElement; let n = 0; while (node && n++ < 5) { yield node; node = node.parentElement; } })()).map(describe)}));
        const largestImages = Array.from(document.images).sort((a, b) =>
            b.naturalWidth * b.naturalHeight - a.naturalWidth * a.naturalHeight).slice(0, 24).map((image) => {
                const rect = image.getBoundingClientRect();
                const ancestors = [];
                let node = image.parentElement;
                while (node && ancestors.length < 6) { ancestors.push(describe(node)); node = node.parentElement; }
                return {naturalWidth: image.naturalWidth, naturalHeight: image.naturalHeight,
                    width: Math.round(rect.width), height: Math.round(rect.height), ancestors};
            });
        return {host: location.hostname, frameCount: window.length, imageCount: document.images.length,
            scrollHeight: document.documentElement.scrollHeight,
            viewport: {width: innerWidth, height: innerHeight}, candidates, labelElements, largestImages};
    }"""
    frames = []
    for index, frame in enumerate(page.frames):
        try:
            data = frame.evaluate(script)
            data["frameIndex"] = index
            frames.append(data)
        except Exception as exc:
            frames.append({"frameIndex": index, "error": type(exc).__name__})
    folder = APP_DIR / "diagnostics"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"page_structure_{datetime.now():%Y%m%d_%H%M%S}.json"
    path.write_text(json.dumps({"frames": frames}, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def scan_product_page(page) -> tuple[list[ProductImage], str]:
    read_images = """roots => {
        const images = roots.flatMap(root => root.matches("img") ? [root] : [...root.querySelectorAll("img")]);
        return images.map(img => ({
            urls: [img.getAttribute("data-src"), img.getAttribute("data-original"),
                img.getAttribute("data-ks-lazyload"), img.getAttribute("data-lazyload"),
                img.getAttribute("data-lazy-src"), img.currentSrc, img.src],
            width: img.naturalWidth || img.width,
            height: img.naturalHeight || img.height,
            y: img.getBoundingClientRect().top + window.scrollY
        }));
    }"""

    def find_container(selectors):
        for frame in page.frames:
            for selector in selectors:
                try:
                    images = frame.locator(selector).evaluate_all(read_images)
                except Exception:
                    continue
                if images:
                    return frame, selector, images
        return None, None, []

    groups = {
        "主图": find_container(MAIN_IMAGE_CONTAINERS),
        "详情图": find_container(DETAIL_IMAGE_CONTAINERS),
    }
    found: dict[str, tuple[ProductImage, int]] = {}
    read_failed = False
    for category, (frame, selector, images) in groups.items():
        if not frame:
            continue
        try:
            bottom = max((int(image["y"]) for image in images), default=0)
            for position in range(0, bottom + 1, 1000):
                frame.evaluate("position => window.scrollTo(0, position)", position)
                page.wait_for_timeout(100)
            images = frame.locator(selector).evaluate_all(read_images)
            frame.evaluate("window.scrollTo(0, 0)")
        except Exception:
            read_failed = True
            continue

        for item in images:
            if 0 < item["width"] < 60 or 0 < item["height"] < 60:
                continue
            url = best_image_url(item["urls"])
            if url:
                key = canonical_image_url(url)
                score = item["width"] * item["height"]
                current = found.get(key)
                if current and current[0].category == "主图" and category == "详情图":
                    if score > current[1]:
                        found[key] = (ProductImage(url, "主图"), score)
                    continue
                if current is None or category == "主图" or score > current[1]:
                    found[key] = (ProductImage(url, category), score)

    warnings = []
    if not groups["主图"][0]:
        warnings.append("未识别到主图画廊，主图未扫描")
    if not groups["详情图"][0]:
        warnings.append("未识别到详情图容器，详情图未扫描")
    if read_failed:
        warnings.append("部分图片区域读取失败，结果可能不完整")
    return [image for image, _score in found.values()], "；".join(warnings)


def _resource_path(relative_path: str) -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / relative_path


@lru_cache(maxsize=1)
def _face_detector():
    return cv2.FaceDetectorYN.create(
        str(_resource_path("models/face_detection_yunet_2023mar.onnx")),
        "",
        (320, 320),
        score_threshold=0.6,
        nms_threshold=0.3,
        top_k=5000,
    )


def face_boxes(rgb: np.ndarray) -> list[tuple[int, int, int, int]]:
    # shortcut: YuNet can miss tiny or occluded faces; review those images manually.
    image = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    detector = _face_detector()
    detector.setInputSize((image.shape[1], image.shape[0]))
    _status, detections = detector.detect(image)
    if detections is None:
        return []
    height, width = image.shape[:2]
    boxes = []
    for x, y, w, h in detections[:, :4]:
        left, top = max(0, int(x)), max(0, int(y))
        right, bottom = min(width, int(x + w)), min(height, int(y + h))
        if right > left and bottom > top:
            boxes.append((left, top, right - left, bottom - top))
    return boxes


def _encode_portable_image(image: Image.Image, preserve_png: bool) -> tuple[bytes, str]:
    output = BytesIO()
    if preserve_png or "A" in image.getbands() or "transparency" in image.info:
        image.save(output, format="PNG")
        return output.getvalue(), ".png"
    image.convert("RGB").save(output, format="JPEG", quality=94)
    return output.getvalue(), ".jpg"


def _open_image(data: bytes) -> tuple[Image.Image, bool]:
    source = Image.open(BytesIO(data))
    preserve_png = source.format == "PNG" or "A" in source.getbands() or "transparency" in source.info
    image = ImageOps.exif_transpose(source)
    if image.mode not in ("RGB", "RGBA"):
        image = image.convert("RGBA" if preserve_png else "RGB")
    return image, preserve_png


def normalize_image(data: bytes) -> tuple[bytes, str]:
    image, preserve_png = _open_image(data)
    return _encode_portable_image(image, preserve_png)


def _transform_image(image: Image.Image, preserve_png: bool, mirror: bool, mosaic: bool) -> tuple[bytes, str, int]:
    faces = 0
    if mosaic:
        rgb = np.asarray(image.convert("RGB"))
        for x, y, w, h in face_boxes(rgb):
            pad_x, pad_y = max(4, round(w * 0.15)), max(4, round(h * 0.15))
            left, top = max(0, x - pad_x), max(0, y - pad_y)
            right, bottom = min(image.width, x + w + pad_x), min(image.height, y + h + pad_y)
            crop = image.crop((left, top, right, bottom))
            small = crop.resize((max(1, crop.width // 48), max(1, crop.height // 48)), Image.Resampling.BILINEAR)
            image.paste(small.resize(crop.size, Image.Resampling.NEAREST), (left, top))
            faces += 1
    if mirror:
        image = ImageOps.mirror(image)
    return _encode_portable_image(image, preserve_png) + (faces,)


def process_image(data: bytes, mirror: bool, mosaic: bool) -> tuple[bytes, str, int]:
    image, preserve_png = _open_image(data)
    return _transform_image(image, preserve_png, mirror, mosaic)


class BrowserWorker(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.commands: queue.Queue = queue.Queue()
        self.events: queue.Queue = queue.Queue()

    def emit(self, kind: str, value=None):
        self.events.put((kind, value))

    def submit(self, command: tuple):
        worker = self
        if not worker.is_alive():
            if worker.ident is None:
                worker.start()
            else:
                worker = BrowserWorker()
                worker.events = self.events
                worker.start()
        worker.commands.put(command)
        return worker

    def run(self):
        context = page = None
        try:
            with sync_playwright() as playwright:
                while True:
                    command = self.commands.get()
                    kind = command[0]
                    if kind == "quit":
                        break
                    try:
                        if kind == "open":
                            if context:
                                try:
                                    context.close()
                                except Exception:
                                    pass
                            context = page = None
                            APP_DIR.mkdir(parents=True, exist_ok=True)
                            context = playwright.chromium.launch_persistent_context(
                                str(APP_DIR / "browser"), channel="msedge", headless=False, viewport=None
                            )
                            page = context.pages[0] if context.pages else context.new_page()
                            page.goto(command[1], wait_until="domcontentloaded", timeout=60000)
                            try:
                                require_taobao_login(context, page)
                                login_hint = "已检测到淘宝登录信息，可以扫描图片。"
                            except RuntimeError as exc:
                                login_hint = str(exc)
                            self.emit("opened", f"浏览器已打开。{login_hint}")
                        elif kind == "scan":
                            if not page:
                                raise RuntimeError("请先打开商品链接。")
                            require_taobao_login(context, page)
                            images, warning = scan_product_page(page)
                            self.emit("scanned", (images, warning))
                        elif kind == "diagnose":
                            if not page:
                                raise RuntimeError("请先打开商品链接。")
                            require_taobao_login(context, page)
                            path = export_page_structure(page)
                            self.emit("diagnosed", str(path))
                        elif kind == "download":
                            if not context or not page:
                                raise RuntimeError("请先打开商品链接。")
                            require_taobao_login(context, page)
                            images, output_dir, mirror, mosaic = command[1:]
                            folder = create_product_folder(output_dir, product_title(page, page.url))
                            self.download(context, page.url, images, folder, mirror, mosaic)
                        elif kind == "batch":
                            urls, output_dir, mirror, mosaic = command[1:]
                            if not context:
                                APP_DIR.mkdir(parents=True, exist_ok=True)
                                context = playwright.chromium.launch_persistent_context(
                                    str(APP_DIR / "browser"), channel="msedge", headless=False, viewport=None
                                )
                                page = context.pages[0] if context.pages else context.new_page()
                            page.goto(urls[0], wait_until="domcontentloaded", timeout=60000)
                            require_taobao_login(context, page)
                            products = []
                            totals = {"ok": 0, "failed": 0, "product_errors": 0, "scan_warnings": 0, "faces": 0}
                            for index, url in enumerate(urls, 1):
                                folder = None
                                stage = "打开商品页"
                                try:
                                    if index > 1:
                                        page.goto(url, wait_until="domcontentloaded", timeout=60000)
                                    page.wait_for_timeout(1000)
                                    require_taobao_login(context, page)
                                    title = product_title(page, url)
                                    folder = create_product_folder(output_dir, title)
                                    stage = "扫描主图和详情图"
                                    images, warning = scan_product_page(page)
                                    if warning:
                                        totals["scan_warnings"] += 1
                                        self.emit("progress", f"商品 {index}/{len(urls)} 扫描提示：{warning}")
                                    if not images:
                                        raise RuntimeError(warning or "未找到主图或详情图")
                                    stage = "下载图片"
                                    ok, failed, faces, folder_path, failure_log = self.download(
                                        context, page.url, images, folder, mirror, mosaic,
                                        report=False, progress_label=f"商品 {index}/{len(urls)}：",
                                    )
                                    totals["ok"] += ok
                                    totals["failed"] += failed
                                    totals["faces"] += faces
                                    product = {"title": title, "folder": folder_path, "ok": ok, "failed": failed}
                                    if warning:
                                        product["warning"] = warning
                                    products.append(product)
                                except Exception as exc:
                                    totals["product_errors"] += 1
                                    if folder is None:
                                        item_id = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("id", [""])[0]
                                        folder = create_product_folder(output_dir, f"商品-{item_id}" if item_id.isdigit() else "淘宝商品")
                                    log_path = folder / "下载失败记录.txt"
                                    try:
                                        log_path.write_text(
                                            f"商品链接：{url}\n失败阶段：{stage}\n失败原因：{type(exc).__name__}: {exc}\n",
                                            encoding="utf-8-sig",
                                        )
                                    except OSError:
                                        pass
                                    products.append({"title": folder.name, "folder": str(folder), "ok": 0, "failed": 1, "error": str(exc)})
                                    self.emit("progress", f"商品 {index}/{len(urls)} 失败：{exc}")
                            self.emit("batch_complete", {**totals, "products": products})
                    except Exception as exc:
                        log_path = record_worker_failure(kind, exc)
                        message = f"{type(exc).__name__}: {exc}"
                        if log_path:
                            message += f"\n失败详情已记入：{log_path}"
                        self.emit("error", message)
        except Exception as exc:
            log_path = record_worker_failure("浏览器服务", exc)
            message = f"浏览器服务已停止：{type(exc).__name__}: {exc}"
            if log_path:
                message += f"\n失败详情已记入：{log_path}"
            self.emit("fatal", message)
        finally:
            if context:
                try:
                    context.close()
                except Exception:
                    pass

    def download(self, context, referer: str, images, output_dir: Path, mirror: bool, mosaic: bool, report=True, progress_label=""):
        output_dir.mkdir(parents=True, exist_ok=True)
        originals = output_dir / "下载原图"
        processed = output_dir / "处理后"
        originals.mkdir(parents=True, exist_ok=True)
        if mirror or mosaic:
            processed.mkdir(parents=True, exist_ok=True)
        ok = failed = face_total = 0
        failure_details = []
        counts = {"主图": 0, "详情图": 0}
        for index, item in enumerate(images, 1):
            stage = "请求图片"
            http_status = ""
            content_type = ""
            response = None
            try:
                response = request_image(context, item.url, referer)
                http_status = response.status
                content_type = response.headers.get("content-type", "").split(";")[0]
                if not response.ok:
                    raise RuntimeError(f"HTTP {response.status}")
                stage = "读取图片内容"
                data = response.body()
                source_image, preserve_png = _open_image(data)
                normalized, ext = _encode_portable_image(source_image, preserve_png)
                next_count = counts[item.category] + 1
                name = f"{item.category}_{next_count:03d}"
                stage = "保存原图"
                (originals / f"{name}{ext}").write_bytes(normalized)
                counts[item.category] = next_count
                if mirror or mosaic:
                    stage = "镜像和人脸处理"
                    transformed, out_ext, faces = _transform_image(
                        source_image.copy(), preserve_png, mirror, mosaic
                    )
                    stage = "保存处理后图片"
                    (processed / f"{name}{out_ext}").write_bytes(transformed)
                    face_total += faces
                ok += 1
            except Exception as exc:
                failed += 1
                failure_details.append([
                    index, item.category, stage, http_status, content_type,
                    type(exc).__name__, str(exc), item.url,
                ])
                self.emit("progress", f"{progress_label}{index}/{len(images)}：{stage}失败（{exc}）")
                continue
            finally:
                if response is not None:
                    try:
                        response.dispose()
                    except Exception:
                        pass
            self.emit("progress", f"{progress_label}已处理 {index}/{len(images)} 张，识别到 {face_total} 张脸")
        failure_log = ""
        if failure_details:
            failure_path = output_dir / "下载失败记录.txt"
            try:
                with failure_path.open("w", encoding="utf-8-sig", newline="") as log_file:
                    writer = csv.writer(log_file, delimiter="\t", lineterminator="\n")
                    writer.writerow(["序号", "类型", "失败阶段", "HTTP状态", "Content-Type", "错误类型", "失败原因", "图片地址"])
                    writer.writerows(failure_details)
                failure_log = str(failure_path)
            except OSError as exc:
                self.emit("progress", f"无法保存失败详情：{exc}")
        result = (ok, failed, face_total, str(output_dir), failure_log)
        if report:
            self.emit("complete", result)
        return result


if __name__ == "__main__":
    sys.modules.setdefault("app", sys.modules[__name__])
    from web_app import serve

    serve()
