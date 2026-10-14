from __future__ import annotations

import html
import mimetypes
import queue
import tempfile
import threading
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path
from tkinter import BooleanVar, END, Listbox, StringVar, Tk, filedialog, messagebox
from tkinter import ttk

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


def scan_product_page(page) -> tuple[list[ProductImage], str]:
    detail_boundary = 1600
    detail_tab_found = False
    try:
        detail_tab = page.get_by_text("商品详情", exact=True).first
        box = detail_tab.bounding_box()
        if box:
            detail_boundary = box["y"] + page.evaluate("window.scrollY")
            detail_tab_found = True
        detail_tab.click(timeout=1200)
        page.wait_for_timeout(350)
    except Exception:
        pass

    found: dict[str, str] = {}
    max_scroll = 120000
    truncated = False
    frame_read_failed = False
    for position in range(0, max_scroll + 1, 750):
        page.evaluate("(position) => window.scrollTo(0, position)", position)
        page.wait_for_timeout(300)
        for frame in page.frames:
            try:
                frame_box = frame.frame_element().bounding_box() if frame != page.main_frame else None
                frame_offset = (frame_box["y"] + page.evaluate("window.scrollY")) if frame_box else 0
                images = frame.evaluate("""() => Array.from(document.images).map(img => ({
                    urls: [img.currentSrc, img.src, img.getAttribute('data-src'),
                           img.getAttribute('data-ks-lazyload'), img.getAttribute('data-lazyload'),
                           img.getAttribute('data-original'), img.getAttribute('data-lazy-src')],
                    width: img.naturalWidth || img.width,
                    height: img.naturalHeight || img.height,
                    y: img.getBoundingClientRect().top + window.scrollY
                }))""")
                for item in images:
                    if item["width"] < 60 or item["height"] < 60:
                        continue
                    for candidate in item["urls"]:
                        url = image_url(candidate)
                        if url:
                            absolute_y = item["y"] + frame_offset
                            category = "主图" if absolute_y < detail_boundary else "详情图"
                            found.setdefault(url, category)
                            break
            except Exception:
                frame_read_failed = True
                continue

        height = page.evaluate("Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)")
        if position >= height - 800:
            break
    else:
        truncated = True
    page.evaluate("window.scrollTo(0, 0)")
    warnings = []
    if truncated:
        warnings.append("页面达到扫描上限，可能还有图片未找到")
    if frame_read_failed:
        warnings.append("部分页面区域无法读取，结果可能不完整")
    if not detail_tab_found:
        warnings.append("未定位到商品详情分界线，图片分类为估算，可手动修正")
    return [ProductImage(url, category) for url, category in found.items()], "；".join(warnings)


def face_boxes(rgb: np.ndarray) -> list[tuple[int, int, int, int]]:
    # shortcut: Haar cascades can still miss occluded faces; use a trained detector for higher recall.
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    cascades = [
        cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml"),
        cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_profileface.xml"),
    ]
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
                            self.emit("opened", "浏览器已打开。请完成登录并确认商品页，再点“扫描图片”。")
                        elif kind == "scan":
                            if not page:
                                raise RuntimeError("请先打开商品链接。")
                            images, warning = scan_product_page(page)
                            self.emit("scanned", (images, warning))
                        elif kind == "download":
                            if not context:
                                raise RuntimeError("请先打开商品链接。")
                            self.download(context, *command[1:])
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

    def download(self, context, images, output_dir: Path, mirror: bool, mosaic: bool):
        output_dir.mkdir(parents=True, exist_ok=True)
        run_dir = Path(tempfile.mkdtemp(prefix=f"{datetime.now():%Y%m%d_%H%M%S}_", dir=output_dir))
        originals = run_dir / "下载原图"
        processed = run_dir / "处理后"
        originals.mkdir(parents=True, exist_ok=True)
        if mirror or mosaic:
            processed.mkdir(parents=True, exist_ok=True)
        ok = failed = face_total = 0
        counts = {"主图": 0, "详情图": 0}
        for index, item in enumerate(images, 1):
            try:
                response = context.request.get(item.url, timeout=30000)
                if not response.ok:
                    raise RuntimeError(f"HTTP {response.status}")
                data = response.body()
                content_type = response.headers.get("content-type", "").split(";")[0]
                if not content_type.startswith("image/"):
                    raise RuntimeError("服务器返回的不是图片")
                ext = mimetypes.guess_extension(content_type) or Path(urllib.parse.urlparse(item.url).path).suffix or ".jpg"
                if ext == ".jpe":
                    ext = ".jpg"
                counts[item.category] += 1
                name = f"{item.category}_{counts[item.category]:03d}"
                (originals / f"{name}{ext}").write_bytes(data)
                if mirror or mosaic:
                    transformed, out_ext, faces = process_image(data, mirror, mosaic)
                    (processed / f"{name}{out_ext}").write_bytes(transformed)
                    face_total += faces
                ok += 1
            except Exception as exc:
                failed += 1
                self.emit("progress", f"{index}/{len(images)}：跳过一张（{exc}）")
                continue
            self.emit("progress", f"已处理 {index}/{len(images)} 张，识别到 {face_total} 张脸")
        self.emit("complete", (ok, failed, face_total, str(run_dir)))


