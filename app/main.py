"""应用入口：本地 HTTP 服务 + pywebview 原生窗口。"""
from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import db
from .api import router
from .config import VERSION, load_settings, web_dir

APP_TITLE = "云课助手 DSH"


def create_app() -> FastAPI:
    db.init()
    app = FastAPI(title=APP_TITLE, version=VERSION)
    app.include_router(router)
    web = web_dir()
    if web.exists():
        app.mount("/", StaticFiles(directory=str(web), html=True), name="web")
    return app


app = create_app()


def _free_port(preferred: int = 8765) -> int:
    for port in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue
    return preferred


def _wait_ready(port: int, timeout: float = 30) -> bool:
    import httpx
    end = time.time() + timeout
    while time.time() < end:
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/api/ping", timeout=1)
            if r.status_code == 200:
                return True
        except Exception:
            time.sleep(0.2)
    return False


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=APP_TITLE)
    ap.add_argument("--server", action="store_true", help="只启动服务，不打开窗口")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--browser", action="store_true", help="用系统浏览器打开")
    args = ap.parse_args(argv)

    port = _free_port(args.port)
    url = f"http://127.0.0.1:{port}"
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                         access_log=False, loop="asyncio")
    server = uvicorn.Server(cfg)

    if args.server:
        print(f"{APP_TITLE} v{VERSION} 已启动：{url}")
        server.run()
        return

    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    if not _wait_ready(port):
        print("服务启动失败，请查看控制台输出。")
        return

    if args.browser:
        import webbrowser
        webbrowser.open(url)
        print(f"{APP_TITLE} 运行在 {url}（关闭此窗口即退出）")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            return

    try:
        import webview
        load_settings()
        webview.create_window(APP_TITLE, url, width=1480, height=920,
                              min_size=(1080, 680), background_color="#0b0f17")
        webview.start(debug=False)
    except Exception as e:  # noqa: BLE001
        print(f"原生窗口不可用（{e}），已改用浏览器打开。")
        import webbrowser
        webbrowser.open(url)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            return
    finally:
        server.should_exit = True


if __name__ == "__main__":
    main(sys.argv[1:])
