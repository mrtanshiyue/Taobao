from __future__ import annotations

import json
import queue
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from app import BrowserWorker, parse_product_urls, valid_product_url


PAGE = Path(__file__).with_name("index.html")


class LocalApp:
    def __init__(self):
        self.worker = BrowserWorker()
        self.worker.start()
        self.lock = threading.Lock()
        self.images = []
        self.busy = False
        self.page_open = False
        self.current_url = ""
        self.status = "粘贴商品链接，打开商品页后扫描图片。"
        self.status_kind = "idle"
        self.output_dir = str(Path.home() / "Pictures" / "淘宝图片")
        self.result = None

    def receive_worker_events(self):
        with self.lock:
            while True:
                try:
                    kind, value = self.worker.events.get_nowait()
                except queue.Empty:
                    break
                if kind == "opened":
                    self.page_open = True
                    self.busy = False
                    self.status = value
                    self.status_kind = "ready"
                elif kind == "scanned":
                    self.images, warning = value
                    self.busy = False
                    self.status = f"扫描完成：找到 {len(self.images)} 张图片。"
                    if warning:
                        self.status += " " + warning + "。"
                    self.status_kind = "ready" if self.images else "error"
                elif kind == "progress":
                    self.status = value
                    self.status_kind = "working"
                elif kind == "complete":
                    ok, failed, faces, folder, failure_log = value
                    self.busy = False
                    self.status = f"下载完成：成功 {ok} 张，失败 {failed} 张，马赛克处理 {faces} 张脸。"
                    if failure_log:
                        self.status += f" 失败详情：{failure_log}"
                    self.status_kind = "done"
                    self.result = {
                        "ok": ok,
                        "failed": failed,
                        "faces": faces,
                        "folder": folder,
                        "failure_log": failure_log,
                    }
                elif kind == "batch_complete":
                    self.busy = False
                    self.page_open = False
                    self.current_url = ""
                    self.status = (
                        f"批量完成：{len(value['products'])} 个商品，成功下载 {value['ok']} 张，"
                        f"图片失败 {value['failed']} 张，商品异常 {value['product_errors']} 个，"
                        f"识别人脸 {value['faces']} 张。"
                    )
                    self.status_kind = "error" if value["failed"] or value["product_errors"] else "done"
                    self.result = value
                elif kind == "error":
                    self.busy = False
                    self.status = value
                    self.status_kind = "error"
                elif kind == "stopped":
                    self.page_open = False
                    self.current_url = ""
                    self.busy = False

    def snapshot(self):
        self.receive_worker_events()
        with self.lock:
            return {
                "status": self.status,
                "status_kind": self.status_kind,
                "busy": self.busy,
                "page_open": self.page_open,
                "current_url": self.current_url,
                "images": [{"url": item.url, "category": item.category} for item in self.images],
                "output_dir": self.output_dir,
                "result": self.result,
            }

    def command(self, command, *args):
        with self.lock:
            if self.busy:
                raise ValueError("上一项操作还在进行，请稍候。")
            if command == "open":
                self.images = []
                self.page_open = False
                self.current_url = args[0]
            elif command == "scan":
                self.images = []
            elif command == "batch":
                self.images = []
                self.page_open = False
                self.current_url = ""
            self.busy = True
            self.status_kind = "working"
            self.status = {
                "open": "正在打开商品页…",
                "scan": "正在扫描页面图片…",
                "download": "正在下载和处理图片…",
                "batch": f"正在批量处理 {len(args[0])} 个商品…",
            }.get(command, "正在处理…")
            self.result = None
            self.worker.commands.put((command, *args))

    def set_category(self, index, category):
        with self.lock:
            if self.busy:
                raise ValueError("上一项操作还在进行，请稍候。")
            if type(index) is not int or not 0 <= index < len(self.images):
                raise ValueError("图片列表已变化，请重新扫描。")
            if category not in ("主图", "详情图"):
                raise ValueError("图片类型无效。")
            self.images[index].category = category


