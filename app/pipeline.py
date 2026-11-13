"""分析流水线：音轨切块 → 语音识别 → 视觉纠错/双语 → 分段摘要 → 总览与落盘。"""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

from . import asr, db, export, llm, media, ocr, prompts, subtitle
from .config import get_provider, load_settings, model_concurrency, resolve_role, video_dir

CANCELED = "canceled"


class Canceled(Exception):
    pass


def _canceled(jid: str) -> bool:
    j = db.get_job(jid)
    return bool(j and j["status"] == CANCELED)


def _check(jid: str) -> None:
    if _canceled(jid):
        raise Canceled()


def _role(opts: dict, name: str) -> tuple[dict, str]:
    cfg = (opts or {}).get(name) or {}
    if cfg.get("provider"):
        p = get_provider(cfg["provider"])
        if p:
            pool = (p.get("asr_models") if name == "asr" else p.get("models")) or p.get("models") or []
            model = cfg.get("model") or (pool[0] if pool else "")
            if model:
                return p, model
    return resolve_role(name)


def _merge_asr_segments(segs: list[dict], max_chars: int = 60, max_dur: float = 18.0) -> list[dict]:
    """把语音接口给出的碎句合并成适合做字幕的句子，减少后续模型调用次数。"""
    out: list[dict] = []
    cur: dict | None = None
    for s in segs:
        t = (s.get("text") or "").strip()
        if not t:
            continue
        st, en = float(s.get("start") or 0), float(s.get("end") or 0)
        if cur is None:
            cur = {"start": st, "end": en, "text": t}
            continue
        ended = bool(re.search(r"[。！？!?；;]$", cur["text"]))
        if (len(cur["text"]) >= max_chars or en - cur["start"] >= max_dur
                or (ended and len(cur["text"]) >= 22)):
            out.append(cur)
            cur = {"start": st, "end": en, "text": t}
        else:
            cur["text"] += t
            cur["end"] = en
    if cur:
        out.append(cur)
    return out


_STRONG_BAD = re.compile(
    r"(please\s+)?subscribe|thank you for watching|see you next time|amara\.org|"
    r"字幕由|点赞|订阅|小铃铛|PET2|https?://|www\.", re.I)


