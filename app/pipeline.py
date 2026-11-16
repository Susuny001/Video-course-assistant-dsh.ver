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


def _asr_hint(title: str) -> str:
    """给 Whisper 一点课程领域提示，减少数学/理科术语听错。"""
    t = title or ""
    hints = (
        ("数学", "这是一节数学课，可能出现矩阵、向量、特征值、特征向量、转置、行列式、线性方程组、微积分。"),
        ("物理", "这是一节物理课，可能出现力学、电磁学、公式、单位、实验。"),
        ("化学", "这是一节化学课，可能出现化学式、溶液、反应、实验。"),
        ("英语", "This is an English language class."),
    )
    for key, hint in hints:
        if key in t:
            return hint
    return ""


_REJECT = {"n": 0}
_DROP = {"n": 0}


def _norm_text(s: str) -> str:
    return re.sub(r"[\W_]+", "", (s or "").lower(), flags=re.UNICODE)


def _fidelity_ok(orig: str, new: str) -> bool:
    """防止“纠错”模型把原句改写成另一段无关内容。"""
    a, b = _norm_text(orig), _norm_text(new)
    if not a:
        return True
    if not b:
        return False
    # 长度暴涨基本就是编造（原句很短时放宽一点绝对长度）
    if len(b) > max(len(a) * 2, len(a) + 30):
        return False
    overlap = len(set(a) & set(b)) / max(1, len(set(a)))
    # 公式/短句要求更高重合度，避免把 P I E T 扩写成实验步骤
    return overlap >= (0.6 if len(a) <= 12 else 0.45)


def _tr_ok(src: str, tr: str) -> bool:
    """译文基本合理性：长度不能离谱，必须是正经翻译而不是模型退化的套话。"""
    t = (tr or "").strip()
    if not t:
        return False
    n_src = len(re.findall(r"[A-Za-z0-9']+", src or ""))
    n_tr = len(re.sub(r"\s+", "", t))
    if n_tr > max(40, n_src * 4 + 40):
        return False
    if n_src >= 3 and n_tr <= 2:
        return False
    # 译文里冒出原文根本没有的实验类术语，是小模型背训练样例的典型症状
    low = (src or "").lower()
    for zh, en in _DOMAIN_WORDS:
        if zh in t and not re.search(en, low):
            return False
    return True


_DOMAIN_WORDS = (
    ("实验", r"experiment|\blab\b|laboratory"),
    ("溶液", r"solution|solvent"),
    ("过氧化氢", r"peroxide"),
    ("硫酸铜", r"sulfate|sulphate|copper"),
    ("试剂", r"reagent|chemical"),
    ("拉伸", r"tensile|tension|stretch|extrusion|extrude"),
    ("材料", r"material"),
    ("烧杯", r"beaker|flask"),
    ("浓度", r"concentration"),
)


def _drop_repeated_translations(cues: list[dict]) -> int:
    """同一句译文被套到多条不同原文上，是模型退化或批量错位的典型症状，全部清空重译。"""
    groups: dict[str, list[dict]] = {}
    for c in cues:
        key = _norm_text(c.get("translation", ""))
        if len(key) >= 6:
            groups.setdefault(key, []).append(c)
    n = 0
    for group in groups.values():
        if len(group) < 3:
            continue
        if len({_norm_text(c.get("text", "")) for c in group}) < 3:
            continue
        for c in group:
            c["translation"] = ""
            n += 1
    return n


def _similar(a: str, b: str) -> float:
    from difflib import SequenceMatcher
    return SequenceMatcher(None, a, b).ratio()


