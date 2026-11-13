"""用 OCR 模型识别课堂画面文字（讯飞 DeepSeek-OCR 等）。"""
from __future__ import annotations

import asyncio
import base64
import mimetypes
from pathlib import Path

from . import llm

OCR_PROMPT = "请识别图片中的所有文字，保持原有换行和符号，原样输出，不要解释，不要翻译。"


async def image_to_text(provider: dict, model: str, image_path: str,
                        prompt: str = OCR_PROMPT) -> str:
    p = Path(image_path)
    if not p.exists():
        return ""
    mime = mimetypes.guess_type(p.name)[0] or "image/jpeg"
    b64 = base64.b64encode(p.read_bytes()).decode()
    msgs = [{
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
        ],
    }]
    out = await llm.complete(provider, model, msgs, think=False, temperature=0,
                             max_tokens=1024, attempts=1, timeout=20)
    return (out or "").strip()


async def image_to_text_chain(provider: dict, models: list[str], image_path: str,
                              prompt: str = OCR_PROMPT) -> tuple[str, str, str]:
    """按顺序试 OCR 模型，单个最多等 8 秒，失败立刻换下一个。"""
    if not models:
        return "", "", "没有可用的 OCR 模型"
    errs: list[str] = []
    for model in models:
        try:
            text = await asyncio.wait_for(
                image_to_text(provider, model, image_path, prompt), timeout=8)
            if text:
                return text, model, ""
            errs.append(f"{model}: 空结果")
        except asyncio.TimeoutError:
            errs.append(f"{model}: 超时")
        except Exception as e:  # noqa: BLE001
            errs.append(f"{model}: {str(e)[:60]}")
    return "", "", " | ".join(errs[-3:])