def _source_lang(cues: list[dict]) -> str:
    text = "".join(c.get("text", "") for c in cues[:300])
    cjk = len(re.findall(r"[\u4e00-\u9fa5]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    if cjk >= 10 and cjk >= latin * 0.25:
        return "zh"
    if latin >= 10:
        return "en"
    return "unknown"


def _is_bad_cue(text: str, src_lang: str) -> bool:
    t = (text or "").strip()
    if not t:
        return True
    if _STRONG_BAD.search(t):
        return True
    if src_lang == "zh":
        if not re.search(r"[\u4e00-\u9fa5]", t) and len(t) >= 24:
            return True
        if re.fullmatch(r"[\d\-_./: ]{6,}", t):
            return True
    return False


def _asr_parallel(p: dict, model: str, n_chunks: int) -> int:
    """语音识别的并发上限同样按模型额度来，不超发。"""
    return max(1, min(model_concurrency(p, model), n_chunks))


def _cues_from_srt(path: str) -> list[dict]:
    merged = _merge_asr_segments(subtitle.parse_srt(path))
    return [{"start": round(s["start"], 2), "end": round(s["end"], 2),
             "raw": s["text"], "text": s["text"], "translation": ""} for s in merged]


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


def _slice_by_cuts(cues: list[dict], cuts: list[int]) -> list[dict]:
    """按字幕序号切段，过短的并入前一段，过长的在中间分开。"""
    out: list[dict] = []
    for a, b in zip(cuts, cuts[1:]):
        if b <= a:
            continue
        part = cues[a:b]
        dur = part[-1]["end"] - part[0]["start"]
        if out and dur < 90:
            prev = out[-1]
            prev["cues"].extend(part)
            prev["end"] = prev["cues"][-1]["end"]
            continue
        out.append({"start": part[0]["start"], "end": part[-1]["end"], "cues": part})
    # 单个主题讲太久时，从中间找最大的句间停顿再分一次
    final: list[dict] = []
    for seg in out:
        while seg["end"] - seg["start"] > 1800 and len(seg["cues"]) > 8:
            cs = seg["cues"]
            mid = seg["start"] + (seg["end"] - seg["start"]) / 2
            best_i, best_gap = len(cs) // 2, -1.0
            for i in range(2, len(cs) - 2):
                if abs(cs[i]["start"] - mid) > 300:
                    continue
                gap = cs[i]["start"] - cs[i - 1]["end"]
                if gap > best_gap:
                    best_gap, best_i = gap, i
            final.append({"start": cs[0]["start"], "end": cs[best_i - 1]["end"], "cues": cs[:best_i]})
            seg = {"start": cs[best_i]["start"], "end": cs[-1]["end"], "cues": cs[best_i:]}
        final.append(seg)
    return final


async def _plan_segments(cues: list[dict], txt_p: dict, txt_m: str,
                         course: str, jid: str) -> list[dict]:
    """让文本模型按教学内容判断主题边界，而不是按固定时长切。"""
    n = len(cues)
    if n == 0:
        return []
    win = 40
    ranges = [(i, min(i + win, n)) for i in range(0, n, win)]
    sem = asyncio.Semaphore(8)

    async def one(w: int) -> int | None:
        a, b = ranges[w]
        part = [{"i": j, **c} for j, c in enumerate(cues[a:b], start=a)]
        prev_tail = "".join(c["text"] for c in cues[max(0, a - 3):a])
        async with sem:
            try:
                out = await llm.complete_role(
                    "text", prompts.segment_plan_messages(part, prev_tail, course),
                    think=False, temperature=0.1, json_mode=True, max_tokens=800,
                )
                data = llm.parse_json_loose(out)
                k = data.get("new_topic_start") if isinstance(data, dict) else None
                if k is None:
                    return None
                k = int(k)
                if a <= k < b and k > 0:
                    return k
            except Exception as e:  # noqa: BLE001
                db.update_job(jid, log_line=f"第 {w+1} 个窗口主题判断失败：{str(e)[:90]}")
        return None

    results = await asyncio.gather(*[one(i) for i in range(len(ranges))])
    cuts = sorted({0, n, *[k for k in results if k]})

    if len(cuts) <= 2 and (cues[-1]["end"] - cues[0]["start"]) > 900:
        # 模型没给出任何主题边界时退回到长停顿切分，避免整节课变成一段
        fallback = _group_segments(cues, target=600, max_len=1200)
        db.update_job(jid, log_line="模型未识别出主题边界，使用停顿切分兜底")
        return fallback
    return _slice_by_cuts(cues, cuts)


def _local_segment_info(text: str) -> dict:
    """模型失败时的兜底：只做摘录，不编造内容。"""
    sents = [s.strip() for s in re.split(r"(?<=[。！？!?；;])", text or "") if s.strip()]
    first = (sents[0] if sents else (text or ""))[:20].strip("，。；：、 ")
    return {
        "title": first or "课堂片段",
        "abstract": "".join(sents[:4])[:150] or (text or "")[:150],
        "keypoints": [s.strip("，。； ")[:34] for s in sents[1:5] if 8 <= len(s) <= 34],
    }


def _looks_latin(s: str) -> bool:
    s = s or ""
    return len(re.findall(r"[A-Za-z]", s)) >= 3 and not re.search(r"[\u4e00-\u9fa5]", s)


def _looks_cjk(s: str) -> bool:
    s = s or ""
    cjk = len(re.findall(r"[\u4e00-\u9fa5]", s))
    latin = len(re.findall(r"[A-Za-z]", s))
    return cjk >= 2 and cjk > latin


def _salvage_cues(raw: str) -> dict:
    """模型输出被截断时，把已经完整的 cue 对象抠出来，能救多少救多少。"""
    items: list[dict] = []
    for m in re.finditer(r'\{\s*"i"\s*:\s*\d+[\s\S]*?\}\s*(?=[,}\]])', raw or ""):
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict) and "i" in obj:
                items.append(obj)
        except Exception:
            continue
    return {"cues": items} if items else {}


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
    if cues and 0 not in by_i and 1 in by_i and len(by_i) == len(cues):
        by_i = {k - 1: v for k, v in by_i.items()}  # 模型从 1 开始编号时兜底
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


# ---------------------------------------------------------------- 实时流水线状态
LIVE: dict[str, "_Live"] = {}
FOCUS: dict[str, float] = {}


def set_focus(vid: str, t: float) -> None:
    """用户拖到哪，后面的识别 / OCR / 修正就优先做哪。"""
    FOCUS[vid] = max(0.0, float(t or 0))
    for st in LIVE.values():
        if st.vid == vid:
            st.focus = FOCUS[vid]


def snapshot(jid: str) -> dict | None:
    st = LIVE.get(jid)
    return st.snapshot() if st else None


def _merge_ranges(ranges: list[tuple[float, float]]) -> list[list[float]]:
    rs = sorted((float(a), float(b)) for a, b in ranges if b > a)
    out: list[list[float]] = []
    for a, b in rs:
        if out and a <= out[-1][1] + 0.5:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def _pick(units: list[dict], focus: float) -> dict | None:
    """优先级：正在看的这一段 > 后面的（由近到远）> 前面的（由近到远）。"""
    best, best_key = None, None
    for u in units:
        if u.get("status") != "pending":
            continue
        s, e = u["start"], u["end"]
        if s <= focus < e:
            key = (0, 0.0)
        elif s >= focus:
            key = (1, s - focus)
        else:
            key = (2, focus - e)
        if best_key is None or key < best_key:
            best, best_key = u, key
    return best


class _Live:
    """一节课的实时进度：三条线各自的已完成时间区间。"""

    def __init__(self, jid: str, vid: str, dur: float, ocr_ready: bool):
        self.jid, self.vid = jid, vid
        self.dur = max(1.0, float(dur or 0))
        self.ocr_ready = ocr_ready
        self.focus = 0.0
        self.asr_units: list[dict] = []
        self.ocr_units: list[dict] = []
        self.fix_units: list[dict] = []
        self.tr_units: list[dict] = []
        self.asr_done: list[tuple[float, float]] = []
        self.ocr_done: list[tuple[float, float]] = []
        self.fix_done: list[tuple[float, float]] = []
        self.cues: list[dict] = []
        self.terms: list[str] = []
        self.source_lang = ""
        self.last_pub = 0.0
        self.last_db = 0.0

    def add_cues(self, cues: list[dict]) -> None:
        self.cues.extend(cues)
        self.cues.sort(key=lambda c: c["start"])

    def ordered_cues(self) -> list[dict]:
        return sorted(self.cues, key=lambda c: c["start"])

    def make_batches(self, cues: list[dict], size: int, skip_fix: bool = False) -> None:
        for i in range(0, len(cues), size):
            part = cues[i:i + size]
            if not part:
                continue
            s, e = part[0]["start"], part[-1]["end"]
            ocr_u = {"start": s, "end": e, "cues": part, "screen": "",
                     "status": "pending" if self.ocr_ready else "done"}
            if self.ocr_ready:
                self.ocr_units.append(ocr_u)
            else:
                self.ocr_done.append((s, e))
            if not skip_fix:
                self.fix_units.append({"start": s, "end": e, "cues": part,
                                       "status": "pending", "ocr": ocr_u})

    def ctx_before(self, unit: dict, n: int = 8) -> str:
        prev = [c.get("text", "") for c in self.cues if c["end"] <= unit["start"] + 0.2]
        return "".join(prev[-n:])

    def ctx_after(self, unit: dict, n: int = 3) -> str:
        nxt = [c.get("text", "") for c in self.cues if c["start"] >= unit["end"] - 0.2]
        return "".join(nxt[:n])

    def ratio(self, ranges: list[tuple[float, float]]) -> float:
        return min(1.0, sum(b - a for a, b in _merge_ranges(ranges)) / self.dur)

    def gates_ok(self) -> bool:
        if self.ratio(self.asr_done) < 0.25:
            return False
        if not self.ocr_ready or self.ocr_all_done():
            return True
        return self.ratio(self.ocr_done) >= 0.25

    def asr_all_done(self) -> bool:
        return all(u["status"] in ("done", "failed") for u in self.asr_units)

    def ocr_all_done(self) -> bool:
        return self.asr_all_done() and all(
            u["status"] in ("done", "failed") for u in self.ocr_units)

    def fix_all_done(self) -> bool:
        return self.ocr_all_done() and all(
            u["status"] in ("done", "failed") for u in self.fix_units)

    def log(self, msg: str) -> None:
        db.update_job(self.jid, log_line=msg[:160])

    def publish(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self.last_pub < 1.0:
            return
        self.last_pub = now
        a = self.ratio(self.asr_done)
        o = self.ratio(self.ocr_done) if self.ocr_ready else 1.0
        f = self.ratio(self.fix_done)
        prog = max(1.0, min(99.0, round(100 * (0.2 * a + 0.3 * o + 0.5 * f), 1)))
        db.update_job(self.jid, progress=prog,
                      message=f"语音 {int(a * 100)}% · OCR {int(o * 100)}% · 修正 {int(f * 100)}%")

    def snapshot(self) -> dict:
        return {
            "asr": _merge_ranges(self.asr_done),
            "ocr": _merge_ranges(self.ocr_done),
            "fixed": _merge_ranges(self.fix_done),
            "focus": self.focus,
            "duration": self.dur,
        }


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
        translation_lang = opts.get("translation_lang", ui.get("translation_lang", "en"))
        term_policy = opts.get("term_policy", ui.get("term_policy", "keep_en"))
        glossary = (opts.get("glossary") or "").strip()
        frames_n = int(opts.get("frames_per_chunk", ui.get("frames_per_chunk", 4)))
        skip_refine = bool(opts.get("skip_refine"))
        course = v["title"]

        asr_p, asr_m = _role(opts, "asr")
        ref_p, ref_m = _role(opts, "refine")
        txt_p, txt_m = _role(opts, "text")

        llm.reset_usage()
        db.update_job(jid, progress=1, message="读取媒体信息", log_line="开始分析")
        db.update_video(vid, status="analyzing")

        dur = float(v.get("duration") or 0)
        info = await asyncio.to_thread(media.probe, v["path"])
        if not dur:
            dur = info["duration"]
        meta.update({k: info[k] for k in ("width", "height", "vcodec", "acodec",
                                          "has_video", "has_audio", "format")})
        meta["media_kind"] = "video" if info["has_video"] else "audio"
        meta["analysis"] = {
            "asr": f"{asr_p['name']} / {asr_m}", "refine": f"{ref_p['name']} / {ref_m}",
            "text": f"{txt_p['name']} / {txt_m}",
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

        # ---------------- 音轨（已有字幕时不需要）
        audio = vdir / "audio.mp3"
        sub_path = opts.get("subtitle_path") or ""
        has_srt = bool(sub_path and Path(sub_path).exists())
        if not has_srt:
            db.update_job(jid, progress=3, message="提取音轨", log_line="提取音轨")
            if not audio.exists():
                await asyncio.to_thread(media.extract_audio, v["path"], str(audio))
        _check(jid)

        # ---------------- 三条线同时跑：语音识别 / OCR / 修正
        has_video = bool(meta.get("has_video"))
        ocr_p = get_provider("xfyun")
        ocr_ready = bool(ocr_p and (ocr_p.get("api_key") or "").strip()) and has_video
        ocr_models = [m for m in (ocr_p.get("models") if ocr_p else []) or []
                      if "ocr" in (m or "").lower()]
        if not ocr_models:
            ocr_models = ["xoppaddleocrv16", "xopdeepseekocr", "xophunyuanocr"]
        vision_limited = ref_p.get("id") == "zhipu" and "4v" in (ref_m or "").lower()
        batch_size = 20 if ocr_ready else (10 if vision_limited else 24)

        state = _Live(jid, vid, dur, ocr_ready)
        LIVE[jid] = state
        FOCUS.setdefault(vid, 0.0)
        state.focus = FOCUS[vid]

        asr_models = [asr_m]
        for m in asr_p.get("asr_models") or []:
            if m and m not in asr_models:
                asr_models.append(m)
        if asr_p.get("id") == "groq" and "whisper-large-v3-turbo" not in asr_models:
            asr_models.append("whisper-large-v3-turbo")
        asr_models = asr_models[:3]
        asr_parallel = _asr_parallel(asr_p, asr_m, 8)
        sub_lang = "" if ui.get("subtitle_lang") in ("auto", "") else ui.get("subtitle_lang", "zh")

        def _unit_cues(segments: list[dict], base: float) -> list[dict]:
            out = []
            for sg in _merge_asr_segments(segments):
                t = (sg.get("text") or "").strip()
                if not t:
                    continue
                out.append({"start": round(base + float(sg["start"]), 2),
                            "end": round(base + float(sg["end"]), 2),
                            "raw": t, "text": t, "translation": ""})
            return out

        if has_srt:
            srt_cues = _cues_from_srt(sub_path)
            src0 = _source_lang(srt_cues)
            srt_cues = [c for c in srt_cues if not _is_bad_cue(c["text"], src0)]
            state.source_lang = src0
            state.add_cues(srt_cues)
            state.make_batches(srt_cues, batch_size, skip_fix=skip_refine)
            state.asr_done.append((0.0, dur))
            db.update_job(jid, log_line=f"使用已有字幕：{Path(sub_path).name}，{len(srt_cues)} 句")
        else:
            chunks = _chunks(dur, chunk_sec)
            state.asr_units = [{"idx": i, "start": cs, "end": cs + cl, "status": "pending"}
                               for i, (cs, cl) in enumerate(chunks)]

        async def asr_worker() -> None:
            while True:
                u = _pick(state.asr_units, state.focus)
                if u is None:
                    return
                u["status"] = "running"
                _check(jid)
                part = vdir / "chunks" / f"chunk_{u['idx']:04d}.mp3"
                part.parent.mkdir(parents=True, exist_ok=True)
                cl = max(1.0, u["end"] - u["start"])
                try:
                    await asyncio.to_thread(media.slice_audio, str(audio), u["start"], cl, str(part))
                    model = asr_models[u["idx"] % len(asr_models)]
                    res = await asr.transcribe(asr_p, model, str(part), language=sub_lang,
                                               duration=cl, prompt=course)
                    cues = _unit_cues(res["segments"], u["start"])
                    src = _source_lang(cues) if cues else ""
                    if src and not state.source_lang:
                        state.source_lang = src
                    cues = [c for c in cues if not _is_bad_cue(c["text"], src or "unknown")]
                    state.add_cues(cues)
                    state.make_batches(cues, batch_size, skip_fix=skip_refine)
                    state.asr_done.append((u["start"], u["end"]))
                    u["status"] = "done"
                    state.log(f"语音第 {u['idx'] + 1} 段完成：{len(cues)} 句")
                except Canceled:
                    raise
                except Exception as e:  # noqa: BLE001
                    u["status"] = "failed"
                    state.log(f"语音第 {u['idx'] + 1} 段失败：{str(e)[:100]}")
                state.publish()

        async def ocr_worker() -> None:
            while True:
                u = _pick(state.ocr_units, state.focus)
                if u is None:
                    if state.asr_all_done():
                        return
                    await asyncio.sleep(0.2)
                    continue
                u["status"] = "running"
                _check(jid)
                try:
                    part = u["cues"]
                    bdur = max(1.0, part[-1]["end"] - part[0]["start"])
                    frames = await asyncio.to_thread(
                        _frames_for, v, vdir, part[0]["start"], bdur, 1)
                    if frames:
                        screen, _used, err = await ocr.image_to_text_chain(
                            ocr_p, ocr_models, frames[0])
                        u["screen"] = screen
                        if err:
                            state.log(f"画面 OCR 失败：{err[:80]}")
                except Canceled:
                    raise
                except Exception as e:  # noqa: BLE001
                    state.log(f"画面 OCR 异常：{str(e)[:80]}")
                u["status"] = "done"
                if u.get("screen"):
                    state.ocr_done.append((u["start"], u["end"]))
                state.publish()

        async def fix_worker() -> None:
            while True:
                if not state.gates_ok():
                    if state.ocr_all_done():
                        return
                    await asyncio.sleep(0.3)
                    continue
                u = _pick(state.fix_units, state.focus)
                if u is None:
                    if state.ocr_all_done():
                        return
                    await asyncio.sleep(0.2)
                    continue
                u["status"] = "running"
                _check(jid)
                part = u["cues"]
                screen = (u.get("ocr") or {}).get("screen", "") if u.get("ocr") else ""
                lang_before = _source_lang(part)
                texts_before = [c.get("text", "") for c in part]
                need_tr = bool(translation_lang and translation_lang not in ("none", "")
                               and translation_lang != lang_before)
                try:
                    frames: list[str] = []
                    if not screen and not ocr_ready and has_video:
                        bdur = max(1.0, part[-1]["end"] - part[0]["start"])
                        frames = await asyncio.to_thread(
                            _frames_for, v, vdir, part[0]["start"], bdur, 1)
                    role = "text" if (screen or not frames) else "refine"
                    msgs = prompts.refine_messages(
                        part, glossary=glossary, term_policy=term_policy,
                        translation_lang="none",
                        context_before=state.ctx_before(u, 4),
                        context_after=state.ctx_after(u, 2),
                        frames=frames, screen_text=screen, course=course)
                    out = await llm.complete_role(
                        role, msgs, think=False, temperature=0.2, json_mode=True,
                        max_tokens=min(8000, 160 * len(part) + 800) if screen
                        else min(12000, 180 * len(part) + 1000))
                    data = llm.parse_json_loose(out)
                    if not (isinstance(data, dict) and data.get("cues")):
                        data = _salvage_cues(out) or data
                    n = _apply_refine(part, data)
                    if n < max(1, len(part) // 2) and len(part) > 4:
                        mid = len(part) // 2
                        for sub in (part[:mid], part[mid:]):
                            try:
                                m2 = prompts.refine_messages(
                                    sub, glossary=glossary, term_policy=term_policy,
                                    translation_lang="none",
                                    frames=frames, screen_text=screen, course=course)
                                o2 = await llm.complete_role(
                                    "text" if screen else role, m2, think=False,
                                    temperature=0.2, json_mode=True,
                                    max_tokens=min(6000, 160 * len(sub) + 800))
                                d2 = llm.parse_json_loose(o2)
                                if not (isinstance(d2, dict) and d2.get("cues")):
                                    d2 = _salvage_cues(o2) or d2
                                _apply_refine(sub, d2)
                            except Exception:  # noqa: BLE001
                                pass
                    for t in (data.get("terms") or []) if isinstance(data, dict) else []:
                        if isinstance(t, str) and t not in state.terms:
                            state.terms.append(t)
                    # 逐句防串语言：英文原句被模型写成中文时，把中文当译文、原文还原
                    fixed_lang = 0
                    for c, before in zip(part, texts_before):
                        now = c.get("text", "")
                        if _looks_latin(before) and _looks_cjk(now):
                            if not (c.get("translation") or "").strip():
                                c["translation"] = now
                            c["text"] = before
                            fixed_lang += 1
                    if fixed_lang:
                        state.log(f"{fixed_lang} 句被模型串成中文，已转存为译文")
                    if need_tr:
                        state.tr_units.append({"start": u["start"], "end": u["end"],
                                               "cues": part, "status": "pending"})
                except Canceled:
                    raise
                except Exception as e:  # noqa: BLE001
                    state.log(f"修正失败，保留原文：{str(e)[:100]}")
                u["status"] = "done"
                state.fix_done.append((u["start"], u["end"]))
                now = time.time()
                if now - state.last_db >= 2.5:
                    state.last_db = now
                    db.replace_cues(vid, state.ordered_cues())
                state.publish()

        async def tr_worker() -> None:
            while True:
                u = _pick(state.tr_units, state.focus)
                if u is None:
                    if state.fix_all_done():
                        return
                    await asyncio.sleep(0.2)
                    continue
                u["status"] = "running"
                _check(jid)
                try:
                    to = await llm.complete_role(
                        "text",
                        prompts.translate_messages(u["cues"], translation_lang, glossary, course),
                        think=False, temperature=0.2, json_mode=True,
                        max_tokens=min(8000, 160 * len(u["cues"]) + 600))
                    dtr = llm.parse_json_loose(to)
                    if not (isinstance(dtr, dict) and dtr.get("cues")):
                        dtr = _salvage_cues(to) or dtr
                    _apply_refine(u["cues"], dtr)
                except Canceled:
                    raise
                except Exception as e:  # noqa: BLE001
                    state.log(f"翻译失败：{str(e)[:90]}")
                u["status"] = "done"
                now = time.time()
                if now - state.last_db >= 2.5:
                    state.last_db = now
                    db.replace_cues(vid, state.ordered_cues())
                state.publish()

        if skip_refine:
            for u in state.fix_units:
                u["status"] = "done"
            state.fix_done.append((0.0, dur))
            db.update_job(jid, log_line="已按设置跳过 AI 纠错")

        workers: list[asyncio.Task] = []
        if not has_srt:
            workers += [asyncio.create_task(asr_worker()) for _ in range(asr_parallel)]
        if ocr_ready:
            workers += [asyncio.create_task(ocr_worker()) for _ in range(6)]
        if not skip_refine:
            workers += [asyncio.create_task(fix_worker()) for _ in range(16)]
            if translation_lang not in ("none", ""):
                workers += [asyncio.create_task(tr_worker()) for _ in range(16)]
        if workers:
            await asyncio.gather(*workers)
        _check(jid)

        all_cues = state.ordered_cues()
        if not all_cues:
            raise RuntimeError("没有识别出任何字幕，请检查语音模型，或改用已有字幕重试。")
        src_lang = state.source_lang or _source_lang(all_cues)
        meta["source_lang"] = src_lang
        db.replace_cues(vid, all_cues)
        db.update_video(vid, meta=meta)
        state.publish(force=True)
        db.update_job(jid, progress=82,
                      log_line=f"语种：{src_lang}，字幕 {len(all_cues)} 句，OCR 与修正已按进度完成")
        try:
            used = llm.usage_snapshot()
            if used:
                top = sorted(used.items(), key=lambda kv: -kv[1].get("ok", 0))[:6]
                db.update_job(jid, log_line="模型调用：" + "；".join(
                    f"{k} 成功{v.get('ok', 0)}/失败{v.get('fail', 0)}" for k, v in top))
            dead = llm.dead_models()
            if dead:
                db.update_job(jid, log_line="已跳过不可用模型：" + "；".join(
                    f"{k}（{v[:50]}）" for k, v in dead.items()))
        except Exception:  # noqa: BLE001
            pass
        collected_terms = state.terms
        do_translate = bool(translation_lang and translation_lang not in ("none", "")
                            and translation_lang != src_lang)
        if translation_lang not in ("none", "") and not do_translate:
            db.update_job(jid, log_line=f"课程语言为 {src_lang}，跳过翻译")

        # 收尾补翻：个别批次的翻译没返回时，把空译文再补一遍
        if do_translate:
            missing = [c for c in all_cues if not (c.get("translation") or "").strip()]
            if missing:
                db.update_job(jid, progress=83,
                              log_line=f"补齐 {len(missing)} 句未翻译字幕")
                sem_fill = asyncio.Semaphore(8)

                async def fill_tr(part: list[dict]) -> None:
                    async with sem_fill:
                        _check(jid)
                        try:
                            to = await llm.complete_role(
                                "text",
                                prompts.translate_messages(part, translation_lang, glossary, course),
                                think=False, temperature=0.2, json_mode=True,
                                max_tokens=min(8000, 160 * len(part) + 600))
                            dtr = llm.parse_json_loose(to)
                            if not (isinstance(dtr, dict) and dtr.get("cues")):
                                dtr = _salvage_cues(to) or dtr
                            _apply_refine(part, dtr)
                        except Canceled:
                            raise
                        except Exception as e:  # noqa: BLE001
                            db.update_job(jid, log_line=f"补翻失败：{str(e)[:90]}")

                await asyncio.gather(*[fill_tr(missing[i:i + 20])
                                       for i in range(0, len(missing), 20)])
                db.replace_cues(vid, all_cues)

        # ---------------- 分段 + 摘要
        db.update_job(jid, progress=84, message="按内容切分课程段落")
        raw_segs = await _plan_segments(all_cues, txt_p, txt_m, course, jid)
        sem = asyncio.Semaphore(8)  # 文本模型并发由 llm 里的每模型信号量兜底

        async def seg_info(s: dict, idx: int) -> dict:
            text = "".join(c.get("text", "") for c in s["cues"])
            info_d: dict = {}
            async with sem:
                try:
                    out = await llm.complete_role(
                        "text", prompts.segment_messages(text, s["start"], s["end"], course),
                        think=False, temperature=0.3, json_mode=True, max_tokens=1400,
                    )
                    info_d = llm.parse_json_loose(out)
                except Exception as e:  # noqa: BLE001
                    db.update_job(jid, log_line=f"第 {idx+1} 段摘要失败：{str(e)[:100]}")
            if not isinstance(info_d, dict) or not info_d.get("title"):
                info_d = _local_segment_info(text)
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
            out = await llm.complete_role("text", prompts.overview_messages(lines, course),
                                          think=False, temperature=0.3, json_mode=True, max_tokens=2000)
            overview = llm.parse_json_loose(out)
        except Exception as e:  # noqa: BLE001
            db.update_job(jid, log_line=f"总览生成失败：{str(e)[:100]}")
        if not isinstance(overview, dict) or not overview.get("summary"):
            overview = {
                "summary": "；".join(s["title"] for s in segments[:6]),
                "takeaways": [s["title"] for s in segments[:5]],
                "terms": [],
            }
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
