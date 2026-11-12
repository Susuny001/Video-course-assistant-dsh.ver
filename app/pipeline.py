"""分析流水线：音轨切块 → 语音识别 → 视觉纠错/双语 → 分段摘要 → 总览与落盘。"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from . import asr, db, demo, export, llm, media, prompts
from .config import (data_dir, get_provider, is_demo, load_settings, resolve_role,
                     video_dir)
from .refdoc import best_excerpt, load_text

CANCELED = "canceled"


class Canceled(Exception):
    pass


def _canceled(jid: str) -> bool:
    j = db.get_job(jid)
    return bool(j and j["status"] == CANCELED)


def _check(jid: str) -> None:
    if _canceled(jid):
        raise Canceled()


def _role(opts: dict, name: str) -> tuple[dict, str, bool]:
    cfg = (opts or {}).get(name) or {}
    if cfg.get("provider"):
        p = get_provider(cfg["provider"])
        if p:
            if cfg["provider"] == "demo" or not _needs_key(p) or (p.get("api_key") or "").strip():
                pool = (p.get("asr_models") if name == "asr" else p.get("models")) or p.get("models") or ["demo"]
                model = cfg.get("model") or pool[0]
                return p, model, is_demo(p)
    return resolve_role(name)


def _needs_key(p: dict) -> bool:
    return p.get("id") not in ("demo", "ollama")


def _chunks(duration: float, size: float) -> list[tuple[float, float]]:
    size = max(60.0, float(size or 300))
    out, t = [], 0.0
    while t < duration - 1:
        out.append((t, min(size, duration - t)))
        t += size
    return out or [(0.0, max(duration, 1.0))]


def _group_segments(cues: list[dict], target: float = 480.0, max_len: float = 900.0) -> list[dict]:
    if not cues:
        return []
    segs, cur = [], [cues[0]]
    for prev, c in zip(cues, cues[1:]):
        dur = c["end"] - cur[0]["start"]
        gap = c["start"] - prev["end"]
        if dur >= max_len or (dur >= target and gap >= 0.6):
            segs.append({"start": cur[0]["start"], "end": cur[-1]["end"], "cues": cur})
            cur = [c]
        else:
            cur.append(c)
    segs.append({"start": cur[0]["start"], "end": cur[-1]["end"], "cues": cur})
    return segs


def _apply_refine(cues: list[dict], data: dict) -> int:
    items = data.get("cues") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return 0
    by_i: dict[int, dict] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            by_i[int(it.get("i"))] = it
        except Exception:
            continue
    n = 0
    for i, c in enumerate(cues):
        it = by_i.get(i)
        if not it:
            continue
        text = (it.get("text") or "").strip()
        if text:
            c["text"] = text
            n += 1
        tr = (it.get("translation") or "").strip()
        if tr:
            c["translation"] = tr
    return n


def _gen_cover(title: str, out: Path) -> str | None:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return None
    W, H = 1280, 720
    img = Image.new("RGB", (W, H), (16, 22, 38))
    d = ImageDraw.Draw(img)
    for y in range(H):
        k = y / H
        d.line([(0, y), (W, y)], fill=(int(16 + 26 * k), int(22 + 30 * k), int(38 + 48 * k)))
    d.ellipse([W - 420, -180, W + 120, 360], fill=(38, 62, 110))
    d.ellipse([-160, H - 260, 320, H + 180], fill=(30, 48, 88))

    def font(size: int):
        for f in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyhbd.ttc",
                  "C:/Windows/Fonts/simhei.ttf"):
            try:
                return ImageFont.truetype(f, size)
            except Exception:
                continue
        return ImageFont.load_default()

    d.text((80, 210), "云课助手 · 课堂录音", font=font(34), fill=(140, 170, 220))
    # 标题自动换行
    f = font(58)
    words, line, lines = list(title), "", []
    for ch in words:
        if d.textlength(line + ch, font=f) > W - 200:
            lines.append(line)
            line = ch
        else:
            line += ch
    lines.append(line)
    y = 290
    for ln in lines[:3]:
        d.text((80, y), ln, font=f, fill=(240, 246, 255))
        y += 78
    d.polygon([(80, 560), (80, 640), (150, 600)], fill=(90, 150, 255))
    d.text((170, 580), "AUDIO COURSE", font=font(26), fill=(120, 150, 200))
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, quality=88)
    return str(out)


def _frames_for(video: dict, vdir: Path, chunk_start: float, chunk_len: float, n: int) -> list[str]:
    meta = video.get("meta") or {}
    if meta.get("media_kind") == "audio" or not meta.get("has_video"):
        return []
    src = meta.get("play_path") or video["path"]
    frames = []
    n = max(0, min(8, int(n)))
    for k in range(n):
        t = chunk_start + chunk_len * (k + 0.5) / max(1, n)
        fp = vdir / "frames" / f"c{int(chunk_start):06d}_{k}.jpg"
        got = media.extract_frame(src, t, str(fp))
        if got:
            frames.append(got)
    return frames


async def analyze(vid: str, jid: str, opts: dict | None = None) -> None:
    opts = opts or {}
    v = db.get_video(vid)
    if not v:
        db.update_job(jid, status="failed", message="课程不存在")
        return
    vdir = video_dir(vid)
    meta = dict(v.get("meta") or {})
    settings = load_settings()
    ui = settings.get("ui") or {}
    try:
        # ---------------- 参数
        chunk_sec = float(opts.get("chunk_seconds") or ui.get("asr_chunk_seconds") or 300)
        seg_target = float(opts.get("segment_minutes") or ui.get("segment_minutes") or 8) * 60
        translation_lang = opts.get("translation_lang", ui.get("translation_lang", "en"))
        term_policy = opts.get("term_policy", ui.get("term_policy", "keep_en"))
        glossary = (opts.get("glossary") or "").strip()
        frames_n = int(opts.get("frames_per_chunk", ui.get("frames_per_chunk", 4)))
        skip_refine = bool(opts.get("skip_refine"))
        ref_path = opts.get("reference_path") or meta.get("reference_path") or ""
        ref_text = load_text(ref_path) if ref_path else ""
        course = v["title"]

        asr_p, asr_m, asr_demo = _role(opts, "asr")
        ref_p, ref_m, ref_demo = _role(opts, "refine")
        txt_p, txt_m, txt_demo = _role(opts, "text")
        demo_mode = asr_demo and ref_demo

        db.update_job(jid, progress=1, message="读取媒体信息", log_line="开始分析")
        db.update_video(vid, status="analyzing")

        dur = float(v.get("duration") or 0)
        info = await asyncio.to_thread(media.probe, v["path"])
        if not dur:
            dur = info["duration"]
        meta.update({k: info[k] for k in ("width", "height", "vcodec", "acodec",
                                          "has_video", "has_audio", "format")})
        meta["media_kind"] = "video" if info["has_video"] else "audio"
        meta["reference_path"] = ref_path
        meta["analysis"] = {
            "asr": f"{asr_p['name']} / {asr_m}", "refine": f"{ref_p['name']} / {ref_m}",
            "text": f"{txt_p['name']} / {txt_m}", "demo": demo_mode,
            "translation_lang": translation_lang, "term_policy": term_policy,
        }
        db.update_video(vid, duration=dur, meta=meta)
        _check(jid)

        # ---------------- 转码 / 封面
        if meta["media_kind"] == "video":
            if not media.web_playable({"path": v["path"], "meta": meta}):
                db.update_job(jid, progress=3, message="转换为浏览器可播放的 MP4", log_line="开始转码")
                out = vdir / "play.mp4"
                if not out.exists():
                    await asyncio.to_thread(media.transcode_mp4, v["path"], str(out))
                meta["play_path"] = str(out)
                db.update_video(vid, meta=meta)
            if not meta.get("cover") or not Path(meta["cover"]).exists():
                cover = await asyncio.to_thread(media.make_cover, meta.get("play_path") or v["path"],
                                                str(vdir / "cover.jpg"), min(30.0, max(1.0, dur * 0.05)))
                if cover:
                    meta["cover"] = cover
                    db.update_video(vid, cover=cover, meta=meta)
        else:
            cover = meta.get("cover")
            if not cover or not Path(cover).exists():
                _gen_cover(course, vdir / "cover.jpg")
                meta["cover"] = str(vdir / "cover.jpg")
                db.update_video(vid, cover=meta["cover"], meta=meta)
        _check(jid)

        # ---------------- 音轨
        db.update_job(jid, progress=5, message="提取音轨", log_line="提取音轨")
        audio = vdir / "audio.mp3"
        if not audio.exists():
            await asyncio.to_thread(media.extract_audio, v["path"], str(audio))
        _check(jid)

        chunks = _chunks(dur, chunk_sec)
        all_cues: list[dict] = []
        chunk_ranges: list[tuple[int, int, float, float]] = []
        fail = 0
        for i, (cs, cl) in enumerate(chunks):
            _check(jid)
            part = vdir / "chunks" / f"chunk_{i:04d}.mp3"
            part.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(media.slice_audio, str(audio), cs, cl, str(part))
            before = len(all_cues)
            try:
                if asr_demo:
                    res = demo.demo_transcribe(ref_text, dur, cs, cl)
                else:
                    res = await asr.transcribe(
                        asr_p, asr_m, str(part),
                        language="" if ui.get("subtitle_lang") in ("auto", "") else ui.get("subtitle_lang", "zh"),
                        duration=cl, prompt=course,
                    )
                for s in res["segments"]:
                    t = (s.get("text") or "").strip()
                    if not t:
                        continue
                    st = round(cs + float(s["start"]), 2)
                    en = round(cs + float(s["end"]), 2)
                    if all_cues and t == all_cues[-1]["text"] and st - all_cues[-1]["end"] < 4:
                        continue
                    all_cues.append({
                        "start": st, "end": en,
                        "raw": t, "text": t, "translation": "",
                    })
            except Exception as e:  # noqa: BLE001
                fail += 1
                db.update_job(jid, log_line=f"第 {i+1} 段识别失败：{str(e)[:120]}")
            if len(all_cues) == before:
                all_cues.append({"start": cs, "end": min(dur, cs + cl), "raw": "",
                                 "text": f"（第 {i+1} 段未识别出内容）", "translation": ""})
            chunk_ranges.append((before, len(all_cues), cs, cl))
            pct = 5 + 45 * (i + 1) / len(chunks)
            db.update_job(jid, progress=round(pct, 1),
                          message=f"语音识别 {i+1}/{len(chunks)}",
                          log_line=f"识别完成 {i+1}/{len(chunks)}")
            db.replace_cues(vid, all_cues)

        if fail == len(chunks) and not asr_demo:
            raise RuntimeError("所有片段语音识别都失败了，请检查 API Key / 模型 / 网络后重试。")

        # ---------------- 视觉纠错 + 翻译
        collected_terms: list[str] = []
        if not skip_refine:
            for i, (a, b, cs, cl) in enumerate(chunk_ranges):
                _check(jid)
                part = all_cues[a:b]
                if not part:
                    continue
                frames = await asyncio.to_thread(_frames_for, v, vdir, cs, cl, frames_n)
                raw_join = "".join(c.get("raw") or "" for c in part)
                ref_excerpt = best_excerpt(ref_text, raw_join) if ref_text else ""
                try:
                    if ref_demo:
                        data = demo.demo_refine(part, translation_lang)
                    else:
                        ctx_before = "".join(c.get("text", "") for c in all_cues[max(0, a - 3):a])
                        ctx_after = "".join(c.get("text", "") for c in all_cues[b:b + 3])
                        msgs = prompts.refine_messages(
                            part, reference=ref_excerpt, glossary=glossary,
                            term_policy=term_policy, translation_lang=translation_lang,
                            context_before=ctx_before, context_after=ctx_after,
                            frames=frames, course=course,
                        )
                        out = await llm.complete(ref_p, ref_m, msgs, think=False,
                                                 temperature=0.2, json_mode=True,
                                                 max_tokens=min(12000, 140 * len(part) + 800))
                        data = llm.parse_json_loose(out)
                    n = _apply_refine(part, data)
                    for t in (data.get("terms") or []) if isinstance(data, dict) else []:
                        if isinstance(t, str) and t not in collected_terms:
                            collected_terms.append(t)
                    db.update_job(jid, log_line=f"纠错 {i+1}/{len(chunk_ranges)}（更新 {n} 句）")
                except Canceled:
                    raise
                except Exception as e:  # noqa: BLE001
                    db.update_job(jid, log_line=f"第 {i+1} 段纠错失败，保留原始转写：{str(e)[:120]}")
                pct = 50 + 33 * (i + 1) / max(1, len(chunk_ranges))
                db.update_job(jid, progress=round(pct, 1),
                              message=f"结合画面纠错 {i+1}/{len(chunk_ranges)}")
                db.replace_cues(vid, all_cues)
        else:
            db.update_job(jid, progress=83, message="跳过 AI 纠错")
        _check(jid)

        # ---------------- 分段 + 摘要
        db.update_job(jid, progress=84, message="切分课程段落")
        raw_segs = _group_segments(all_cues, seg_target)
        sem = asyncio.Semaphore(3)

        async def seg_info(s: dict, idx: int) -> dict:
            text = "".join(c.get("text", "") for c in s["cues"])
            info_d: dict = {}
            async with sem:
                try:
                    if txt_demo:
                        info_d = demo.demo_segment_info(text, s["start"], s["end"])
                    else:
                        out = await llm.complete(
                            txt_p, txt_m,
                            prompts.segment_messages(text, s["start"], s["end"], course),
                            think=False, temperature=0.3, json_mode=True, max_tokens=700,
                        )
                        info_d = llm.parse_json_loose(out)
                except Exception as e:  # noqa: BLE001
                    db.update_job(jid, log_line=f"第 {idx+1} 段摘要失败：{str(e)[:100]}")
            if not isinstance(info_d, dict) or not info_d.get("title"):
                info_d = demo.demo_segment_info(text, s["start"], s["end"])
            return {
                "start": s["start"], "end": s["end"],
                "title": str(info_d.get("title") or "")[:40],
                "abstract": str(info_d.get("abstract") or "")[:600],
                "keypoints": [str(x)[:80] for x in (info_d.get("keypoints") or [])][:6],
            }

        segments = await asyncio.gather(*[seg_info(s, i) for i, s in enumerate(raw_segs)])
        db.replace_segments(vid, segments)
        db.update_job(jid, progress=94, message="生成课程总览")
        _check(jid)

        # ---------------- 总览
        overview: dict = {}
        lines = "\n".join(f"- {s['title']}：{s['abstract']}" for s in segments)
        try:
            if txt_demo:
                overview = demo.demo_overview(segments)
            else:
                out = await llm.complete(txt_p, txt_m, prompts.overview_messages(lines, course),
                                         think=False, temperature=0.3, json_mode=True, max_tokens=1200)
                overview = llm.parse_json_loose(out)
        except Exception as e:  # noqa: BLE001
            db.update_job(jid, log_line=f"总览生成失败：{str(e)[:100]}")
        if not isinstance(overview, dict) or not overview.get("summary"):
            overview = demo.demo_overview(segments)
        if collected_terms:
            exist = {t.get("term") for t in overview.get("terms") or []}
            for t in collected_terms[:20]:
                if t not in exist:
                    overview.setdefault("terms", []).append({"term": t, "cn": ""})

        # ---------------- 落盘
        bilingual = bool(translation_lang and translation_lang != "none")
        meta["overview"] = overview
        meta["terms"] = [t for t in collected_terms]
        meta["analyzed_at"] = time.time()
        meta["demo_mode"] = demo_mode
        db.update_video(vid, status="ready", meta=meta)
        artifacts = export.write_all(vdir, {**v, "updated_at": time.strftime("%Y-%m-%d %H:%M")},
                                     segments, all_cues, overview, bilingual)
        meta["artifacts"] = artifacts
        db.update_video(vid, meta=meta)
        db.update_job(jid, progress=100, status="done", message="分析完成",
                      log_line="分析完成，字幕与笔记已保存")
    except Canceled:
        db.update_video(vid, status="new")
        db.update_job(jid, status=CANCELED, message="已取消")
    except Exception as e:  # noqa: BLE001
        db.update_video(vid, status="failed")
        db.update_job(jid, status="failed", message=f"分析失败：{str(e)[:300]}",
                      log_line=f"错误：{str(e)[:300]}")