def _drop_echo_cues(cues: list[dict], course: str) -> tuple[list[dict], int]:
    """Whisper 在静音段会复述提示词，产生“这些数学课…”这类成段假句。

    判据：句子带课程学科词，且开头模板重复出现（≥3 次）或与已出现句子高度相似。
    """
    word = next((w for w in ("数学", "物理", "化学", "英语", "语文")
                 if w in (course or "")), "")
    if not word:
        return cues, 0
    from collections import Counter
    norms = [_norm_text(c.get("text", "")) for c in cues]
    hit = [bool(word in (c.get("text") or "")) and len(n) >= 5
           for c, n in zip(cues, norms)]
    # 提示词专属措辞，正常课堂不会这么说
    ECHO_PHRASES = ("可能出现", "这是一节", "本节课程", "这节课将")
    prefix = Counter(n[:4] for n, ok in zip(norms, hit) if ok)
    hint_norm = _norm_text(_asr_hint(course))
    seen: list[str] = []
    keep: list[dict] = []
    dropped = 0
    for c, n, ok in zip(cues, norms, hit):
        text = c.get("text") or ""
        echo = (any(p in text for p in ECHO_PHRASES)
                or (ok and (prefix[n[:4]] >= 3
                            or any(_similar(n, s) >= 0.7 for s in seen)
                            or (len(hint_norm) >= 10 and _similar(n, hint_norm) >= 0.45))))
        if echo:
            dropped += 1
            continue
        if ok:
            seen.append(n)
        keep.append(c)
    return keep, dropped


_NOISE_TEXT = {"谢谢", "谢谢大家", "谢谢观看", "好", "好好", "好的", "嗯", "嗯嗯",
               "啊", "对对", "对", "是", "拜拜", "再见", "宣誓", "字幕", "请订阅"}


def _drop_noise_cues(cues: list[dict]) -> tuple[list[dict], int]:
    """静音/音乐段 Whisper 会吐出“谢谢”“好”这类口水词，当字幕没有意义。"""
    keep: list[dict] = []
    dropped = 0
    for c in cues:
        core = re.sub(r"[\W_]+", "", c.get("text") or "")
        if len(core) <= 4 and (core in _NOISE_TEXT or set(core) <= set("好嗯啊哦喔呀吧")):
            dropped += 1
            continue
        keep.append(c)
    return keep, dropped


def _split_long_cues(cues: list[dict], limit: int = 22) -> list[dict]:
    """长句按标点切成短句，并按字数比例分配时间轴，符合主流平台单行字幕。"""
    out: list[dict] = []
    for c in cues:
        text = (c.get("text") or "").strip()
        start = float(c.get("start") or 0)
        end = float(c.get("end") or 0)
        # 有译文时不切，避免原文与译文错位
        if (c.get("translation") or "").strip() or len(text) <= limit * 1.3 or end <= start:
            out.append(c)
            continue
        pieces = re.split(r"(?<=[，。！？；：、,.!?;:])", text)
        chunks: list[str] = []
        cur = ""
        for p in pieces:
            if not p:
                continue
            if len(cur) + len(p) <= limit:
                cur += p
                continue
            if cur:
                chunks.append(cur)
            while len(p) > limit:
                chunks.append(p[:limit])
                p = p[limit:]
            cur = p
        if cur:
            chunks.append(cur)
        chunks = [x.strip() for x in chunks if x.strip()]
        if len(chunks) < 2:
            out.append(c)
            continue
        total = sum(len(x) for x in chunks)
        span = end - start
        t0 = start
        for ch in chunks:
            dur = span * len(ch) / total
            out.append({**c, "start": round(t0, 2), "end": round(t0 + dur, 2),
                        "text": ch, "raw": ch, "translation": ""})
            t0 += dur
    return out


def _drop_empty_cues(cues: list[dict]) -> tuple[list[dict], int]:
    """切句后可能剩下只有标点的碎片，直接丢掉。"""
    keep = [c for c in cues if re.sub(r"[\W_]+", "", c.get("text") or "")]
    return keep, len(cues) - len(keep)


_END_PUNCT = "。！？，；：、…,.!?;:"


