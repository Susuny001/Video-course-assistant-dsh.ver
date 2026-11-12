"""语音识别：OpenAI 兼容 /audio/transcriptions 接口。"""
from __future__ import annotations

import asyncio
import mimetypes
import re
from pathlib import Path

import httpx

from .config import is_demo


def _headers(provider: dict) -> dict:
    key = (provider.get("api_key") or "").strip()
    return {"Authorization": f"Bearer {key}"} if key else {}


def split_sentences(text: str) -> list[str]:
    text = (text or "").replace("\r", "\n").strip()
    if not text:
        return []
    parts: list[str] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        # 先按句末标点切，超长的再按逗号切
        for s in re.findall(r"[^。！？!?；;\n]+[。！？!?；;]?", line):
            s = s.strip()
            if not s:
                continue
            if len(s) > 90:
                buf = ""
                for piece in re.split(r"(?<=[，,、])", s):
                    if len(buf) + len(piece) > 80 and buf:
                        parts.append(buf)
                        buf = piece
                    else:
                        buf += piece
                if buf.strip():
                    parts.append(buf.strip())
            else:
                parts.append(s)
    return parts


def _timed_from_text(text: str, duration: float) -> list[dict]:
    sents = split_sentences(text)
    if not sents:
        return []
    total = sum(len(s) for s in sents) or 1
    out, t = [], 0.0
    for s in sents:
        d = duration * len(s) / total
        out.append({"start": round(t, 2), "end": round(min(duration, t + d), 2), "text": s})
        t += d
    return out


async def transcribe(provider: dict, model: str, path: str, *,
                     language: str = "", duration: float = 0.0,
                     prompt: str = "") -> dict:
    """返回 {text, language, segments:[{start,end,text}]}。"""
    if is_demo(provider):
        raise RuntimeError("demo-provider")

    p = Path(path)
    mime = mimetypes.guess_type(p.name)[0] or "audio/mpeg"
    url = provider["base_url"].rstrip("/") + "/audio/transcriptions"

    last_err = ""
    for fmt in ("verbose_json", "json"):
        data = {"model": model, "response_format": fmt, "temperature": "0"}
        if language and language != "auto":
            data["language"] = language
        if prompt:
            data["prompt"] = prompt[:800]
        try:
            async with httpx.AsyncClient(timeout=900) as client:
                with p.open("rb") as fh:
                    r = await client.post(
                        url, headers=_headers(provider), data=data,
                        files={"file": (p.name, fh, mime)},
                    )
            if r.status_code != 200:
                last_err = f"HTTP {r.status_code}: {r.text[:300]}"
                continue
            payload = r.json()
            text = (payload.get("text") or "").strip()
            segs = []
            for s in payload.get("segments") or []:
                t = (s.get("text") or "").strip()
                if not t:
                    continue
                segs.append({"start": float(s.get("start") or 0),
                             "end": float(s.get("end") or 0), "text": t})
            if not segs and text:
                segs = _timed_from_text(text, duration or 60)
            return {
                "text": text,
                "language": payload.get("language") or language or "",
                "segments": segs,
            }
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
            await asyncio.sleep(1.0)
    raise RuntimeError(f"语音识别失败：{last_err}")
