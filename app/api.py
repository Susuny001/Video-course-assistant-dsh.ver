"""HTTP API。"""
from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import re
import time
from pathlib import Path
from typing import AsyncIterator

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import (FileResponse, JSONResponse, PlainTextResponse, Response,
                               StreamingResponse)

from . import db, dialog, export, llm, media, pipeline, prompts, search
from .config import VERSION, get_provider, load_settings, save_settings, video_dir

router = APIRouter(prefix="/api")
_tasks: set[asyncio.Task] = set()


def _bg(coro) -> None:
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


def _video_or_404(vid: str) -> dict:
    v = db.get_video(vid)
    if not v:
        raise HTTPException(404, "课程不存在")
    return v


def _media_path(v: dict) -> str:
    p = (v.get("meta") or {}).get("play_path") or v["path"]
    if not Path(p).exists():
        raise HTTPException(404, "媒体文件已被移动或删除")
    return p


# ---------------------------------------------------------------- 基础
@router.get("/ping")
def ping():
    return {"ok": True, "version": VERSION, "ffmpeg": media.available()}


@router.get("/settings")
def get_settings():
    s = load_settings()
    return {"settings": s, "ffmpeg": media.available(), "version": VERSION}


@router.post("/settings")
def set_settings(patch: dict = Body(...)):
    s = save_settings(patch)
    return {"ok": True, "settings": s}


@router.post("/settings/test")
async def test_settings(body: dict = Body(...)):
    pid = body.get("provider", "")
    model = body.get("model", "")
    p = get_provider(pid)
    if not p:
        raise HTTPException(400, "服务商不存在")
    if not p.get("api_key") and pid != "ollama":
        return {"ok": False, "message": "还没有填写 API Key"}
    model = model or (p.get("models") or [""])[0]
    if not model:
        return {"ok": False, "message": "还没有填写模型名"}
    ok, msg = await llm.test_provider(p, model)
    return {"ok": ok, "message": msg}


# ---------------------------------------------------------------- 导入
@router.post("/import")
async def import_videos(body: dict = Body(default={})):
    paths = body.get("paths") or []
    if not paths:
        return {"ok": False, "message": "没有选择文件", "videos": []}
    added = []
    for p in paths:
        p = str(p)
        if not Path(p).exists():
            continue
        try:
            info = await asyncio.to_thread(media.probe, p)
        except Exception as e:  # noqa: BLE001
            added.append({"ok": False, "path": p, "error": str(e)})
            continue
        title = Path(p).stem.replace("_", " ")
        v = db.add_video(
            p, title, duration=info["duration"], width=info["width"], height=info["height"],
            size=info["size"], status="new",
            meta={"format": info["format"], "vcodec": info["vcodec"], "acodec": info["acodec"],
                  "has_video": info["has_video"], "has_audio": info["has_audio"],
                  "media_kind": "video" if info["has_video"] else "audio"},
        )
        added.append({"ok": True, "video": v})
    return {"ok": True, "videos": added}


@router.post("/import/dialog")
async def import_dialog(body: dict = Body(default={})):
    files = await asyncio.to_thread(dialog.pick_files, "video", True)
    if not files:
        return {"ok": False, "videos": [], "paths": []}
    return await import_videos({"paths": files})


@router.get("/videos")
def list_videos():
    return {"videos": db.list_videos()}


@router.get("/videos/{vid}")
def get_video(vid: str):
    v = _video_or_404(vid)
    return {
        "video": v,
        "segments": db.get_segments(vid),
        "cues": db.get_cues(vid),
        "chats": db.list_chats(vid),
        "overview": (v.get("meta") or {}).get("overview") or {},
    }


@router.delete("/videos/{vid}")
def delete_video(vid: str):
    _video_or_404(vid)
    db.delete_video(vid)
    return {"ok": True}


@router.post("/videos/{vid}/position")
def save_position(vid: str, body: dict = Body(...)):
    v = _video_or_404(vid)
    meta = dict(v.get("meta") or {})
    meta["position"] = float(body.get("position") or 0)
    db.update_video(vid, meta=meta)
    return {"ok": True}


# ---------------------------------------------------------------- 分析任务
@router.post("/videos/{vid}/analyze")
async def analyze(vid: str, opts: dict = Body(default={})):
    v = _video_or_404(vid)
    meta = dict(v.get("meta") or {})
    job = db.create_job(vid)
    _bg(pipeline.analyze(vid, job, opts))
    return {"ok": True, "job": job}