def _merge_fragments(cues: list[dict], max_len: int = 90, max_gap: float = 0.8) -> list[dict]:
    """ASR 经常把词从中间切开（“证明”→“证”+“明”），先拼成完整句再交给修正模型。"""
    out: list[dict] = []
    for c in cues:
        text = (c.get("text") or "").strip()
        if not text:
            continue
        if out:
            prev = out[-1]
            ptext = prev["text"]
            gap = float(c.get("start") or 0) - float(prev["end"] or 0)
            if (ptext and ptext[-1] not in _END_PUNCT and gap <= max_gap
                    and len(ptext) + len(text) <= max_len):
                prev["text"] = ptext + text
                prev["raw"] = (prev.get("raw") or ptext) + text
                prev["end"] = c.get("end")
                continue
        out.append({"start": c.get("start"), "end": c.get("end"),
                    "text": text, "raw": c.get("raw") or text})
    return out


def _missing_cues(cues: list[dict], data: dict) -> list[dict]:
    """模型经常只返回一部分 i，没返回的句子会被静默跳过，必须挑出来单独重试。"""
    items = data.get("cues") if isinstance(data, dict) else data
    got: set[int] = set()
    for it in items or []:
        if not isinstance(it, dict):
            continue
        try:
            got.add(int(it.get("i")))
        except Exception:
            continue
    if cues and 0 not in got and 1 in got and len(got) == len(cues):
        got = {k - 1 for k in got}
    return [c for i, c in enumerate(cues) if i not in got]


def _term_fix(tr: str, src: str = "") -> str:
    """术语与机器腔的确定性修正，不依赖模型。"""
    t = tr or ""
    t = t.replace("原地排序", "in-place 排序").replace("原地算法", "in-place 算法")
    if "原地" in t and re.search(r"排序|算法", (src or "") + t):
        t = t.replace("原地", "in-place")
    # “突出两个词汇” → “讲两个词”；“将快速突出” → “快速讲”
    t = re.sub(r"(?:将)?(?:快速|简单|先)?突出(?:一下)?"
               r"([两几一二三四五六七八九十\d]+)个?词汇", r"快速讲\1个词", t)
    t = t.replace("将快速讲", "快速讲").replace("是如果", "：如果")
    # “一个：如果你的排序是破坏性的，” → “第一，看你的排序是不是破坏性的，”
    t = re.sub(r"一个[：:]\s*如果(.{1,24}?)是(.{1,12}?)[，,]", r"第一，看\1是不是\2，", t)
    return t


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
    used_tr: dict[str, str] = {}
    # 模型可以返回空 text 表示“这句是假句/无关内容，删掉”；
    # 但如果它一口气删掉四成以上，说明是模型失效，不接受删除。
    blanks = sum(1 for it in by_i.values()
                 if "text" in it and not (it.get("text") or "").strip())
    allow_drop = blanks <= max(1, len(cues) * 0.4)
    for i, c in enumerate(cues):
        it = by_i.get(i)
        if not it:
            continue
        raw_text = it.get("text")
        text = (raw_text or "").strip()
        if "text" in it and not text and allow_drop:
            c["text"] = ""
            c["translation"] = ""
            _DROP["n"] += 1
            continue
        if text:
            if _fidelity_ok(c.get("text", ""), text):
                c["text"] = text
                n += 1
            else:
                # 不采纳被改写的原文，也不采纳它的译文，整句保留原转写
                _REJECT["n"] += 1
                continue
        tr = (it.get("translation") or "").strip()
        if not tr:
            continue
        tr = _term_fix(tr, c.get("text", ""))
        if not _tr_ok(c.get("text", ""), tr):
            _REJECT["n"] += 1
            continue
        key, src_key = _norm_text(tr), _norm_text(c.get("text", ""))
        if key and key in used_tr and used_tr[key] != src_key:
            # 同一批里同一句译文对上了不同原文，说明批量错位，丢弃
            _REJECT["n"] += 1
            continue
        used_tr[key] = src_key
        c["translation"] = tr
    return n


