"""OpenAI 兼容的大模型客户端（文本 / 视觉 / 流式 / 思考开关）。"""
from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import re
from pathlib import Path
from typing import AsyncIterator

import httpx

SYSTEM_ERROR = "模型调用失败"


def _headers(provider: dict) -> dict:
    h = {"Content-Type": "application/json"}
    key = (provider.get("api_key") or "").strip()
    if key:
        h["Authorization"] = f"Bearer {key}"
    if provider.get("id") == "openrouter":
        h["HTTP-Referer"] = "http://localhost"
        h["X-Title"] = "Video Course Assistant DSH"
    return h


def _reasoning_payload(provider: dict, model: str, think: bool) -> dict:
    pid = provider.get("id", "")
    m = (model or "").lower()
    extra: dict = {}
    if "qwen3" in m or "glm-4.5" in m or "deepseek-v3.1" in m:
        extra["enable_thinking"] = bool(think)
    if pid == "zhipu" and "glm-4.5" in m:
        extra["thinking"] = {"type": "enabled" if think else "disabled"}
    if re.match(r"^(o[1-9]|gpt-5)", m) or "deepseek-reasoner" in m:
        extra["reasoning_effort"] = "medium" if think else "low"
    return extra


def text_part(text: str) -> dict:
    return {"type": "text", "text": text}


def image_part(path: str) -> dict | None:
    p = Path(path)
    if not p.exists():
        return None
    mime = mimetypes.guess_type(p.name)[0] or "image/jpeg"
    b64 = base64.b64encode(p.read_bytes()).decode()
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}


def vision_message(text: str, frames: list[str], detail: str = "low") -> dict:
    content: list[dict] = [text_part(text)]
    for f in frames[:8]:
        part = image_part(f)
        if part:
            part["image_url"]["detail"] = detail
            content.append(part)
    return {"role": "user", "content": content}


async def complete(provider: dict, model: str, messages: list[dict], *,
                   think: bool = False, temperature: float = 0.4,
                   max_tokens: int | None = None, timeout: float = 900,
                   json_mode: bool = False) -> str:
    url = provider["base_url"].rstrip("/") + "/chat/completions"
    payload: dict = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "stream": False,
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    payload.update(_reasoning_payload(provider, model, think))

    last_err = ""
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.post(url, headers=_headers(provider), json=payload)
            if r.status_code == 200:
                data = r.json()
                return (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
            last_err = f"HTTP {r.status_code}: {r.text[:400]}"
            if r.status_code == 400 and payload.pop("response_format", None) is not None:
                continue  # 有些服务商不支持 json_object，退回普通输出再解析
            if r.status_code in (400, 401, 403, 404):
                break
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
        await asyncio.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{SYSTEM_ERROR}：{last_err}")


async def stream(provider: dict, model: str, messages: list[dict], *,
                 think: bool = False, temperature: float = 0.4,
                 max_tokens: int | None = None) -> AsyncIterator[str]:
    url = provider["base_url"].rstrip("/") + "/chat/completions"
    payload: dict = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "stream": True,
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens
    payload.update(_reasoning_payload(provider, model, think))

    async with httpx.AsyncClient(timeout=900) as client:
        async with client.stream("POST", url, headers=_headers(provider), json=payload) as r:
            if r.status_code != 200:
                body = (await r.aread()).decode("utf-8", "replace")
                raise RuntimeError(f"{SYSTEM_ERROR}：HTTP {r.status_code}: {body[:400]}")
            async for line in r.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except Exception:
                    continue
                delta = (obj.get("choices") or [{}])[0].get("delta") or {}
                piece = delta.get("content") or ""
                if piece:
                    yield piece


def parse_json_loose(text: str) -> dict:
    """从模型输出里尽量抠出 JSON。"""
    if not text:
        return {}
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except Exception:
        pass
    for pattern in (r"\{[\s\S]*\}", r"\[[\s\S]*\]"):
        m = re.search(pattern, text)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                continue
    return {}


async def test_provider(provider: dict, model: str) -> tuple[bool, str]:
    try:
        out = await complete(
            provider, model,
            [{"role": "user", "content": "只回复两个字：可用"}],
            temperature=0, max_tokens=16, timeout=40,
        )
        return True, (out or "").strip()[:60] or "连接成功"
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:300]
