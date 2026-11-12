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
                              prompt: str = OCR_PROMPT,
                              wait_all: float = 25.0) -> tuple[str, str, str]:
    """多个 OCR 模型同时抢答，谁先返回有效文字用谁，避免单个模型挂死拖住整批。"""
    if not models:
        return "", "", "没有可用的 OCR 模型"
    tasks = {asyncio.create_task(image_to_text(provider, m, image_path, prompt)): m
             for m in models}
    pending = set(tasks)
    errs: list[str] = []
    deadline = asyncio.get_running_loop().time() + wait_all
    while pending:
        left = deadline - asyncio.get_running_loop().time()
        if left <= 0:
            break
        done, pending = await asyncio.wait(pending, timeout=left,
                                           return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            model = tasks[t]
            try:
                text = t.result()
                if text:
                    for p in pending:
                        p.cancel()
                    return text, model, ""
                errs.append(f"{model}: 空结果")
            except Exception as e:  # noqa: BLE001
                errs.append(f"{model}: {str(e)[:60]}")
    for p in pending:
        p.cancel()
    return "", "", " | ".join(errs[-3:]) or "OCR 超时"
