"""ffmpeg / ffprobe 媒体处理：探测、抽音轨、切片、抽帧、转码。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from .config import app_root

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _find(name: str) -> str | None:
    local = app_root() / "bin" / f"{name}.exe"
    if local.exists():
        return str(local)
    local2 = app_root() / "bin" / name
    if local2.exists():
        return str(local2)
    return shutil.which(name)


FFMPEG = _find("ffmpeg")
FFPROBE = _find("ffprobe")


def available() -> bool:
    return bool(FFMPEG)


def run(args: list[str], timeout: int = 3600) -> subprocess.CompletedProcess:
    if not FFMPEG:
        raise RuntimeError("没有找到 ffmpeg，请运行 setup.bat 安装，或把 ffmpeg.exe 放进 bin 目录。")
    return subprocess.run(
        args, capture_output=True, timeout=timeout,
        creationflags=_CREATE_NO_WINDOW,
    )


def probe(path: str) -> dict:
    if not FFPROBE:
        raise RuntimeError("没有找到 ffprobe。")
    p = subprocess.run(
        [FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=_CREATE_NO_WINDOW,
    )
    try:
        info = json.loads(p.stdout or "{}")
    except Exception:
        info = {}
    fmt = info.get("format") or {}
    streams = info.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"
              and s.get("disposition", {}).get("attached_pic", 0) == 0), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = float(fmt.get("duration") or (v or {}).get("duration") or 0)
    return {
        "duration": duration,
        "size": int(fmt.get("size") or 0),
        "format": fmt.get("format_name", ""),
        "width": int((v or {}).get("width") or 0),
        "height": int((v or {}).get("height") or 0),
        "vcodec": (v or {}).get("codec_name", ""),
        "acodec": (a or {}).get("codec_name", ""),
        "has_video": bool(v),
        "has_audio": bool(a),
        "fps": _fps((v or {}).get("avg_frame_rate", "0/1")),
    }


def _fps(s: str) -> float:
    try:
        n, d = s.split("/")
        return round(float(n) / float(d), 3) if float(d) else 0
    except Exception:
        return 0


def web_playable(video: dict) -> bool:
    """浏览器/webview 友好的容器与编码。"""
    fmt = (video.get("meta", {}) or {}).get("format", "")
    path = video.get("path", "")
    ext = Path(path).suffix.lower()
    codec = (video.get("meta", {}) or {}).get("vcodec", "")
    if video.get("meta", {}).get("media_kind") == "audio":
        return ext in (".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".flac")
    return ext in (".mp4", ".m4v", ".webm") and codec in ("h264", "vp8", "vp9", "av1")


def extract_audio(src: str, out: str, bitrate: str = "48k", sr: int = 16000) -> str:
    """抽取单声道音轨，顺便做响度归一化：网课录音普遍偏小，不归一化语音识别会胡编。"""
    out_p = Path(out)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    p = run([FFMPEG, "-y", "-v", "error", "-i", src, "-vn", "-ac", "1", "-ar", str(sr),
             "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
             "-b:a", bitrate, str(out_p)])
    if p.returncode != 0:
        raise RuntimeError("提取音轨失败：" + p.stderr.decode("utf-8", "replace")[-800:])
    return str(out_p)


def slice_audio(src: str, start: float, duration: float, out: str) -> str:
    p = run([FFMPEG, "-y", "-v", "error", "-ss", f"{max(0, start):.3f}", "-t", f"{duration:.3f}",
             "-i", src, "-ac", "1", "-ar", "16000", "-b:a", "48k", out])
    if p.returncode != 0:
        raise RuntimeError("音频切片失败：" + p.stderr.decode("utf-8", "replace")[-800:])
    return out


def extract_frame(src: str, t: float, out: str, width: int = 960) -> str | None:
    out_p = Path(out)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    p = run([FFMPEG, "-y", "-v", "error", "-ss", f"{max(0, t):.3f}", "-i", src,
             "-frames:v", "1", "-vf", f"scale='min({width},iw)':-2", "-q:v", "4", str(out_p)])
    if p.returncode != 0 or not out_p.exists() or out_p.stat().st_size < 500:
        # 有些容器 seek 失败，退回到从头解码
        p = run([FFMPEG, "-y", "-v", "error", "-i", src, "-ss", f"{max(0, t):.3f}",
                 "-frames:v", "1", "-vf", f"scale='min({width},iw)':-2", "-q:v", "4", str(out_p)],
                timeout=900)
        if not out_p.exists() or out_p.stat().st_size < 500:
            return None
    return str(out_p)


def make_cover(src: str, out: str, at: float = 3.0, width: int = 640) -> str | None:
    if not extract_frame(src, at, out, width):
        return extract_frame(src, 0, out, width)
    return out


def transcode_mp4(src: str, out: str) -> str:
    p = run([FFMPEG, "-y", "-v", "error", "-i", src, "-c:v", "libx264", "-preset", "veryfast",
             "-crf", "23", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", out],
            timeout=7200)
    if p.returncode != 0:
        raise RuntimeError("转码失败：" + p.stderr.decode("utf-8", "replace")[-800:])
    return out


def make_silent_video(src_audio: str, out: str, cover: str | None, duration: float) -> str:
    """纯音频课程：生成一张封面 + 静音画面的 mp4，方便统一播放与抽帧。"""
    args = [FFMPEG, "-y", "-v", "error"]
    if cover and Path(cover).exists():
        args += ["-loop", "1", "-i", cover]
    else:
        args += ["-f", "lavfi", "-i", "color=c=0x111827:s=1280x720:d=1"]
    args += ["-i", src_audio, "-t", f"{max(1, duration):.3f}",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-r", "5",
             "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=0x111827",
             "-c:a", "aac", "-b:a", "128k", "-shortest", "-movflags", "+faststart", out]
    p = run(args, timeout=3600)
    if p.returncode != 0:
        raise RuntimeError("生成播放文件失败：" + p.stderr.decode("utf-8", "replace")[-800:])
    return out
