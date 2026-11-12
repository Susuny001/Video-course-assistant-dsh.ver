"""原生文件选择对话框（tkinter，后台线程调用）。"""
from __future__ import annotations

import threading


def pick_files(kind: str = "video", multiple: bool = True) -> list[str]:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception:
        return []

    result: list[str] = []

    def run():
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        if kind == "video":
            types = [("视频 / 音频", "*.mp4 *.mkv *.avi *.mov *.flv *.wmv *.webm *.ts *.m4v "
                                    "*.mp3 *.m4a *.wav *.aac *.flac *.ogg"),
                     ("所有文件", "*.*")]
            title = "选择课程视频或录音"
        elif kind == "text":
            types = [("文字稿", "*.md *.txt *.srt"), ("所有文件", "*.*")]
            title = "选择参考文字稿"
        else:
            types = [("所有文件", "*.*")]
            title = "选择文件"
        try:
            if multiple:
                files = filedialog.askopenfilenames(title=title, filetypes=types)
            else:
                f = filedialog.askopenfilename(title=title, filetypes=types)
                files = [f] if f else []
            result.extend([f for f in files if f])
        finally:
            root.destroy()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout=600)
    return result
