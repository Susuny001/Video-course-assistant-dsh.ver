"""OpenAI 兼容的大模型客户端（文本 / 视觉 / 流式 / 思考开关）。"""
from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import re
import time
from pathlib import Path
from typing import AsyncIterator

import httpx

from .config import model_concurrency, role_candidates

SYSTEM_ERROR = "模型调用失败"

_sems: dict[tuple[str, str], asyncio.Semaphore] = {}
USAGE: dict[str, dict[str, int]] = {}
DEAD: dict[str, str] = {}  # 余额不足/无权限这类错误，本次任务内不再重试
INFLIGHT: dict[str, int] = {}  # 每个模型当前在途请求数，用于把任务并行摊到所有免费模型
COOLDOWN: dict[str, float] = {}  # 429 后的短暂冷却，避免死磕一个模型
_rr = 0


def reset_usage() -> None:
    USAGE.clear()
    DEAD.clear()
    INFLIGHT.clear()
    COOLDOWN.clear()


def usage_snapshot() -> dict[str, dict[str, int]]:
    return {k: dict(v) for k, v in USAGE.items()}


def dead_models() -> dict[str, str]:
    return dict(DEAD)


def _bump(key: str, field: str) -> None:
    d = USAGE.setdefault(key, {"ok": 0, "fail": 0})
    d[field] = d.get(field, 0) + 1


def _sem(provider: dict, model: str) -> asyncio.Semaphore:
    """同一模型共用一个信号量，把并发压到额度上限，避免整批 429。"""
    key = ((provider or {}).get("id", ""), model or "")
    sem = _sems.get(key)
    if sem is None:
        sem = asyncio.Semaphore(model_concurrency(provider, model))
        _sems[key] = sem
    return sem


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
    if pid in ("siliconflow", "dashscope") and ("qwen3" in m or "deepseek-v3.1" in m):
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


def _clamp_max_tokens(provider: dict, model: str, n: int | None) -> int | None:
    """智谱的视觉模型输出上限是 1024，超了直接 400。"""
    if not n:
        return n
    if provider.get("id") == "zhipu" and ("4v" in (model or "").lower() or "vision" in (model or "").lower()):
        return min(n, 1024)
    return n


async def complete(provider: dict, model: str, messages: list[dict], *,
                   think: bool = False, temperature: float = 0.4,
                   max_tokens: int | None = None, timeout: float = 900,
                   json_mode: bool = False, attempts: int = 4) -> str:
    url = provider["base_url"].rstrip("/") + "/chat/completions"
    payload: dict = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "stream": False,
    }
    mt = _clamp_max_tokens(provider, model, max_tokens)
    if mt:
        payload["max_tokens"] = mt
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    payload.update(_reasoning_payload(provider, model, think))

    last_err = ""
    attempt = 0
    limit = max(1, attempts)
    while attempt < limit:
        attempt += 1
        try:
            async with _sem(provider, model):
                async with httpx.AsyncClient(timeout=timeout) as client:
                    r = await client.post(url, headers=_headers(provider), json=payload)
            if r.status_code == 200:
                data = r.json()
                ch = (data.get("choices") or [{}])[0] or {}
                content = (ch.get("message") or {}).get("content") or ""
                if content.strip():
                    return content
                # 有的模型/网关不支持 json_object 会回 200 + 空内容，去掉再试一次
                last_err = f"HTTP 200 空内容（finish_reason={ch.get('finish_reason')}）"
                if payload.pop("response_format", None) is not None:
                    attempt -= 1  # 这是同一模型的兼容重试，不占用失败次数
                    continue
                break
            last_err = f"HTTP {r.status_code}: {r.text[:400]}"
            if r.status_code == 429:
                if attempt >= limit:
                    break
                await asyncio.sleep(3.0 * attempt)
                continue
            if r.status_code == 400 and payload.pop("response_format", None) is not None:
                attempt -= 1  # 有些服务商不支持 json_object，退回普通输出再解析
                continue
            if r.status_code in (400, 401, 403, 404):
                break
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
        if attempt < limit:
            await asyncio.sleep(1.2 * attempt)
    raise RuntimeError(f"{SYSTEM_ERROR}：{last_err}")


def _mkey(provider: dict, model: str) -> str:
    return f"{(provider or {}).get('id', '')}/{model}"


