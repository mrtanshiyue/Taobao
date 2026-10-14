from __future__ import annotations

import html
import csv
import json
import mimetypes
import queue
import re
import tempfile
import threading
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from tkinter import BooleanVar, StringVar, Tk, filedialog, font as tkfont, messagebox, ttk

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
        or not any(host == domain or host.endswith("." + domain) for domain in PRODUCT_HOSTS)
    ):
        raise ValueError("请输入 HTTPS 的淘宝、天猫或淘宝短链接。")
    return value


def image_url(value: str) -> str | None:
    value = html.unescape(value or "").strip()
    if value.startswith("//"):
        value = "https:" + value
    if not value.startswith(("http://", "https://")):
        return None
    host = (urllib.parse.urlparse(value).hostname or "").lower()
    if not any(host == suffix or host.endswith("." + suffix) for suffix in IMAGE_HOSTS):
        return None
    if any(word in value.lower() for word in ("icon", "logo", "qrcode")):
        return None
    return value


def canonical_image_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    path = parsed.path
    extension = re.search(r"\.(?:jpe?g|png|webp|gif|bmp)(?=$|[_@])", path, re.IGNORECASE)
    if extension:
        path = path[:extension.end()]
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def taobao_login_present(context) -> bool:
    cookies = context.cookies(["https://www.taobao.com", "https://item.taobao.com"])
    names = {cookie["name"] for cookie in cookies if cookie.get("value")}
    return bool(names.intersection({"unb", "cookie17"}))


def require_taobao_login(context):
    if not taobao_login_present(context):
        raise RuntimeError("未检测到淘宝登录信息。请先在弹出的 Edge 商品页登录，再重新点击扫描或下载。")


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
            urls: [img.currentSrc, img.src, img.getAttribute("data-src"),
                img.getAttribute("data-ks-lazyload"), img.getAttribute("data-lazyload"),
                img.getAttribute("data-original"), img.getAttribute("data-lazy-src")],
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
            for candidate in item["urls"]:
                url = image_url(candidate)
                if not url:
                    continue
                key = canonical_image_url(url)
                score = item["width"] * item["height"]
                current = found.get(key)
                if current and current[0].category == "主图" and category == "详情图":
                    break
                if current is None or category == "主图" or score > current[1]:
                    found[key] = (ProductImage(url, category), score)
                break

    warnings = []
    if not groups["主图"][0]:
        warnings.append("未识别到主图画廊，主图未扫描")
    if not groups["详情图"][0]:
        warnings.append("未识别到详情图容器，详情图未扫描")
    if read_failed:
        warnings.append("部分图片区域读取失败，结果可能不完整")
    return [image for image, _score in found.values()], "；".join(warnings)


@lru_cache(maxsize=1)
def _face_cascades():
    return (
        cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml"),
        cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_profileface.xml"),
    )


def face_boxes(rgb: np.ndarray) -> list[tuple[int, int, int, int]]:
    # shortcut: Haar cascades can still miss occluded faces; use a trained detector for higher recall.
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    cascades = _face_cascades()
    boxes: list[tuple[int, int, int, int]] = []
    for cascade_index, cascade in enumerate(cascades):
        if cascade.empty():
            continue
        variants = [(gray, False)]
        if cascade_index == 1:
            variants.append((cv2.flip(gray, 1), True))
        for variant, flipped in variants:
            for x, y, w, h in cascade.detectMultiScale(
                variant, scaleFactor=1.05, minNeighbors=4, minSize=(24, 24)
            ):
                boxes.append((gray.shape[1] - x - w if flipped else x, y, w, h))

    # Remove overlapping detections from frontal/profile cascades.
    result: list[tuple[int, int, int, int]] = []
    for box in sorted(boxes, key=lambda item: item[2] * item[3], reverse=True):
        x, y, w, h = box
        if all(
            (max(0, min(x + w, xx + ww) - max(x, xx)) * max(0, min(y + h, yy + hh) - max(y, yy)))
            / max(1, w * h + ww * hh - max(0, min(x + w, xx + ww) - max(x, xx)) * max(0, min(y + h, yy + hh) - max(y, yy))) < 0.45
            for xx, yy, ww, hh in result
        ):
            result.append(box)
    return result