@router.get("/jobs/{jid}")
def get_job(jid: str):
    j = db.get_job(jid)
    if not j:
        raise HTTPException(404, "任务不存在")
    return j


@router.post("/jobs/{jid}/cancel")
def cancel_job(jid: str):
    db.update_job(jid, status="canceled", message="已取消")
    return {"ok": True}


@router.post("/videos/{vid}/transcode")
async def transcode(vid: str):
    v = _video_or_404(vid)
    job = db.create_job(vid, "transcode")
    _bg(_transcode(v, job))
    return {"ok": True, "job": job}


async def _transcode(v: dict, jid: str) -> None:
    try:
        meta = dict(v.get("meta") or {})
        db.update_job(jid, progress=5, message="转码中")
        out = video_dir(v["id"]) / "play.mp4"
        await asyncio.to_thread(media.transcode_mp4, v["path"], str(out))
        meta["play_path"] = str(out)
        db.update_video(v["id"], meta=meta)
        db.update_job(jid, progress=100, status="done", message="转码完成")
    except Exception as e:  # noqa: BLE001
        db.update_job(jid, status="failed", message=str(e)[:300])


# ---------------------------------------------------------------- 媒体流
def _iter_file(path: str, start: int, end: int, chunk: int = 1024 * 512) -> AsyncIterator[bytes]:
    async def gen():
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                data = f.read(min(chunk, left))
                if not data:
                    break
                left -= len(data)
                yield data
    return gen()


@router.api_route("/videos/{vid}/media", methods=["GET", "HEAD"])
def stream_media(vid: str, request: Request):
    v = _video_or_404(vid)
    path = _media_path(v)
    size = os.path.getsize(path)
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    if path.endswith(".mp3"):
        mime = "audio/mpeg"
    rng = request.headers.get("range") or request.headers.get("Range")
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-cache"}
    if rng:
        m = re.match(r"bytes=(\d*)-(\d*)", rng)
        if m:
            s = int(m.group(1) or 0)
            e = int(m.group(2)) if m.group(2) else size - 1
            s = max(0, min(s, size - 1))
            e = max(s, min(e, size - 1))
            headers.update({"Content-Range": f"bytes {s}-{e}/{size}",
                            "Content-Length": str(e - s + 1)})
            if request.method == "HEAD":
                return Response(status_code=206, headers=headers, media_type=mime)
            return StreamingResponse(_iter_file(path, s, e), status_code=206,
                                     media_type=mime, headers=headers)
    headers["Content-Length"] = str(size)
    if request.method == "HEAD":
        return Response(status_code=200, headers=headers, media_type=mime)
    return StreamingResponse(_iter_file(path, 0, size - 1), media_type=mime, headers=headers)


@router.get("/videos/{vid}/cover")
def cover(vid: str):
    v = _video_or_404(vid)
    p = (v.get("meta") or {}).get("cover")
    if p and Path(p).exists():
        return FileResponse(p, headers={"Cache-Control": "max-age=3600"})
    raise HTTPException(404, "没有封面")


@router.get("/videos/{vid}/frame")
async def frame(vid: str, t: float = 0):
    v = _video_or_404(vid)
    meta = v.get("meta") or {}
    if meta.get("media_kind") == "audio":
        raise HTTPException(404, "纯音频没有画面")
    src = meta.get("play_path") or v["path"]
    out = video_dir(vid) / "frames" / "onfly" / f"{int(t)}.jpg"
    if not out.exists():
        await asyncio.to_thread(media.extract_frame, src, t, str(out), 960)
    if not out.exists():
        raise HTTPException(404, "抽帧失败")
    return FileResponse(out, headers={"Cache-Control": "max-age=3600"})