def _label(provider: dict, model: str) -> str:
    return f"{(provider or {}).get('name') or (provider or {}).get('id')}/{model}"


def _load_ratio(provider: dict, model: str) -> float:
    cap = max(1, model_concurrency(provider, model))
    return (INFLIGHT.get(_mkey(provider, model), 0) + 1) / cap


def _pick_least_loaded(cands: list[tuple[dict, str]]) -> tuple[dict, str]:
    """在所有可用免费模型里挑当前负载最低的一个；同负载时轮换。"""
    global _rr
    _rr += 1
    if not cands:
        raise RuntimeError("没有可用模型")
    idx = _rr % len(cands)
    rotated = cands[idx:] + cands[:idx]
    return min(rotated, key=lambda pm: _load_ratio(pm[0], pm[1]))


async def complete_role(role: str, messages: list[dict], *, cycles: int = 2,
                        timeout: float = 120, **kw) -> str:
    """免费模型并行池：每个请求都分给当前最空闲的模型，而不是全挤在第一个模型上。

    同一批 16/32 个 worker 会同时落到 GLM-4-Flash、GLM-4.7-Flash、讯飞 4B、Groq Qwen/GPT-OSS 上，
    各模型按自己的并发上限吃满；某个模型 429 时只短暂冷却，任务立刻改派给其它空闲模型。
    """
    cands = role_candidates(role)
    if not cands:
        raise RuntimeError("没有可用模型，请到设置里配置 API Key 和模型。")
    errs: list[str] = []
    for _cycle in range(max(1, cycles)):
        pool = [(p, m) for p, m in cands if _mkey(p, m) not in DEAD]
        if not pool:
            break
        now = time.monotonic()
        ready = [c for c in pool if COOLDOWN.get(_mkey(*c), 0) <= now]
        if not ready:
            wait = min(COOLDOWN.get(_mkey(*c), now) for c in pool) - now
            await asyncio.sleep(max(0.2, min(3.0, wait)))
            ready = pool
        while ready:
            p, m = _pick_least_loaded(ready)
            ready.remove((p, m))
            key = _mkey(p, m)
            INFLIGHT[key] = INFLIGHT.get(key, 0) + 1
            try:
                out = await complete(p, m, messages, attempts=1, timeout=timeout, **kw)
                _bump(_label(p, m), "ok")
                return out
            except Exception as e:  # noqa: BLE001
                _bump(_label(p, m), "fail")
                msg = str(e)
                # 只有余额/权限/模型不存在才整场跳过；429 只是临时限流，冷却后还能用
                fatal = any(code in msg for code in ("HTTP 401", "HTTP 402",
                                                     "HTTP 403", "HTTP 404"))
                if fatal:
                    DEAD[key] = msg[:120]
                elif "HTTP 429" in msg:
                    COOLDOWN[key] = time.monotonic() + 15
                errs.append(f"{_label(p, m)}: {msg[:90]}")
            finally:
                INFLIGHT[key] = max(0, INFLIGHT.get(key, 0) - 1)
    if not errs and DEAD:
        raise RuntimeError("所有模型都不可用（余额/权限）：" + " | ".join(
            f"{k}: {v[:60]}" for k, v in DEAD.items()))
    raise RuntimeError("所有模型都失败：" + " | ".join(errs[-4:]))


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

    async with _sem(provider, model):
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


async def stream_role(role: str, messages: list[dict], *, preferred: tuple[dict, str] | None = None,
                      cycles: int = 2, **kw) -> AsyncIterator[str]:
    """流式问答的备用模型切换：连不上或首字都没出就换下一个。"""
    cands = role_candidates(role)
    if preferred:
        cands = [preferred] + [(p, m) for p, m in cands
                               if (p["id"], m) != (preferred[0]["id"], preferred[1])]
    if not cands:
        raise RuntimeError("没有可用模型，请到设置里配置。")
    errs: list[str] = []
    for _cycle in range(max(1, cycles)):
        for p, m in cands:
            started = False
            try:
                async for piece in stream(p, m, messages, **kw):
                    started = True
                    yield piece
                if started:
                    return
                errs.append(f"{p['name']}/{m}: 空响应")
            except Exception as e:  # noqa: BLE001
                if started:
                    raise
                errs.append(f"{p['name']}/{m}: {str(e)[:90]}")
    raise RuntimeError("所有模型都失败：" + " | ".join(errs[-3:]))


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
