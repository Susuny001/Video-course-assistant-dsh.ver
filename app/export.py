"""把字幕 / 笔记落盘：json、srt、md。"""
from __future__ import annotations

import json
from pathlib import Path

from .prompts import fmt_ts


def srt_ts(sec: float) -> str:
    ms = int(round(max(0.0, sec) * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_srt(cues: list[dict], bilingual: bool = True) -> str:
    out = []
    for i, c in enumerate(cues, 1):
        text = (c.get("text") or "").strip()
        tr = (c.get("translation") or "").strip() if bilingual else ""
        body = text if not tr else f"{text}\n{tr}"
        out.append(f"{i}\n{srt_ts(c['start'])} --> {srt_ts(max(c['end'], c['start'] + 0.6))}\n{body}\n")
    return "\n".join(out)


def build_notes(video: dict, segments: list[dict], overview: dict, cues: list[dict],
                bilingual: bool = True) -> str:
    lines = [f"# {video['title']}", ""]
    lines.append(f"- 时长：{fmt_ts(video.get('duration', 0))}")
    lines.append(f"- 分析时间：{video.get('updated_at', '')}")
    if overview.get("summary"):
        lines += ["", "## 课程概览", "", overview["summary"]]
    if overview.get("takeaways"):
        lines += ["", "### 重点结论", ""] + [f"- {x}" for x in overview["takeaways"]]
    if overview.get("terms"):
        lines += ["", "### 术语表", "", "| 术语 | 中文 |", "| --- | --- |"]
        lines += [f"| {t.get('term', '')} | {t.get('cn', '')} |" for t in overview["terms"]]
    lines += ["", "## 分段笔记", ""]
    for s in segments:
        lines.append(f"### {s['title']}　`{fmt_ts(s['start'])} - {fmt_ts(s['end'])}`")
        if s.get("abstract"):
            lines += ["", s["abstract"]]
        if s.get("keypoints"):
            lines += [""] + [f"- {k}" for k in s["keypoints"]]
        lines.append("")
    lines += ["## 完整字幕", ""]
    for c in cues:
        ts = fmt_ts(c["start"])
        lines.append(f"**[{ts}]** {c.get('text', '')}")
        if bilingual and c.get("translation"):
            lines.append(f"> {c['translation']}")
        lines.append("")
    return "\n".join(lines)


def write_all(vdir: Path, video: dict, segments: list[dict], cues: list[dict],
              overview: dict, bilingual: bool = True) -> dict:
    vdir.mkdir(parents=True, exist_ok=True)
    (vdir / "subs.json").write_text(json.dumps(
        {"video": video["title"], "cues": cues, "segments": segments, "overview": overview},
        ensure_ascii=False, indent=2), "utf-8")
    (vdir / "subs.srt").write_text(build_srt(cues, bilingual), "utf-8")
    (vdir / "notes.md").write_text(build_notes(video, segments, overview, cues, bilingual), "utf-8")
    return {
        "srt": str(vdir / "subs.srt"),
        "json": str(vdir / "subs.json"),
        "notes": str(vdir / "notes.md"),
    }