def process_image(data: bytes, mirror: bool, mosaic: bool) -> tuple[bytes, str, int]:
    image = ImageOps.exif_transpose(Image.open(BytesIO(data)))
    if image.mode not in ("RGB", "RGBA"):
        image = image.convert("RGB")
    faces = 0
    if mosaic:
        rgb = np.asarray(image.convert("RGB"))
        for x, y, w, h in face_boxes(rgb):
            crop = image.crop((x, y, x + w, y + h))
            small = crop.resize((max(1, w // 12), max(1, h // 12)), Image.Resampling.BILINEAR)
            image.paste(small.resize((w, h), Image.Resampling.NEAREST), (x, y))
            faces += 1
    if mirror:
        image = ImageOps.mirror(image)

    output = BytesIO()
    fmt = Image.open(BytesIO(data)).format or "JPEG"
    if fmt.upper() in ("JPG", "JPEG"):
        image.convert("RGB").save(output, format="JPEG", quality=94)
        ext = ".jpg"
    elif fmt.upper() == "PNG":
        image.save(output, format="PNG")
        ext = ".png"
    elif fmt.upper() == "WEBP":
        image.save(output, format="WEBP", quality=94)
        ext = ".webp"
    else:
        image.convert("RGB").save(output, format="JPEG", quality=94)
        ext = ".jpg"
    return output.getvalue(), ext, faces


class BrowserWorker(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.commands: queue.Queue = queue.Queue()
        self.events: queue.Queue = queue.Queue()

    def emit(self, kind: str, value=None):
        self.events.put((kind, value))

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
                                context.close()
                            APP_DIR.mkdir(parents=True, exist_ok=True)
                            context = playwright.chromium.launch_persistent_context(
                                str(APP_DIR / "browser"), channel="msedge", headless=False, viewport=None
                            )
                            page = context.pages[0] if context.pages else context.new_page()
                            page.goto(command[1], wait_until="domcontentloaded", timeout=60000)
                            login_hint = (
                                "已检测到淘宝登录信息，可以扫描图片。"
                                if taobao_login_present(context)
                                else "未检测到淘宝登录信息，请在弹出的 Edge 页面登录后再扫描。"
                            )
                            self.emit("opened", f"浏览器已打开。{login_hint}")
                        elif kind == "scan":
                            if not page:
                                raise RuntimeError("请先打开商品链接。")
                            require_taobao_login(context)
                            images, warning = scan_product_page(page)
                            self.emit("scanned", (images, warning))
                        elif kind == "diagnose":
                            if not page:
                                raise RuntimeError("请先打开商品链接。")
                            require_taobao_login(context)
                            if urllib.parse.urlparse(page.url).hostname == "login.taobao.com":
                                raise RuntimeError("当前仍在淘宝登录页，请在 Edge 完成登录并确认商品页已打开。")
                            path = export_page_structure(page)
                            self.emit("diagnosed", str(path))
                        elif kind == "download":
                            if not context:
                                raise RuntimeError("请先打开商品链接。")
                            require_taobao_login(context)
                            self.download(context, page.url, *command[1:])
                    except Exception as exc:
                        self.emit("error", str(exc))
        except Exception as exc:
            self.emit("error", f"浏览器启动失败：{exc}")
        finally:
            if context:
                try:
                    context.close()
                except Exception:
                    pass
            self.emit("stopped")

    def download(self, context, referer: str, images, output_dir: Path, mirror: bool, mosaic: bool):
        output_dir.mkdir(parents=True, exist_ok=True)
        run_dir = Path(tempfile.mkdtemp(prefix=f"{datetime.now():%Y%m%d_%H%M%S}_", dir=output_dir))
        originals = run_dir / "下载原图"
        processed = run_dir / "处理后"
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
            try:
                response = context.request.get(
                    item.url, timeout=30000, headers={"Referer": referer}
                )
                http_status = response.status
                content_type = response.headers.get("content-type", "").split(";")[0]
                if not response.ok:
                    raise RuntimeError(f"HTTP {response.status}")
                stage = "读取图片内容"
                data = response.body()
                if not content_type.startswith("image/"):
                    raise RuntimeError(f"服务器返回的不是图片（Content-Type: {content_type or '未知'}）")
                ext = mimetypes.guess_extension(content_type) or Path(urllib.parse.urlparse(item.url).path).suffix or ".jpg"
                if ext == ".jpe":
                    ext = ".jpg"
                counts[item.category] += 1
                name = f"{item.category}_{counts[item.category]:03d}"
                stage = "保存原图"
                (originals / f"{name}{ext}").write_bytes(data)
                if mirror or mosaic:
                    stage = "镜像和人脸处理"
                    transformed, out_ext, faces = process_image(data, mirror, mosaic)
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
                self.emit("progress", f"{index}/{len(images)}：{stage}失败（{exc}）")
                continue
            self.emit("progress", f"已处理 {index}/{len(images)} 张，识别到 {face_total} 张脸")
        failure_log = ""
        if failure_details:
            failure_path = run_dir / "下载失败记录.txt"
            try:
                with failure_path.open("w", encoding="utf-8-sig", newline="") as log_file:
                    writer = csv.writer(log_file, delimiter="\t", lineterminator="\n")
                    writer.writerow(["序号", "类型", "失败阶段", "HTTP状态", "Content-Type", "错误类型", "失败原因", "图片地址"])
                    writer.writerows(failure_details)
                failure_log = str(failure_path)
            except OSError as exc:
                self.emit("progress", f"无法保存失败详情：{exc}")
        self.emit("complete", (ok, failed, face_total, str(run_dir), failure_log))


class App:
    COLORS = {
        "canvas": "#F2F5F6",
        "surface": "#FFFFFF",
        "ink": "#1D3442",
        "muted": "#657982",
        "line": "#D9E2E5",
        "hero": "#173746",
        "accent": "#F0522B",
        "accent_active": "#D94120",
        "teal": "#147A72",
        "warning": "#A96015",
        "error": "#B84236",
    }

    def __init__(self, root: Tk):
        self.root = root
        self.root.title("淘宝图片助手")
        width = min(1060, max(760, root.winfo_screenwidth() - 80))
        height = min(720, max(580, root.winfo_screenheight() - 100))
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(min(760, width), min(580, height))
        self.worker = BrowserWorker()
        self.worker.start()
        self.images: list[ProductImage] = []
        self.url = StringVar()
        self.output_dir = StringVar(value=str(Path.home() / "Pictures" / "淘宝图片"))
        self.mirror = BooleanVar(value=True)
        self.mosaic = BooleanVar(value=True)
        self.status = StringVar(value="粘贴商品链接，然后打开商品页。")
        self.main_count = StringVar(value="主图 0")
        self.detail_count = StringVar(value="详情图 0")
        self.busy = False
        self.page_open = False
        self.opened_input = ""
        self.opening_input = ""
        self.build_ui()
        self.root.after(150, self.poll_events)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def build_ui(self):
        c = self.COLORS
        self.root.configure(background=c["canvas"])
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        families = set(tkfont.families(self.root))
        family = next((name for name in ("Microsoft YaHei UI", "Segoe UI", "PingFang SC") if name in families), "TkDefaultFont")
        style.configure(".", font=(family, 10), foreground=c["ink"])
        style.configure("App.TFrame", background=c["canvas"])
        style.configure("Hero.TFrame", background=c["hero"])
        style.configure("Card.TFrame", background=c["surface"], bordercolor=c["line"], borderwidth=1, relief="solid")
        style.configure("HeroMark.TLabel", background=c["accent"], foreground="#FFFFFF", font=(family, 18, "bold"), padding=(11, 5))
        style.configure("HeroTitle.TLabel", background=c["hero"], foreground="#FFFFFF", font=(family, 18, "bold"))
        style.configure("HeroSub.TLabel", background=c["hero"], foreground="#C4D2D7", font=(family, 9))
        style.configure("HeroTag.TLabel", background="#244B59", foreground="#E2F0EF", font=(family, 9, "bold"), padding=(10, 7))
        style.configure("Section.TLabel", background=c["surface"], foreground=c["ink"], font=(family, 12, "bold"))
        style.configure("Hint.TLabel", background=c["surface"], foreground=c["muted"], font=(family, 9))
        style.configure("Field.TEntry", padding=(9, 8), fieldbackground="#FBFCFC", bordercolor=c["line"], lightcolor=c["line"], darkcolor=c["line"])
        style.configure("Primary.TButton", background=c["accent"], foreground="#FFFFFF", borderwidth=0, padding=(13, 9), font=(family, 9, "bold"))
        style.map("Primary.TButton", background=[("disabled", "#D8DEDF"), ("pressed", c["accent_active"]), ("active", "#F56B48")], foreground=[("disabled", "#89969A")])
        style.configure("Secondary.TButton", background="#EAF0F1", foreground=c["ink"], borderwidth=0, padding=(12, 8), font=(family, 9, "bold"))
        style.map("Secondary.TButton", background=[("disabled", "#EFF2F2"), ("pressed", "#DDE7E9"), ("active", "#DDE7E9")])
        style.configure("Quiet.TButton", background=c["surface"], foreground=c["muted"], borderwidth=0, padding=(8, 7))
        style.map("Quiet.TButton", background=[("disabled", c["surface"]), ("active", "#F3F6F6")])
        style.configure("Option.TCheckbutton", background=c["surface"], foreground=c["ink"], padding=(0, 4))
        style.map("Option.TCheckbutton", background=[("active", c["surface"])])
        style.configure("Count.TLabel", background="#EEF2F3", foreground=c["ink"], padding=(9, 5), font=(family, 9, "bold"))
        style.configure("Images.Treeview", background="#FFFFFF", fieldbackground="#FFFFFF", foreground=c["ink"], rowheight=30, borderwidth=0, font=(family, 9))
        style.configure("Images.Treeview.Heading", background="#F1F4F5", foreground=c["muted"], relief="flat", padding=(9, 8), font=(family, 9, "bold"))
        style.map("Images.Treeview", background=[("selected", "#DCECEF")], foreground=[("selected", c["ink"])])
        for name, bg, fg in (("Idle", "#EAF0F1", c["muted"]), ("Working", "#EAF1F8", "#315B83"), ("Ready", "#E8F1EC", c["teal"]), ("Done", "#E8F1EC", c["teal"]), ("Warning", "#F7EFE3", c["warning"]), ("Error", "#F8E9E7", c["error"])):
            style.configure(f"Status{name}.TFrame", background=bg)
            style.configure(f"Status{name}.TLabel", background=bg, foreground=fg, font=(family, 9))
            style.configure(f"Status{name}Dot.TLabel", background=bg, foreground=fg, font=(family, 10, "bold"))

        hero = ttk.Frame(self.root, style="Hero.TFrame", padding=(24, 18))
        hero.pack(fill="x")
        ttk.Label(hero, text="淘", style="HeroMark.TLabel").pack(side="left")
        brand = ttk.Frame(hero, style="Hero.TFrame")
        brand.pack(side="left", padx=(12, 0))
        ttk.Label(brand, text="淘宝图片助手", style="HeroTitle.TLabel").pack(anchor="w")
        ttk.Label(brand, text="主图与详情图下载 · 镜像 · 人脸马赛克", style="HeroSub.TLabel").pack(anchor="w", pady=(3, 0))
        ttk.Label(hero, text="●  本机处理", style="HeroTag.TLabel").pack(side="right")

        content = ttk.Frame(self.root, style="App.TFrame", padding=(22, 18, 22, 18))
        content.pack(fill="both", expand=True)
        content.columnconfigure(0, weight=1)
        content.rowconfigure(1, weight=1)

        top = ttk.Frame(content, style="App.TFrame")
        top.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        top.columnconfigure(0, weight=11, uniform="top")
        top.columnconfigure(1, weight=10, uniform="top")

        page_card = ttk.Frame(top, style="Card.TFrame", padding=17)
        page_card.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        ttk.Label(page_card, text="商品页面", style="Section.TLabel").pack(anchor="w")
        ttk.Label(page_card, text="打开商品页后，在 Edge 中完成淘宝登录。", style="Hint.TLabel").pack(anchor="w", pady=(4, 13))
        self.url_entry = ttk.Entry(page_card, textvariable=self.url, style="Field.TEntry")
        self.url_entry.pack(fill="x")
        self.url_entry.bind("<Return>", lambda _event: self.open_page())
        self.url.trace_add("write", lambda *_args: self.url_changed())
        page_actions = ttk.Frame(page_card, style="Card.TFrame")
        page_actions.pack(fill="x", pady=(12, 0))
        self.open_button = ttk.Button(page_actions, text="打开商品页", style="Primary.TButton", command=self.open_page)
        self.open_button.pack(side="left")
        self.scan_button = ttk.Button(page_actions, text="扫描图片", style="Secondary.TButton", command=self.scan)
        self.scan_button.pack(side="left", padx=(8, 0))
        self.diagnose_button = ttk.Button(page_actions, text="页面诊断", style="Quiet.TButton", command=self.diagnose)
        self.diagnose_button.pack(side="right")

        settings_card = ttk.Frame(top, style="Card.TFrame", padding=17)
        settings_card.grid(row=0, column=1, sticky="nsew", padx=(10, 0))
        ttk.Label(settings_card, text="保存与处理", style="Section.TLabel").pack(anchor="w")
        ttk.Label(settings_card, text="每次任务新建文件夹；原图和勾选的处理结果分开保存。", style="Hint.TLabel").pack(anchor="w", pady=(4, 10))
        folder_row = ttk.Frame(settings_card, style="Card.TFrame")
        folder_row.pack(fill="x")
        self.folder_entry = ttk.Entry(folder_row, textvariable=self.output_dir, style="Field.TEntry")
        self.folder_entry.pack(side="left", fill="x", expand=True)
        self.folder_button = ttk.Button(folder_row, text="选择…", style="Secondary.TButton", command=self.choose_folder)
        self.folder_button.pack(side="left", padx=(7, 0))
        options = ttk.Frame(settings_card, style="Card.TFrame")
        options.pack(fill="x", pady=(8, 5))
        self.mirror_option = ttk.Checkbutton(options, text="左右镜像", variable=self.mirror, style="Option.TCheckbutton", command=self.update_download_label)
        self.mirror_option.pack(side="left", padx=(0, 16))
        self.mosaic_option = ttk.Checkbutton(options, text="人脸打马赛克", variable=self.mosaic, style="Option.TCheckbutton", command=self.update_download_label)
        self.mosaic_option.pack(side="left")
        self.download_button = ttk.Button(settings_card, text="下载并处理图片", style="Primary.TButton", command=self.download)
        self.download_button.pack(fill="x", pady=(3, 0))
        self.update_download_label()

        results = ttk.Frame(content, style="Card.TFrame", padding=(16, 14, 16, 12))
        results.grid(row=1, column=0, sticky="nsew")
        results.columnconfigure(0, weight=1)
        results.rowconfigure(1, weight=1)
        result_header = ttk.Frame(results, style="Card.TFrame")
        result_header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(result_header, text="扫描结果", style="Section.TLabel").pack(side="left")
        counts = ttk.Frame(result_header, style="Card.TFrame")
        counts.pack(side="right")
        ttk.Label(counts, textvariable=self.main_count, style="Count.TLabel").pack(side="left", padx=(0, 6))
        ttk.Label(counts, textvariable=self.detail_count, style="Count.TLabel").pack(side="left")

        table = ttk.Frame(results, style="Card.TFrame")
        table.grid(row=1, column=0, sticky="nsew")
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(table, columns=("category", "url"), show="headings", selectmode="browse", style="Images.Treeview")
        self.tree.heading("category", text="图片类型")
        self.tree.heading("url", text="图片地址")
        self.tree.column("category", width=110, minwidth=90, stretch=False, anchor="w")
        self.tree.column("url", width=760, minwidth=280, stretch=True, anchor="w")
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self.refresh_controls())
        vertical = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(table, orient="horizontal", command=self.tree.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.empty_label = ttk.Label(table, text="扫描后，主图和详情图会显示在这里。", style="Hint.TLabel")
        self.empty_label.place(relx=0.5, rely=0.5, anchor="center")

        table_actions = ttk.Frame(results, style="Card.TFrame")
        table_actions.grid(row=2, column=0, sticky="ew", pady=(9, 0))
        ttk.Label(table_actions, text="选中图片后调整类型：", style="Hint.TLabel").pack(side="left", padx=(0, 7))
        self.main_button = ttk.Button(table_actions, text="设为主图", style="Quiet.TButton", command=lambda: self.set_category("主图"))
        self.main_button.pack(side="left", padx=(0, 5))
        self.detail_button = ttk.Button(table_actions, text="设为详情图", style="Quiet.TButton", command=lambda: self.set_category("详情图"))
        self.detail_button.pack(side="left")

        status_panel = ttk.Frame(content, style="StatusIdle.TFrame", padding=(12, 9))
        status_panel.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        self.status_dot = ttk.Label(status_panel, text="●", style="StatusIdleDot.TLabel")
        self.status_dot.pack(side="left", padx=(0, 8))
        self.status_label = ttk.Label(status_panel, textvariable=self.status, style="StatusIdle.TLabel", wraplength=900, justify="left")
        self.status_label.pack(side="left", fill="x", expand=True)
        self.status_panel = status_panel
        self.refresh_controls()

    def set_status(self, text: str, kind: str = "Idle"):
        self.status.set(text)
        self.status_panel.configure(style=f"Status{kind}.TFrame")
        self.status_dot.configure(style=f"Status{kind}Dot.TLabel")
        self.status_label.configure(style=f"Status{kind}.TLabel")

    def update_download_label(self):
        label = "下载并处理图片" if self.mirror.get() or self.mosaic.get() else "仅下载原图"
        self.download_button.configure(text=label)

    def refresh_controls(self):
        enabled = not self.busy
        same_product = self.page_open and self.url.get().strip() == self.opened_input
        self.open_button.configure(state="normal" if enabled else "disabled")
        self.url_entry.configure(state="normal" if enabled else "disabled")
        self.scan_button.configure(state="normal" if enabled and same_product else "disabled")
        self.diagnose_button.configure(state="normal" if enabled and same_product else "disabled")
        self.download_button.configure(state="normal" if enabled and same_product and self.images else "disabled")
        self.folder_button.configure(state="normal" if enabled else "disabled")
        self.folder_entry.configure(state="normal" if enabled else "disabled")
        self.mirror_option.configure(state="normal" if enabled else "disabled")
        self.mosaic_option.configure(state="normal" if enabled else "disabled")
        tree_state = "!disabled" if enabled else "disabled"
        self.tree.state([tree_state])
        has_selection = bool(self.tree.selection())
        self.main_button.configure(state="normal" if enabled and has_selection else "disabled")
        self.detail_button.configure(state="normal" if enabled and has_selection else "disabled")

    def url_changed(self):
        if self.page_open and self.url.get().strip() != self.opened_input and not self.busy:
            self.set_status("商品链接已修改。请重新打开商品页后再扫描或下载。", "Warning")
        self.refresh_controls()

    def replace_images(self, images: list[ProductImage]):
        self.images = images
        children = self.tree.get_children()
        if children:
            self.tree.delete(*children)
        main = detail = 0
        for index, item in enumerate(images):
            self.tree.insert("", "end", iid=str(index), values=(item.category, item.url))
            if item.category == "主图":
                main += 1
            else:
                detail += 1
        self.main_count.set(f"主图 {main}")
        self.detail_count.set(f"详情图 {detail}")
        if images:
            self.empty_label.place_forget()
        else:
            self.empty_label.place(relx=0.5, rely=0.5, anchor="center")
        self.refresh_controls()

    def open_page(self):
        if self.busy:
            return
        try:
            url = valid_product_url(self.url.get())
        except ValueError as exc:
            messagebox.showerror("链接无效", str(exc))
            return
        self.opening_input = self.url.get().strip()
        self.page_open = False
        self.replace_images([])
        self.busy = True
        self.set_status("正在打开 Edge 商品页…", "Working")
        self.refresh_controls()
        self.worker.commands.put(("open", url))

    def set_category(self, category: str):
        selection = self.tree.selection()
        if not selection or self.busy:
            return
        index = int(selection[0])
        self.images[index].category = category
        self.tree.item(selection[0], values=(category, self.images[index].url))
        self.replace_images(self.images)
        self.tree.selection_set(str(index))
        self.refresh_controls()

    def scan(self):
        if self.busy or not self.page_open:
            return
        self.replace_images([])
        self.busy = True
        self.set_status("正在读取商品主图和详情图…", "Working")
        self.refresh_controls()
        self.worker.commands.put(("scan",))

    def diagnose(self):
        if self.busy or not self.page_open:
            return
        self.busy = True
        self.set_status("正在导出页面结构（不含 Cookie 和图片地址）…", "Working")
        self.refresh_controls()
        self.worker.commands.put(("diagnose",))

    def download(self):
        if self.busy:
            return
        if not self.images:
            messagebox.showinfo("还没有图片", "请先打开商品页并扫描图片。")
            return
        raw_output = self.output_dir.get().strip()
        output_path = Path(raw_output).expanduser()
        if not raw_output or not output_path.is_absolute():
            messagebox.showerror("保存位置无效", "请选择一个完整的保存文件夹路径。")
            return
        self.busy = True
        self.set_status("正在下载并处理图片…", "Working")
        self.refresh_controls()
        self.worker.commands.put(("download", list(self.images), output_path, self.mirror.get(), self.mosaic.get()))

    def choose_folder(self):
        if self.busy:
            return
        selected = filedialog.askdirectory(initialdir=self.output_dir.get() or str(Path.home()))
        if selected:
            self.output_dir.set(selected)

    def poll_events(self):
        try:
            while True:
                kind, value = self.worker.events.get_nowait()
                if kind == "opened":
                    self.page_open = True
                    self.opened_input = self.opening_input
                    self.busy = False
                    self.set_status(value, "Ready")
                elif kind == "scanned":
                    images, warning = value
                    self.replace_images(images)
                    self.busy = False
                    message = f"扫描完成：找到 {len(self.images)} 张图片。"
                    if warning:
                        message += " " + warning + "。"
                    self.set_status(message, "Ready" if self.images else "Warning")
                elif kind == "progress":
                    self.set_status(value, "Working")
                elif kind == "complete":
                    ok, failed, faces, folder, failure_log = value
                    self.busy = False
                    self.set_status(f"完成：成功 {ok} 张，失败 {failed} 张，马赛克处理 {faces} 张脸。", "Warning" if failed else "Done")
                    details = f"\n失败详情：\n{failure_log}" if failure_log else ""
                    message = f"已保存到：\n{folder}\n\n成功 {ok} 张，失败 {failed} 张，识别人脸 {faces} 张。{details}"
                    (messagebox.showwarning if failed else messagebox.showinfo)("下载完成", message)
                elif kind == "diagnosed":
                    self.busy = False
                    self.set_status("页面诊断文件已导出。", "Done")
                    messagebox.showinfo(
                        "诊断已导出",
                        f"诊断文件已保存到：\n{value}\n\n文件不包含 Cookie、商品标题或图片地址。请把 JSON 文件发给开发者。",
                    )
                elif kind == "error":
                    self.busy = False
                    self.set_status(value, "Error")
                    messagebox.showerror("操作失败", value)
                elif kind == "stopped":
                    self.page_open = False
                    self.opened_input = ""
                    self.busy = False
                    self.set_status("浏览器服务已停止。重新打开商品页即可继续。", "Warning")
        except queue.Empty:
            pass
        self.refresh_controls()
        self.root.after(150, self.poll_events)

    def close(self):
        self.worker.commands.put(("quit",))
        self.root.destroy()


if __name__ == "__main__":
    root = Tk()
    App(root)
    root.mainloop()