async def _translate_one(cue: dict, lang: str, glossary: str, course: str) -> None:
    out = await llm.complete_role(
        "text", prompts.translate_messages([cue], lang, glossary, course),
        think=False, temperature=0.2, json_mode=True, max_tokens=1200)
    data = llm.parse_json_loose(out)
    if not (isinstance(data, dict) and data.get("cues")):
        data = _salvage_cues(out) or data
    _apply_refine([cue], data)


async def _translate_cues(cues: list[dict], lang: str, glossary: str,
                          course: str, size: int = 6) -> None:
    """小批量翻译：模型吞句/串行会让整批译文错位，必须校验条数，对不上就逐句重译。"""
    for i in range(0, len(cues), size):
        part = cues[i:i + size]
        try:
            out = await llm.complete_role(
                "text", prompts.translate_messages(part, lang, glossary, course),
                think=False, temperature=0.2, json_mode=True,
                max_tokens=min(4000, 220 * len(part) + 400))
            data = llm.parse_json_loose(out)
            if not (isinstance(data, dict) and data.get("cues")):
                data = _salvage_cues(out) or data
            items = data.get("cues") if isinstance(data, dict) else data
            if not isinstance(items, list) or len(items) != len(part):
                for c in part:
                    await _translate_one(c, lang, glossary, course)
                continue
            _apply_refine(part, {"cues": items})
            for c in part:
                if not (c.get("translation") or "").strip():
                    await _translate_one(c, lang, glossary, course)
        except Canceled:
            raise
        except Exception:  # noqa: BLE001
            continue


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
        self.ocr_fail = 0

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
        if self.ratio(self.ocr_done) >= 0.25:
            return True
        # OCR 服务连续失败时不能一直干等：只要语音已过 25%，先用纯文本纠错/翻译
        return self.ocr_fail >= 6 and not self.ocr_done

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


async def _polish_cues(cues: list[dict], lang: str, course: str, size: int = 8) -> None:
    """二次审校：只改中文，修机器腔和逐词直译。"""
    for i in range(0, len(cues), size):
        part = cues[i:i + size]
        try:
            out = await llm.complete_role(
                "text", prompts.polish_messages(part, lang, course),
                think=False, temperature=0.2, json_mode=True,
                max_tokens=min(4000, 200 * len(part) + 400))
            data = llm.parse_json_loose(out)
            items = data.get("cues") if isinstance(data, dict) else data
            if not isinstance(items, list) or len(items) != len(part):
                continue
            by_i: dict[int, str] = {}
            for it in items:
                if not isinstance(it, dict):
                    continue
                try:
                    by_i[int(it.get("i"))] = (it.get("translation") or "").strip()
                except Exception:
                    continue
            for k, c in enumerate(part):
                tr = by_i.get(k, "")
                if not tr or tr == (c.get("translation") or "").strip():
                    continue
                if _tr_ok(c.get("text", ""), tr):
                    c["translation"] = _term_fix(tr, c.get("text", ""))
        except Canceled:
            raise
        except Exception:  # noqa: BLE001
            continue