def serve():
    app = LocalApp()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format, *_args):
            pass

        def send_json(self, data, status=200):
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def local_request(self):
            host = self.headers.get("Host", "").split(":", 1)[0].strip("[]").lower()
            if self.client_address[0] not in ("127.0.0.1", "::1") or host not in ("127.0.0.1", "localhost"):
                self.send_json({"error": "只允许本机访问。"}, 403)
                return False
            return True

        def read_json(self):
            if self.headers.get_content_type() != "application/json":
                raise ValueError("请求格式无效。")
            size = int(self.headers.get("Content-Length", "0"))
            if size <= 0 or size > 1_000_000:
                raise ValueError("请求内容无效。")
            data = json.loads(self.rfile.read(size).decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("请求内容无效。")
            return data

        def do_GET(self):
            if not self.local_request():
                return
            if self.path == "/api/state":
                self.send_json(app.snapshot())
            elif self.path == "/":
                body = PAGE.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_json({"error": "页面不存在。"}, 404)

        def do_POST(self):
            if not self.local_request():
                return
            try:
                data = self.read_json()
                if self.path == "/api/open":
                    url = valid_product_url(str(data.get("url", "")))
                    app.command("open", url)
                    message = "正在打开商品页。"
                elif self.path == "/api/scan":
                    with app.lock:
                        if not app.page_open:
                            raise ValueError("请先打开商品页。")
                    app.command("scan")
                    message = "正在扫描页面图片。"
                elif self.path == "/api/category":
                    app.set_category(data.get("index"), data.get("category"))
                    message = "图片类型已更新。"
                elif self.path == "/api/download":
                    with app.lock:
                        if not app.page_open or not app.images:
                            raise ValueError("请先打开商品页并扫描到图片。")
                    output_dir = Path(str(data.get("output_dir", ""))).expanduser()
                    if not str(data.get("output_dir", "")).strip() or not output_dir.is_absolute():
                        raise ValueError("请输入完整的保存路径。")
                    mirror = data.get("mirror", True)
                    mosaic = data.get("mosaic", True)
                    if not isinstance(mirror, bool) or not isinstance(mosaic, bool):
                        raise ValueError("处理选项无效。")
                    with app.lock:
                        app.output_dir = str(output_dir)
                        images = list(app.images)
                    app.command("download", images, output_dir, mirror, mosaic)
                    message = "开始下载和处理图片。"
                elif self.path == "/api/batch":
                    urls = parse_product_urls(str(data.get("urls", "")))
                    output_dir = Path(str(data.get("output_dir", ""))).expanduser()
                    if not str(data.get("output_dir", "")).strip() or not output_dir.is_absolute():
                        raise ValueError("请输入完整的保存路径。")
                    mirror = data.get("mirror", True)
                    mosaic = data.get("mosaic", True)
                    if not isinstance(mirror, bool) or not isinstance(mosaic, bool):
                        raise ValueError("处理选项无效。")
                    with app.lock:
                        app.output_dir = str(output_dir)
                    app.command("batch", urls, output_dir, mirror, mosaic)
                    message = f"开始批量处理 {len(urls)} 个商品。"
                else:
                    self.send_json({"error": "操作不存在。"}, 404)
                    return
                self.send_json({"message": message}, 202)
            except (ValueError, json.JSONDecodeError, UnicodeError, OSError) as exc:
                self.send_json({"error": str(exc)}, 400)

    class LocalServer(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    server = LocalServer(("127.0.0.1", 0), Handler)
    address = f"http://127.0.0.1:{server.server_port}/"
    print(f"淘宝图片助手已启动：{address}")
    print("关闭此窗口即可停止本地服务。")
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        webbrowser.open(address)
        server_thread.join()
    except KeyboardInterrupt:
        server.shutdown()
    finally:
        app.worker.commands.put(("quit",))
        server.server_close()


if __name__ == "__main__":
    serve()