class App:
    def __init__(self, root: Tk):
        self.root = root
        self.root.title("淘宝图片助手")
        self.root.geometry("760x520")
        self.worker = BrowserWorker()
        self.worker.start()
        self.images: list[ProductImage] = []
        self.url = StringVar()
        self.output_dir = StringVar(value=str(Path.home() / "Pictures" / "淘宝图片"))
        self.mirror = BooleanVar(value=True)
        self.mosaic = BooleanVar(value=True)
        self.status = StringVar(value="粘贴商品链接，然后打开商品页。")
        self.build_ui()
        self.root.after(150, self.poll_events)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def build_ui(self):
        frame = ttk.Frame(self.root, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="淘宝商品链接").grid(row=0, column=0, sticky="w")
        ttk.Entry(frame, textvariable=self.url).grid(row=1, column=0, columnspan=3, sticky="ew", pady=(4, 12))
        ttk.Button(frame, text="打开商品页", command=self.open_page).grid(row=2, column=0, sticky="w")
        ttk.Button(frame, text="扫描图片", command=self.scan).grid(row=2, column=1, sticky="w", padx=8)
        ttk.Button(frame, text="下载并处理", command=self.download).grid(row=2, column=2, sticky="w")
        ttk.Label(frame, text="保存位置").grid(row=3, column=0, sticky="w", pady=(16, 0))
        ttk.Entry(frame, textvariable=self.output_dir).grid(row=4, column=0, columnspan=2, sticky="ew", pady=4)
        ttk.Button(frame, text="选择…", command=self.choose_folder).grid(row=4, column=2, sticky="e")
        options = ttk.Frame(frame)
        options.grid(row=5, column=0, columnspan=3, sticky="w", pady=8)
        ttk.Checkbutton(options, text="左右镜像", variable=self.mirror).pack(side="left", padx=(0, 18))
        ttk.Checkbutton(options, text="人脸打马赛克", variable=self.mosaic).pack(side="left")
        ttk.Label(frame, text="扫描到的图片（类型可手动修正）").grid(row=6, column=0, sticky="w", pady=(10, 4))
        self.listbox = Listbox(frame, height=10)
        self.listbox.grid(row=7, column=0, columnspan=3, sticky="nsew")
        category_buttons = ttk.Frame(frame)
        category_buttons.grid(row=8, column=0, columnspan=3, sticky="w", pady=(5, 0))
        ttk.Button(category_buttons, text="设为主图", command=lambda: self.set_category("主图")).pack(side="left", padx=(0, 8))
        ttk.Button(category_buttons, text="设为详情图", command=lambda: self.set_category("详情图")).pack(side="left")
        ttk.Label(frame, textvariable=self.status, wraplength=710).grid(row=9, column=0, columnspan=3, sticky="w", pady=(8, 0))
        frame.columnconfigure(0, weight=1)
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(7, weight=1)

    def open_page(self):
        try:
            url = valid_product_url(self.url.get())
        except ValueError as exc:
            messagebox.showerror("链接无效", str(exc))
            return
        self.status.set("正在打开浏览器…")
        self.worker.commands.put(("open", url))

    def set_category(self, category: str):
        selection = self.listbox.curselection()
        if not selection:
            messagebox.showinfo("先选择图片", "请先在列表中选择一张图片。")
            return
        index = selection[0]
        self.images[index].category = category
        self.listbox.delete(index)
        self.listbox.insert(index, f"{category}  {self.images[index].url}")
        self.listbox.selection_set(index)

    def scan(self):
        self.status.set("正在扫描页面，请稍候…")
        self.worker.commands.put(("scan",))

    def download(self):
        if not self.images:
            messagebox.showinfo("还没有图片", "请先打开商品页并扫描图片。")
            return
        self.status.set("正在下载和处理图片…")
        self.worker.commands.put(("download", list(self.images), Path(self.output_dir.get()), self.mirror.get(), self.mosaic.get()))

    def choose_folder(self):
        selected = filedialog.askdirectory(initialdir=self.output_dir.get() or str(Path.home()))
        if selected:
            self.output_dir.set(selected)

    def poll_events(self):
        try:
            while True:
                kind, value = self.worker.events.get_nowait()
                if kind == "opened":
                    self.status.set(value)
                elif kind == "scanned":
                    self.images, warning = value
                    self.listbox.delete(0, END)
                    for item in self.images:
                        self.listbox.insert(END, f"{item.category}  {item.url}")
                    message = f"扫描完成：找到 {len(self.images)} 张图片。"
                    if warning:
                        message += " " + warning + "。"
                    self.status.set(message)
                elif kind == "progress":
                    self.status.set(value)
                elif kind == "complete":
                    ok, failed, faces, folder = value
                    self.status.set(f"完成：成功 {ok} 张，失败 {failed} 张，马赛克处理 {faces} 张脸。")
                    messagebox.showinfo("下载完成", f"已保存到：\n{folder}\n\n成功 {ok} 张，失败 {failed} 张。")
                elif kind == "error":
                    self.status.set(value)
                    messagebox.showerror("操作失败", value)
        except queue.Empty:
            pass
        self.root.after(150, self.poll_events)

    def close(self):
        self.worker.commands.put(("quit",))
        self.root.destroy()


if __name__ == "__main__":
    root = Tk()
    App(root)
    root.mainloop()
