"""参考文稿（录音对应的文字稿）处理：加载、按时间切片、检索最相关片段。"""
from __future__ import annotations

import re
from pathlib import Path


def load_text(path: str) -> str:
    p = Path(path)
    if not p.exists():
        return ""
    raw = p.read_text("utf-8", errors="replace")
    if p.suffix.lower() in (".md", ".markdown"):
        raw = re.sub(r"^#{1,6}\s*", "", raw, flags=re.M)
        raw = re.sub(r"\*\*(.+?)\*\*", r"\1", raw)
        raw = re.sub(r"`{1,3}", "", raw)
        raw = re.sub(r"^\s*[-*]\s+", "", raw, flags=re.M)
        raw = raw.replace("---", "")
    return re.sub(r"\n{3,}", "\n\n", raw).strip()


def _clean(s: str) -> str:
    return re.sub(r"[\s，。！？；：、,.!?;:“”\"'（）()《》【】\\-—_]", "", s)


def _bigrams(s: str) -> set[str]:
    s = _clean(s)
    return {s[i:i + 2] for i in range(max(0, len(s) - 1))} or ({s} if s else set())


def best_excerpt(text: str, query: str, max_chars: int = 1800) -> str:
    """按二字组重合度挑出与 query 最相关的文稿片段。"""
    if not text:
        return ""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if len(p.strip()) > 20]
    if not paras:
        return text[:max_chars]
    q = _bigrams(query)
    if not q:
        return text[:max_chars]
    scored = []
    for i, p in enumerate(paras):
        pb = _bigrams(p)
        score = len(q & pb) / (len(pb) ** 0.4 + 1)
        scored.append((score, i))
    scored.sort(reverse=True)
    best = scored[0][1]
    chunk = paras[max(0, best - 1): best + 2]
    return "\n\n".join(chunk)[:max_chars]


def slice_by_time(text: str, total_duration: float, start: float, duration: float) -> str:
    """把一份文稿按字数比例铺到时间轴上，取对应时间窗的内容（演示模式用）。"""
    if not text or total_duration <= 0:
        return ""
    total = len(text)
    c0 = int(total * max(0.0, start) / total_duration)
    c1 = int(total * min(total_duration, start + duration) / total_duration)
    c0 = max(0, min(total - 1, c0))
    c1 = max(c0 + 1, min(total, c1))
    # 向前后扩到句界，避免半句
    lo = max(text.rfind("。", 0, c0), text.rfind("\n", 0, c0))
    if lo > c0 - 120:
        c0 = lo + 1
    hi_candidates = [x for x in (text.find("。", c1), text.find("\n", c1)) if x != -1]
    if hi_candidates:
        hi = min(hi_candidates) + 1
        if hi < c1 + 120:
            c1 = hi
    return text[c0:c1].strip()
