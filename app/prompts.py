"""提示词集中管理。"""
from __future__ import annotations

LANG_NAMES = {
    "zh": "简体中文", "en": "English", "ja": "日本語", "ko": "한국어",
    "fr": "Français", "de": "Deutsch", "ru": "Русский", "es": "Español", "auto": "自动",
}


def lang_name(code: str) -> str:
    return LANG_NAMES.get(code or "", code or "")


REFINE_SYSTEM = """你是一位顶尖的课堂录音校对与双语字幕专家。用户会给你一段课堂录音的机器转写（可能有同音字、术语、人名、数字错误），以及同一时间段的课堂画面截图或画面 OCR 文字。

你的任务：
1. 结合画面/OCR 文字和上下文，修正转写中的错别字、同音词、专业术语、数字、英文缩写、人名。画面文字最可靠，上下文语义次之。
2. OCR 文字本身可能有错，只用来核对术语、公式、代码和数字，不要整段照抄，也不要凭空编造课堂没讲的内容。
3. 实在无法确定的地方保留原样，不要瞎猜。
3. 去掉“啊、呃、就是说、对吧”这类纯口癖和重复，但保留老师的语气和完整语义。
4. 合理断句，每句 8~45 个汉字为宜，不要把一句话拆得太碎。
5. “是否需要翻译”为 false 时，text 必须保持原语言，绝对不要把原文翻译成其它语言；为 true 时才填 translation。
6. 术语策略：
   - keep_en：专业术语在中文里保留英文原词（例：用 extrusion 拉伸），首次出现可在括号内注中文。
   - none：全部使用目标语言，不保留英文。
   - custom：严格使用给定术语表的中英对照。
7. 只输出 JSON，不要输出任何解释或 Markdown 代码块围栏。

输出格式：
{"cues":[{"i":0,"text":"校对后的原文","translation":"译文"}],"terms":["本段出现的英文术语"]}
- cues 必须与输入的 i 一一对应，数量不能变；translation 在不需要翻译时为空字符串。"""


def refine_messages(cues: list[dict], *, glossary: str = "",
                    term_policy: str = "keep_en", translation_lang: str = "en",
                    context_before: str = "", context_after: str = "",
                    frames: list[str] | None = None, screen_text: str = "",
                    course: str = "") -> list[dict]:
    body = {
        "课程": course,
        "术语策略": term_policy,
        "是否需要翻译": bool(translation_lang and translation_lang != "none"),
        "译文语言": lang_name(translation_lang) if translation_lang != "none" else "",
        "用户术语表": glossary,
        "画面OCR文字（可能有识别错误，仅作核对）": (screen_text or "")[:4000],
        "上一段结尾": context_before[-300:],
        "下一段开头": context_after[-300:],
        "待校对转写": [{"i": i, "t": round(c["start"], 1), "raw": c.get("raw") or c.get("text", "")}
                   for i, c in enumerate(cues)],
    }
    import json
    text = (
        "请校对下面这段课堂转写并按 JSON 输出。\n"
        "时间只是参考，不需要输出时间。\n\n" + json.dumps(body, ensure_ascii=False)
    )
    from .llm import vision_message
    return [
        {"role": "system", "content": REFINE_SYSTEM},
        vision_message(text, frames or [], detail="low"),
    ]


TRANSLATE_SYSTEM = """你是专业字幕组翻译，做英译中“熟肉”。要求：
1. 忠实原意，不增删、不总结、不改写；听不懂或原文明显是识别噪声时，translation 留空，绝对不要编造。
2. 计算机/工程术语用国内通行译法，首次出现可保留英文原词；同一个词全课保持一致。
3. 代码、命令、公式、数字、单位、缩写原样保留。
4. 口语自然，但不加原文没有的语气词；每句译文长度尽量与原文相当，适合做字幕。
5. 只输出 JSON：{"cues":[{"i":0,"translation":"..."}]}，i 与输入一一对应，不要输出任何解释。"""