# ---------------------------------------------------------------- 导出
@router.get("/videos/{vid}/export")
def export_file(vid: str, fmt: str = "srt"):
    v = _video_or_404(vid)
    vdir = video_dir(vid)
    bilingual = (load_settings().get("ui", {}).get("translation_lang") != "none")
    segs, cues = db.get_segments(vid), db.get_cues(vid)
    overview = (v.get("meta") or {}).get("overview") or {}
    safe = re.sub(r'[\\/:*?"<>|]', "_", v["title"])[:60]
    if fmt == "srt":
        p = vdir / f"{safe}.srt"
        p.write_text(export.build_srt(cues, bilingual), "utf-8")
        return FileResponse(p, filename=p.name, media_type="text/plain; charset=utf-8")
    if fmt == "md":
        p = vdir / f"{safe}.md"
        p.write_text(export.build_notes(v, segs, overview, cues, bilingual), "utf-8")
        return FileResponse(p, filename=p.name, media_type="text/markdown; charset=utf-8")
    return PlainTextResponse(
        json.dumps({"segments": segs, "cues": cues, "overview": overview},
                   ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{vid}.json"'},
    )


# ---------------------------------------------------------------- 关键词重点提取
@router.post("/videos/{vid}/keywords")
async def keywords(vid: str, body: dict = Body(...)):
    v = _video_or_404(vid)
    kws = body.get("keywords") or ""
    if isinstance(kws, str):
        kws = [k.strip() for k in re.split(r"[,，、;；\s]+", kws) if k.strip()]
    kws = [k for k in kws if k]
    if not kws:
        raise HTTPException(400, "请输入关键词")
    cues = db.get_cues(vid)
    segs = db.get_segments(vid)
    if not cues:
        raise HTTPException(400, "还没有字幕，请先分析课程")

    hits = search.keyword_hits(kws, cues, limit=40)
    used_ai = False
    pid = body.get("provider") or load_settings()["roles"].get("text", {}).get("provider", "")
    p = get_provider(pid)
    ai_ready = bool(p and (p.get("api_key") or "").strip())
    mid = load_settings()["roles"].get("text", {}).get("model")
    if body.get("use_ai", True) and ai_ready and hits:
        used_ai = True
        try:
            passages = [{"id": i, "t": round(h["start"]), "text": h["text"]} for i, h in enumerate(hits[:40])]
            out = await llm.complete(p, mid, prompts.keyword_messages(kws, passages),
                                     think=False, temperature=0.1, json_mode=True, max_tokens=1600)
            data = llm.parse_json_loose(out)
            ids = {int(x.get("id")): x.get("why", "") for x in data.get("hits", [])
                   if isinstance(x, dict) and str(x.get("id", "")).isdigit()}
            if ids:
                hits = [{**hits[i], "why": why} for i, why in ids.items() if 0 <= i < len(hits)]
        except Exception:  # noqa: BLE001
            used_ai = False
    if body.get("use_ai", True) and ai_ready and not hits and segs:
        # 字幕里没有字面命中时，让模型按语义从分段摘要里找
        used_ai = True
        try:
            passages = [{"id": i, "t": round(s["start"]), "text": f"{s['title']}：{s['abstract']}"}
                        for i, s in enumerate(segs)]
            out = await llm.complete(p, mid, prompts.keyword_messages(kws, passages),
                                     think=False, temperature=0.2, json_mode=True, max_tokens=1200)
            data = llm.parse_json_loose(out)
            for x in data.get("hits", []):
                try:
                    i = int(x.get("id"))
                except Exception:
                    continue
                if 0 <= i < len(segs):
                    s = segs[i]
                    hits.append({"start": s["start"], "end": s["end"], "text": s["abstract"],
                                 "why": x.get("why", "语义相关")})
        except Exception:  # noqa: BLE001
            used_ai = False

    for h in hits:
        seg = next((s for s in segs if s["start"] <= h["start"] <= s["end"]), None)
        h["segment"] = seg["title"] if seg else ""
    return {"ok": True, "keywords": kws, "hits": hits, "used_ai": used_ai}


# ---------------------------------------------------------------- 对话
@router.get("/videos/{vid}/chats")
def get_chats(vid: str):
    return {"chats": db.list_chats(vid)}


@router.post("/videos/{vid}/chats")
def create_chat(vid: str, body: dict = Body(default={})):
    _video_or_404(vid)
    cid = db.create_chat(vid, body.get("title") or "新对话")
    return {"ok": True, "chat_id": cid}


@router.get("/chats/{cid}")
def chat_detail(cid: str):
    rows = db.one("SELECT * FROM chats WHERE id=?", (cid,))
    if not rows:
        raise HTTPException(404, "对话不存在")
    return {"chat": rows, "messages": db.get_messages(cid)}


@router.delete("/chats/{cid}")
def remove_chat(cid: str):
    db.delete_chat(cid)
    return {"ok": True}


def _selection_context(vid: str, sel: dict | None) -> tuple[dict | None, list[str]]:
    if not sel:
        return None, []
    start, end = float(sel.get("start") or 0), float(sel.get("end") or 0)
    cues = [c for c in db.get_cues(vid) if c["end"] >= start - 0.5 and c["start"] <= end + 0.5]
    text = "\n".join(f"[{prompts.fmt_ts(c['start'])}] {c['text']}" for c in cues)
    return {"start": start, "end": end, "text": text}, [c["text"] for c in cues]


def _frames_in_range(v: dict, start: float, end: float, n: int = 4) -> list[str]:
    meta = v.get("meta") or {}
    if meta.get("media_kind") == "audio":
        return []
    src = meta.get("play_path") or v["path"]
    out_dir = video_dir(v["id"]) / "frames" / "sel"
    out_dir.mkdir(parents=True, exist_ok=True)
    frames = []
    span = max(1.0, end - start)
    for i in range(n):
        t = start + span * (i + 0.5) / n
        fp = out_dir / f"{int(start)}_{int(end)}_{i}.jpg"
        if fp.exists() or media.extract_frame(src, t, str(fp), 900):
            frames.append(str(fp))
    return frames


def _pick_context(question: str, cues: list[dict], max_chars: int = 18000) -> list[dict]:
    """按问题相关性挑字幕片段，避免长课程把后半段挤出上下文。"""
    if not cues:
        return []
    words = set(re.findall(r"[\u4e00-\u9fa5]{2}|[a-zA-Z]{3,}", question or ""))
    scored = []
    for i, c in enumerate(cues):
        t = c.get("text", "")
        s = sum(1 for w in words if w in t)
        if s:
            scored.append((s, i))
    if not scored:
        return cues[:120]
    scored.sort(reverse=True)
    keep: set[int] = set()
    for _s, i in scored[:12]:
        for j in range(max(0, i - 3), min(len(cues), i + 4)):
            keep.add(j)
    out, total = [], 0
    for i in sorted(keep):
        c = cues[i]
        line = f"[{prompts.fmt_ts(c['start'])}] {c['text']}"
        if total + len(line) > max_chars:
            break
        out.append(c)
        total += len(line) + 1
    return out


@router.post("/chat")
async def chat(body: dict = Body(...)):
    vid = body.get("video_id", "")
    v = _video_or_404(vid)
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "内容为空")
    cid = body.get("chat_id") or ""
    if not cid or not db.one("SELECT id FROM chats WHERE id=?", (cid,)):
        cid = db.create_chat(vid, text[:24] or "新对话")
    sel, _ = _selection_context(vid, body.get("selection"))
    chat_role = load_settings()["roles"].get("chat") or {}
    pid = body.get("provider") or chat_role.get("provider", "")
    p = get_provider(pid)
    model = body.get("model") or chat_role.get("model") or ((p or {}).get("models") or [""])[0]
    if not p or not model:
        raise HTTPException(400, "还没有可用的答疑模型，请先到设置里选择服务商和模型。")
    think = bool(body.get("think", False))
    segs = db.get_segments(vid)
    cues = db.get_cues(vid)
    ctx_cues = cues if sel else _pick_context(text, cues)
    system = prompts.chat_system(v["title"], segs, ctx_cues, sel,
                                 glossary=body.get("glossary") or "")
    history = db.get_messages(cid)[-12:]
    msgs = [{"role": "system", "content": system}]
    for m in history:
        if m["role"] in ("user", "assistant"):
            msgs.append({"role": m["role"], "content": m["content"]})
    user_msg = text
    if sel:
        user_msg += f"\n\n（我选中的片段：{prompts.fmt_ts(sel['start'])} - {prompts.fmt_ts(sel['end'])}）"
    db.add_message(cid, "user", text, {"selection": sel})

    if not p or not (p.get("api_key") or "").strip():
        raise HTTPException(400, "答疑模型还没有配置 API Key，请先到设置里填写。")
    frames: list[str] = []
    if body.get("include_frames") and sel:
        frames = await asyncio.to_thread(_frames_in_range, v, sel["start"], sel["end"], 4)
    if frames:
        msgs.append(llm.vision_message(user_msg, frames))
    else:
        msgs.append({"role": "user", "content": user_msg})

    async def gen():
        acc = []
        try:
            async for piece in llm.stream(p, model, msgs, think=think,
                                          temperature=float(body.get("temperature") or 0.4)):
                acc.append(piece)
                yield piece
        except Exception as e:  # noqa: BLE001
            err = f"\n\n> 调用失败：{str(e)[:300]}"
            acc.append(err)
            yield err
        finally:
            if acc:
                db.add_message(cid, "assistant", "".join(acc),
                               {"provider": pid, "model": model})

    headers = {"X-Chat-Id": cid, "Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    return StreamingResponse(gen(), media_type="text/plain; charset=utf-8", headers=headers)
