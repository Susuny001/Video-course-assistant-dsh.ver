"""演示模式：不联网也能跑通完整流程（转写用参考文稿铺时间轴，问答用本地检索）。"""
from __future__ import annotations

import re

from .asr import split_sentences
from .refdoc import slice_by_time

# 常用工程制图术语，用于演示模式的双语字幕与术语提示
TERMS: dict[str, str] = {
    "剖视图": "section view", "断面图": "cross-section", "基本视图": "principal view",
    "主视图": "front view", "俯视图": "top view", "左视图": "left view",
    "后视图": "rear view", "右视图": "right view", "仰视图": "bottom view",
    "三视图": "three-view drawing", "斜视图": "auxiliary view", "向视图": "referenced view",
    "局部视图": "partial view", "全剖视图": "full section", "半剖视图": "half section",
    "局部剖视图": "broken-out section", "旋转剖": "aligned section", "阶梯剖": "offset section",
    "斜剖": "oblique section", "复合剖": "composite section", "剖切平面": "cutting plane",
    "剖面线": "hatching", "轮廓线": "outline", "投影": "projection", "投影面": "projection plane",
    "组合体": "composite solid", "装配图": "assembly drawing", "零件图": "part drawing",
    "标准件": "standard part", "常用件": "common part", "螺纹": "thread", "螺栓": "bolt",
    "螺母": "nut", "垫圈": "washer", "键": "key", "销": "pin", "齿轮": "gear",
    "轴承": "bearing", "弹簧": "spring", "图层": "layer", "多段线": "polyline",
    "标注": "dimension", "尺寸": "dimension", "公差": "tolerance", "表面粗糙度": "surface roughness",
    "倒角": "chamfer", "圆角": "fillet", "肋板": "rib", "孔": "hole", "槽": "slot",
    "轴线": "axis", "对称": "symmetry", "展开": "development", "机件": "machine part",
    "视图": "view", "作图": "construction", "补线": "missing line", "辅助线": "construction line",
    "大作业": "term project", "期中考试": "midterm", "期末": "final exam",
}

_FILLER = re.compile(r"(^|[，。！？；\s])(嗯|呃|额|啊|那个|这个|就是说|然后呢|对吧|是吧|好吧|对不对)[，,、]?")


def clean_fillers(text: str) -> str:
    prev = None
    while prev != text:
        prev = text
        text = _FILLER.sub(r"\1", text)
    text = re.sub(r"[，,]{2,}", "，", text)
    return text.strip("，, ")


def demo_transcribe(reference_text: str, total_duration: float,
                    start: float, duration: float) -> dict:
    piece = slice_by_time(reference_text, total_duration, start, duration) if reference_text else ""
    if not piece or len(piece) < 8:
        piece = (f"这里是第 {int(start // 60) + 1} 分钟左右的课堂内容。"
                 f"当前使用演示模式，请为课程上传参考文稿，或在设置里配置语音识别 API 后重新分析。")
    sents = split_sentences(piece) or [piece]
    total_chars = sum(len(s) for s in sents) or 1
    out, t = [], 0.0
    for s in sents:
        d = duration * len(s) / total_chars
        # 返回相对时间，由流水线统一加上分块起点
        out.append({"start": round(t, 2), "end": round(min(duration, t + d), 2), "text": s})
        t += d
    return {"text": piece, "language": "zh", "segments": out}


def demo_refine(cues: list[dict], translation_lang: str = "en") -> dict:
    out, terms = [], []
    for i, c in enumerate(cues):
        text = clean_fillers(c.get("raw") or c.get("text", ""))
        found = [en for cn, en in TERMS.items() if cn in text]
        for en in found:
            if en not in terms:
                terms.append(en)
        translation = ""
        if translation_lang and translation_lang != "none" and found:
            translation = "EN · " + " / ".join(dict.fromkeys(found))
        out.append({"i": i, "text": text, "translation": translation})
    return {"cues": out, "terms": terms}


def demo_segment_info(text: str, start: float, end: float) -> dict:
    text = (text or "").strip()
    sentences = split_sentences(text)
    first = (sentences[0] if sentences else text).strip("，。；：、 ")
    if len(first) > 20:
        first = first[:20] + "…"
    title = first or "课堂片段"
    abstract = "".join(sentences[:4])[:150] or text[:150]
    keys = []
    for s in sentences[1:]:
        s = s.strip("，。； ")
        if 8 <= len(s) <= 34:
            keys.append(s)
        if len(keys) >= 4:
            break
    return {"title": title, "abstract": abstract, "keypoints": keys}


def demo_overview(segments: list[dict]) -> dict:
    if not segments:
        return {"summary": "", "takeaways": [], "terms": []}
    summary = "本节课共分 " + str(len(segments)) + " 段：" + "；".join(s["title"] for s in segments[:4]) + " 等。"
    takeaways = [s["title"] for s in segments[:5]]
    joined = " ".join(s.get("abstract", "") for s in segments)
    terms = [{"term": en, "cn": cn} for cn, en in TERMS.items() if cn in joined][:12]
    return {"summary": summary, "takeaways": takeaways, "terms": terms}


def _score(query: str, text: str) -> float:
    q = set(re.findall(r"[\u4e00-\u9fa5]{2}|[a-zA-Z]{3,}", query))
    if not q:
        return 0.0
    t = text or ""
    return sum(1 for w in q if w in t) / len(q)


def demo_chat(question: str, cues: list[dict], segments: list[dict],
              selection: dict | None = None) -> str:
    pool = cues
    if selection:
        pool = [c for c in cues if c["start"] >= selection["start"] - 1 and c["end"] <= selection["end"] + 1]
        if not pool:
            pool = cues
    ranked = sorted(pool, key=lambda c: -_score(question, c.get("text", "")))[:4]
    ranked = [c for c in ranked if _score(question, c.get("text", "")) > 0] or ranked[:2]
    seg = None
    if ranked:
        t = ranked[0]["start"]
        seg = next((s for s in segments if s["start"] <= t <= s["end"]), None)

    lines = ["> 演示模式：未配置大模型 API，以下是本地检索到的课堂原文。"
             "在「设置」里填入任意 OpenAI 兼容的 Key 后即可获得真正的 AI 回答。", ""]
    if seg:
        lines += [f"**所属分段：{seg['title']}**", seg.get("abstract", ""), ""]
    if ranked:
        lines.append("**相关课堂内容**")
        for c in ranked:
            mm, ss = divmod(int(c["start"]), 60)
            lines.append(f"- [{mm:02d}:{ss:02d}] {c.get('text', '')}")
    else:
        lines.append("没有在字幕里找到直接相关的内容，换个说法或先完成课程分析再问。")
    return "\n".join(lines)


def demo_keyword_hits(keywords: list[str], cues: list[dict], limit: int = 30) -> list[dict]:
    hits = []
    for c in cues:
        text = c.get("text", "")
        matched = [k for k in keywords if k and k.lower() in text.lower()]
        if matched:
            hits.append({"start": c["start"], "end": c["end"], "text": text,
                         "why": "命中：" + "、".join(matched)})
        if len(hits) >= limit:
            break
    return hits
