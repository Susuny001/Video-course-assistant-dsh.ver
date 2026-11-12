"""本地关键词检索（不调用模型）。"""
from __future__ import annotations


def keyword_hits(keywords: list[str], cues: list[dict], limit: int = 40) -> list[dict]:
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
