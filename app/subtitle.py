"""已有字幕（SRT）导入：给有 CC 的外语课用，省掉语音识别。"""
from __future__ import annotations

import re
from pathlib import Path


def _seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")[:3]) / 1000


def parse_srt(path: str) -> list[dict]:
    p = Path(path)
    raw = p.read_text("utf-8", errors="replace").lstrip("\ufeff")
    blocks = re.split(r"\n\s*\n", raw.replace("\r\n", "\n").replace("\r", "\n"))
    cues: list[dict] = []
    for block in blocks:
        lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        ti = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if ti is None:
            continue
        m = re.search(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)", lines[ti])
        if not m:
            continue
        st = _seconds(*m.groups()[:4])
        en = _seconds(*m.groups()[4:])
        text = " ".join(lines[ti + 1:]).strip()
        text = re.sub(r"<[^>]+>", "", text)
        text = re.sub(r"\{\\[^}]+\}", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            cues.append({"start": round(st, 2), "end": round(max(st + 0.3, en), 2), "text": text})
    return cues