async def retranslate(vid: str, jid: str, mode: str = "missing") -> None:
    """重新翻译已有字幕：mode=missing 只补空译文，all 整讲重译。"""
    v = db.get_video(vid) or {}
    lang = (load_settings().get("ui") or {}).get("translation_lang") or "zh"
    cues = db.get_cues(vid)
    if not cues:
        db.update_job(jid, progress=100, message="没有字幕", status="done",
                      log_line="没有可翻译的字幕")
        return
    if mode == "all":
        for c in cues:
            c["translation"] = ""
    if mode == "polish":
        todo = [c for c in cues if (c.get("translation") or "").strip()]
        db.update_job(jid, progress=2, message=f"审校 {len(todo)} 句译文",
                      log_line=f"开始审校译文：{len(todo)} 句")
        sem = asyncio.Semaphore(8)

        async def one_polish(part: list[dict]) -> None:
            async with sem:
                await _polish_cues(part, lang, v.get("title") or "")

        try:
            await asyncio.gather(*[one_polish(todo[i:i + 8])
                                   for i in range(0, len(todo), 8)])
            db.replace_cues(vid, cues)
        except Exception as e:  # noqa: BLE001
            db.update_job(jid, status="failed", message="审校失败",
                          log_line=f"审校失败：{str(e)[:100]}")
            return
        db.update_job(jid, progress=100, message="译文审校完成", status="done",
                      log_line="译文审校完成")
        return
    todo = [c for c in cues if not (c.get("translation") or "").strip()]
    db.update_job(jid, progress=2, message=f"重译 {len(todo)} 句",
                  log_line=f"开始重译：{len(todo)} 句（模式 {mode}）")
    sem = asyncio.Semaphore(12)

    async def one(part: list[dict]) -> None:
        async with sem:
            try:
                await _translate_cues(part, lang, "", v.get("title") or "")
            except Exception:  # noqa: BLE001
                pass

    try:
        await asyncio.gather(*[one(todo[i:i + 6]) for i in range(0, len(todo), 6)])
        db.replace_cues(vid, cues)
    except Exception as e:  # noqa: BLE001
        db.update_job(jid, status="failed", message="重译失败",
                      log_line=f"重译失败：{str(e)[:100]}")
        return
    left = sum(1 for c in cues if not (c.get("translation") or "").strip())
    db.update_job(jid, progress=100, message="重译完成", status="done",
                  log_line=f"重译完成，剩余空译文 {left} 句")