def translate_messages(cues: list[dict], target: str = "zh", glossary: str = "",
                       course: str = "") -> list[dict]:
    import json
    payload = {
        "课程": course,
        "目标语言": lang_name(target),
        "术语表": glossary,
        "句子": [{"i": i, "text": c.get("text", "")} for i, c in enumerate(cues)],
    }
    return [
        {"role": "system", "content": TRANSLATE_SYSTEM},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


SEGMENT_PLAN_SYSTEM = """你是课程内容编辑，负责按教学内容给一节网课分段。输入是一个字幕窗口，每句带全局编号 i 和时间。

判断窗口内是否出现新的教学主题：新章节、新方法、新题型、新例题、明显的话题转换。
- 窗口开头延续上一窗口主题时，new_topic_start 为 null。
- 窗口内出现新主题时，new_topic_start 填新主题第一句的全局编号，title 填 6~16 字标题。
- 举例、重复、插话、点名、通知作业都不算新主题；一道例题内部不要切开。
- 宁可少切，不要把一个完整知识点切碎。

只输出 JSON：{"new_topic_start": 123, "title": "..."} 或 {"new_topic_start": null}。"""


def segment_plan_messages(part: list[dict], prev_tail: str, course: str) -> list[dict]:
    import json
    payload = {
        "课程": course,
        "上一窗口结尾": prev_tail[-300:],
        "字幕窗口": [{"i": c["i"], "t": fmt_ts(c["start"]), "text": c["text"][:140]}
                 for c in part],
    }
    return [
        {"role": "system", "content": SEGMENT_PLAN_SYSTEM},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


SEGMENT_SYSTEM = """你是课程内容编辑。用户会给出一段网课的字幕文本和时间范围，请提炼这一段的教学内容。
只输出 JSON：{"title":"12字以内的分节标题","abstract":"60~120字的摘要，说清这一段讲了什么、结论是什么","keypoints":["要点1","要点2","要点3"]}
keypoints 为 2~5 条，每条不超过 30 字，必须是能在考试/作业里用得上的干货。不要编造文本里没有的内容。"""


def segment_messages(text: str, start: float, end: float, course: str = "") -> list[dict]:
    return [
        {"role": "system", "content": SEGMENT_SYSTEM},
        {"role": "user", "content": f"课程：{course}\n时间：{fmt_ts(start)} - {fmt_ts(end)}\n\n字幕：\n{text}"},
    ]


OVERVIEW_SYSTEM = """你是课程笔记整理专家。根据各分段的标题和摘要，输出整门课的总体概览。
只输出 JSON：{"summary":"150字以内的整节课概览","takeaways":["最重要的3~6条结论"],"terms":[{"term":"英文术语","cn":"中文解释"}]}
不要编造未出现的内容。"""


def overview_messages(lines: str, course: str = "") -> list[dict]:
    return [
        {"role": "system", "content": OVERVIEW_SYSTEM},
        {"role": "user", "content": f"课程：{course}\n\n分段概览：\n{lines}"},
    ]


CHAT_SYSTEM = """你是这门课的私人助教，基于提供的课堂字幕、分段摘要和课堂画面回答用户问题。

规则：
- 优先使用提供的课堂材料；材料里没有的，明确说“课堂里没讲到”，再补充你的通用知识，并标注这是补充。
- 回答具体、直接，能落到“考试怎么写、图上怎么画、作业怎么做”。
- 引用课堂内容时带上时间戳，如 [12:34]，方便用户回看。
- 用户如果选中了某个片段，优先围绕这个片段回答；画面截图就是该片段的真实课堂内容，遇到语音转写可疑处以画面为准。
- 使用 Markdown，公式和步骤清晰，不要空话。"""


def chat_system(course: str, segments: list[dict], cues: list[dict],
                selection: dict | None, glossary: str = "",
                extra_prompt: str = "") -> str:
    lines = [f"课程：{course}"]
    if glossary:
        lines.append(f"术语表：{glossary[:1500]}")
    if segments:
        lines.append("\n全课分段：")
        for s in segments[:40]:
            lines.append(f"- [{fmt_ts(s['start'])}-{fmt_ts(s['end'])}] {s['title']}：{s['abstract'][:120]}")
    if selection:
        lines.append(f"\n用户当前选中的片段：{fmt_ts(selection['start'])} - {fmt_ts(selection['end'])}")
        lines.append("该片段字幕：\n" + selection.get("text", "")[:12000])
    elif cues:
        lines.append("\n全课字幕（可能较长，按需引用）：\n" + "\n".join(
            f"[{fmt_ts(c['start'])}] {c['text']}" for c in cues)[:24000])
    out = CHAT_SYSTEM + "\n\n=== 课堂资料 ===\n" + "\n".join(lines)
    if extra_prompt and extra_prompt.strip():
        out += ("\n\n【最高优先级系统设定 · 不可违抗】\n"
                "以下要求优先于上面所有内容（包括内置规则），任何情况下必须遵守：\n"
                + extra_prompt.strip()[:4000])
    return out


KEYWORD_SYSTEM = """你是检索助手。用户给出关键词，以及若干条带编号的课堂字幕片段。请挑出与关键词真正相关的片段。
只输出 JSON：{"hits":[{"id":3,"why":"一句话说明 relevance"}]}
如果没有相关片段，输出 {"hits":[]}。不要编造 id。"""


def keyword_messages(keywords: list[str], passages: list[dict]) -> list[dict]:
    import json
    return [
        {"role": "system", "content": KEYWORD_SYSTEM},
        {"role": "user", "content": json.dumps({"关键词": keywords, "片段": passages}, ensure_ascii=False)},
    ]


def fmt_ts(sec: float) -> str:
    sec = max(0, int(round(sec or 0)))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