async def reasr(vid: str, jid: str, opts: dict | None = None) -> None:
    """重新做语音识别：按 5 分钟切片重跑 ASR，重建字幕，并可选自动重译。"""
    opts = opts or {}
    v = db.get_video(vid) or {}
    ui = load_settings().get("ui") or {}
    dur = float(v.get("duration") or 0)
    vdir = video_dir(vid)
    audio = vdir / "audio.mp3"
    chunk_sec = float(opts.get("chunk_seconds") or ui.get("asr_chunk_seconds") or 300)
    db.update_job(jid, progress=2, message="准备音轨", log_line="重新识别：准备音轨")
    try:
        if not audio.exists():
            await asyncio.to_thread(media.extract_audio, v["path"], str(audio))
        asr_p, asr_m = _role(opts, "asr")
    except Exception as e:  # noqa: BLE001
        db.update_job(jid, status="failed", message="重新识别失败",
                      log_line=f"重新识别失败：{str(e)[:100]}")
        return

    chunks = _chunks(dur, chunk_sec)
    db.update_job(jid, progress=5, message=f"重新识别 {len(chunks)} 段",
                  log_line=f"重新识别：共 {len(chunks)} 段")
    results: list[dict] = []
    done = {"n": 0}
    lock = asyncio.Lock()
    sem = asyncio.Semaphore(4)

    async def one(idx: int, cs: float, cl: float) -> None:
        async with sem:
            part = vdir / "chunks" / f"rea_{idx:04d}.mp3"
            part.parent.mkdir(parents=True, exist_ok=True)
            try:
                await asyncio.to_thread(media.slice_audio, str(audio), cs, cl, str(part))
                res = await asr.transcribe(asr_p, asr_m, str(part),
                                           language=ui.get("subtitle_lang") or "",
                                           duration=cl, prompt="")
                cues = []
                for sg in _merge_asr_segments(res["segments"]):
                    t = (sg.get("text") or "").strip()
                    if t:
                        cues.append({"start": round(cs + float(sg["start"]), 2),
                                     "end": round(cs + float(sg["end"]), 2),
                                     "raw": t, "text": t, "translation": ""})
            except Exception as e:  # noqa: BLE001
                db.update_job(jid, log_line=f"第 {idx + 1} 段失败：{str(e)[:80]}")
                cues = []
            async with lock:
                results.extend(cues)
                done["n"] += 1
                db.update_job(jid, progress=5 + 80 * done["n"] / max(1, len(chunks)),
                              message=f"重新识别 {done['n']}/{len(chunks)} 段")

    await asyncio.gather(*[one(i, cs, cl) for i, (cs, cl) in enumerate(chunks)])

    cues = sorted(results, key=lambda c: c["start"])
    src = _source_lang(cues)
    cues = [c for c in cues if not _is_bad_cue(c["text"], src or "unknown")]
    cues = _merge_fragments(cues)
    if not cues:
        db.update_job(jid, status="failed", message="没有识别出字幕",
                      log_line="重新识别失败：没有识别出字幕")
        return
    db.replace_cues(vid, cues)
    db.update_video(vid, meta={**(v.get("meta") or {}), "source_lang": src})
    db.update_job(jid, progress=88, message="重新识别完成",
                  log_line=f"重新识别完成：{len(cues)} 句，语种 {src}")

    lang = ui.get("translation_lang") or "zh"
    if lang not in ("none", "") and lang != src:
        await _translate_cues(cues, lang, "", v.get("title") or "")
        db.replace_cues(vid, cues)
    db.update_job(jid, progress=100, message="重新识别完成", status="done",
                  log_line="重新识别与翻译已完成")


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
        hint = _asr_hint(course)
        # 中文提示词会被 Whisper 在静音段原样复述（“这是一节数学课…”），只在英文课保留提示
        asr_prompt = hint if hint.startswith("This is") else ""

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
        sub_path = opts.get("subtitle_path") or meta.get("subtitle_path") or ""
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
                                               duration=cl, prompt=asr_prompt)
                    cues = _unit_cues(res["segments"], u["start"])
                    src = _source_lang(cues) if cues else ""
                    if src and not state.source_lang:
                        state.source_lang = src
                    cues = [c for c in cues if not _is_bad_cue(c["text"], src or "unknown")]
                    cues = _merge_fragments(cues)
                    state.add_cues(cues)
                    state.make_batches(cues, batch_size, skip_fix=skip_refine)
                    state.asr_done.append((u["start"], u["end"]))
                    u["status"] = "done"
                    # 语音一出来就先落库，保证“边看边出字幕”，不用等修正门槛
                    now = time.time()
                    if now - state.last_db >= 2.5:
                        state.last_db = now
                        db.replace_cues(vid, state.ordered_cues())
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
                ok = True
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
                            ok = False
                            state.ocr_fail += 1
                            state.log(f"画面 OCR 失败：{err[:80]}")
                        else:
                            state.ocr_fail = 0
                except Canceled:
                    raise
                except Exception as e:  # noqa: BLE001
                    ok = False
                    state.ocr_fail += 1
                    state.log(f"画面 OCR 异常：{str(e)[:80]}")
                u["status"] = "done"
                # 处理过就算覆盖：黑板没字的画面也不能让修正/翻译一直等门槛
                if ok:
                    state.ocr_done.append((u["start"], u["end"]))
                state.publish()

        async def fix_worker() -> None:
            while True:
                # 必须等 ASR 全部结束：否则尾部语音还没出来、队列瞬时为空时会提前退出，
                # 视频最后十几分钟就永远不进入修正（曾导致结尾大段错字没人改）
                if not state.gates_ok():
                    if state.ocr_all_done() and state.asr_all_done():
                        return
                    await asyncio.sleep(0.3)
                    continue
                u = _pick(state.fix_units, state.focus)
                if u is None:
                    if state.ocr_all_done() and state.asr_all_done():
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
                    # 纠错固定走视觉模型：纯文本 glm-4-flash 遇到碎句会背术语表
                    role = "refine"
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
                    # 模型少返回的句子必须逐句补，否则会原样留着 ASR 错字
                    miss = _missing_cues(part, data)
                    if miss:
                        state.log(f"{len(miss)} 句未返回，逐句重试")
                        for mc in miss:
                            try:
                                m1 = prompts.refine_messages(
                                    [mc], glossary=glossary, term_policy=term_policy,
                                    translation_lang="none",
                                    context_before=state.ctx_before(u, 4),
                                    context_after=state.ctx_after(u, 2),
                                    frames=frames, screen_text=screen, course=course)
                                o1 = await llm.complete_role(
                                    role, m1, think=False, temperature=0.2,
                                    json_mode=True, max_tokens=1500)
                                d1 = llm.parse_json_loose(o1)
                                if not (isinstance(d1, dict) and d1.get("cues")):
                                    d1 = _salvage_cues(o1) or d1
                                _apply_refine([mc], d1)
                            except Canceled:
                                raise
                            except Exception:  # noqa: BLE001
                                continue
                    if _REJECT["n"]:
                        state.log(f"拦截 {_REJECT['n']} 句疑似编造内容，保留原转写")
                        _REJECT["n"] = 0
                    if _DROP["n"]:
                        state.log(f"删除 {_DROP['n']} 句假句/无关内容")
                        _DROP["n"] = 0
                    if n < max(1, len(part) // 2) and len(part) > 4:
                        mid = len(part) // 2
                        for sub in (part[:mid], part[mid:]):
                            try:
                                m2 = prompts.refine_messages(
                                    sub, glossary=glossary, term_policy=term_policy,
                                    translation_lang="none",
                                    frames=frames, screen_text=screen, course=course)
                                o2 = await llm.complete_role(
                                    role, m2, think=False,
                                    temperature=0.2, json_mode=True,
                                    max_tokens=min(6000, 160 * len(sub) + 800))
                                d2 = llm.parse_json_loose(o2)
                                if not (isinstance(d2, dict) and d2.get("cues")):
                                    d2 = _salvage_cues(o2) or d2
                                _apply_refine(sub, d2)
                            except Exception:  # noqa: BLE001
                                pass
                    part_text = "".join(texts_before) + (screen or "")
                    for t in (data.get("terms") or []) if isinstance(data, dict) else []:
                        # 术语必须真的出现在这节课里，否则就是模型夹带的无关内容
                        if (isinstance(t, str) and t.strip() and t.strip() in part_text
                                and t not in state.terms):
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
                    await _translate_cues(u["cues"], translation_lang, glossary, course)
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
        all_cues, echo_n = _drop_echo_cues(all_cues, course)
        if echo_n:
            db.update_job(jid, log_line=f"清除 {echo_n} 句静音段回声幻觉（模型复述提示词）")
        all_cues, noise_n = _drop_noise_cues(all_cues)
        if noise_n:
            db.update_job(jid, log_line=f"清除 {noise_n} 句静音段口水词")
        before_split = len(all_cues)
        all_cues = _split_long_cues(all_cues)
        all_cues, empty_n = _drop_empty_cues(all_cues)
        if len(all_cues) != before_split:
            db.update_job(jid, log_line=f"长句已切分为 {len(all_cues)} 条单行字幕")
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
            dropped = _drop_repeated_translations(all_cues)
            if dropped:
                db.update_job(jid, log_line=f"发现 {dropped} 句译文重复套用，已清空重译")
            missing = [c for c in all_cues if not (c.get("translation") or "").strip()]
            if missing:
                db.update_job(jid, progress=83,
                              log_line=f"补齐 {len(missing)} 句未翻译字幕")
                sem_fill = asyncio.Semaphore(8)

                async def fill_tr(part: list[dict]) -> None:
                    async with sem_fill:
                        _check(jid)
                        try:
                            await _translate_cues(part, translation_lang, glossary, course)
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
