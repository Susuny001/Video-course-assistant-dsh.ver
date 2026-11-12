"""配置与路径管理。

配置文件保存在数据目录下的 config.json，API Key 只存在本机，不上传任何地方。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from pathlib import Path

APP_NAME = "云课助手"
VERSION = "1.0.0"

_lock = threading.RLock()  # save_settings 内部会再次 load_settings，必须可重入


def app_root() -> Path:
    r"""程序根目录：源码运行时是仓库根目录；打包后是 exe 所在目录。

    本项目把 exe 放在仓库的 dist 下，此时回到上一级，保证源码方式和 exe 方式
    共用同一份 data 与 samples；把 exe 单独拷走时仍以 exe 所在目录为准。
    """
    if getattr(sys, "frozen", False):
        p = Path(sys.executable).resolve().parent
        if p.name.lower() == "dist" and (p.parent / "web").exists():
            return p.parent
        return p
    return Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    """数据目录，默认在程序目录下（不占 C 盘）。可用环境变量 YKZS_DATA_DIR 覆盖。"""
    env = os.environ.get("YKZS_DATA_DIR")
    root = Path(env) if env else app_root() / "data"
    root.mkdir(parents=True, exist_ok=True)
    return root


def bundled_dir() -> Path:
    """打包后只读资源所在目录（PyInstaller 解包目录）。"""
    return Path(getattr(sys, "_MEIPASS", app_root()))


def video_dir(vid: str) -> Path:
    p = data_dir() / "videos" / vid
    p.mkdir(parents=True, exist_ok=True)
    return p


def web_dir() -> Path:
    p = app_root() / "web"
    if p.exists():
        return p
    return bundled_dir() / "web"


def samples_dir() -> Path:
    p = app_root() / "samples"
    if p.exists():
        return p
    return bundled_dir() / "samples"


# ---------------------------------------------------------------- 默认服务商预设
# 全部走 OpenAI 兼容接口，用户填自己的 Key 即可；免费额度以官方页面为准。
DEFAULT_PROVIDERS = [
    {
        "id": "siliconflow",
        "name": "硅基流动 SiliconFlow（有免费模型）",
        "base_url": "https://api.siliconflow.cn/v1",
        "api_key": "",
        "models": [
            "Qwen/Qwen3-8B",
            "Qwen/Qwen3-235B-A22B-Instruct-2507",
            "Qwen/Qwen2.5-7B-Instruct",
            "Qwen/Qwen2.5-VL-72B-Instruct",
            "deepseek-ai/DeepSeek-V3",
            "THUDM/GLM-4-9B-0414",
        ],
        "asr_models": ["FunAudioLLM/SenseVoiceSmall"],
        "vision": True,
        "notes": "注册后在「账号 → API 密钥」创建 Key。SenseVoiceSmall 语音识别免费，Qwen 系列有免费额度。",
        "docs": "https://cloud.siliconflow.cn/account/ak",
    },
    {
        "id": "dashscope",
        "name": "阿里云百炼 DashScope",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key": "",
        "models": [
            "qwen-plus",
            "qwen-max",
            "qwen3-max",
            "qwen-vl-max",
            "qwen-vl-plus",
        ],
        "asr_models": [],
        "vision": True,
        "notes": "新用户有免费额度；qwen-vl 系列支持看图。",
        "docs": "https://bailian.console.aliyun.com/",
    },
    {
        "id": "zhipu",
        "name": "智谱 GLM（glm-4-flash 免费）",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "api_key": "",
        "models": ["glm-4.7-flash", "glm-4v-flash", "glm-4-flash", "glm-4.5-air"],
        "asr_models": [],
        "vision": True,
        "notes": "glm-4-flash 与 glm-4v-flash 免费；glm-4.7-flash 用于文本和分段。",
        "docs": "https://open.bigmodel.cn/usercenter/apikeys",
    },
    {
        "id": "xfyun",
        "name": "讯飞 MaaS（OCR 专用）",
        "base_url": "https://maas-api.cn-huabei-1.xf-yun.com/v2",
        "api_key": "",
        "models": ["xoppaddleocrv16", "xopdeepseekocr", "xophunyuanocr", "spark-x2.6-4b"],
        "asr_models": [],
        "vision": True,
        "notes": "PaddleOCR-VL-1.6 / DeepSeek-OCR / HunyuanOCR 用来识别课堂画面文字，免费额度；只做 OCR，不参与对话。",
        "docs": "https://maas.xfyun.cn/modelService?platform=maas",
    },
    {
        "id": "deepseek",
        "name": "DeepSeek 官方",
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "asr_models": [],
        "vision": False,
        "notes": "OpenAI 兼容；deepseek-chat 普通对话，deepseek-reasoner 带思考。不支持语音和看图，外语课翻译/分段可接这里。",
        "docs": "https://platform.deepseek.com/api_keys",
    },
    {
        "id": "groq",
        "name": "Groq（Whisper 语音识别）",
        "base_url": "https://api.groq.com/openai/v1",
        "api_key": "",
        "models": ["llama-3.3-70b-versatile"],
        "asr_models": ["whisper-large-v3", "whisper-large-v3-turbo"],
        "vision": False,
        "notes": "语音识别免费额度，速度快，返回句级时间戳；Key 在控制台 API Keys 创建。",
        "docs": "https://console.groq.com/keys",
    },
    {
        "id": "openrouter",
        "name": "OpenRouter（聚合，有免费模型）",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": "",
        "models": [
            "qwen/qwen-2.5-vl-72b-instruct:free",
            "meta-llama/llama-3.3-70b-instruct:free",
            "google/gemini-2.0-flash-exp:free",
        ],
        "asr_models": [],
        "vision": True,
        "notes": "需要能访问 openrouter.ai；:free 结尾的模型免费。",
        "docs": "https://openrouter.ai/keys",
    },
    {
        "id": "ollama",
        "name": "本地 Ollama（完全离线）",
        "base_url": "http://127.0.0.1:11434/v1",
        "api_key": "ollama",
        "models": ["qwen2.5:7b", "qwen2.5vl:7b", "llava:7b", "llama3.1:8b"],
        "asr_models": [],
        "vision": True,
        "notes": "先安装 Ollama 并 `ollama pull qwen2.5vl:7b`，无需联网。",
        "docs": "https://ollama.com/download",
    },
]


# 各模型同时在途请求上限。免费额度按官方文档/实测取“不会再触发 429 的最大值”，
# 宁可略保守也不要整批失败；数字可按账号等级在 config.json 的 "limits" 里覆盖。
CONCURRENCY = {
    "siliconflow": 4,
    "dashscope": 2,
    "zhipu": 4,
    "groq": 4,
    "openrouter": 2,
    "ollama": 2,
    "xfyun": 6,
    "deepseek": 16,
}
MODEL_CONCURRENCY = {
    "glm-4.7-flash": 4,
    "glm-4v-flash": 4,
    "glm-4.5-flash": 8,
    "glm-4-flash": 12,
    "spark-x2.6-4b": 6,
    "xoppaddleocrv16": 6,
    "xopdeepseekocr": 6,
    "xophunyuanocr": 6,
    "whisper-large-v3": 4,
    "whisper-large-v3-turbo": 4,
}


def model_concurrency(provider: dict, model: str) -> int:
    """单个模型的并发上限；模型级优先，其次服务商级，最后兜底 2。"""
    s = load_settings()
    limits = s.get("limits") or {}
    pid = (provider or {}).get("id", "")
    per_model = limits.get("models") or {}
    n = per_model.get(f"{pid}/{model}") or per_model.get(model)
    if not n:
        n = (limits.get("providers") or {}).get(pid) or MODEL_CONCURRENCY.get(model) \
            or CONCURRENCY.get(pid) or 2
    try:
        return max(1, int(n))
    except Exception:
        return 2


def default_settings() -> dict:
    return {
        "providers": json.loads(json.dumps(DEFAULT_PROVIDERS, ensure_ascii=False)),
        "roles": {
            "asr": {"provider": "siliconflow", "model": "FunAudioLLM/SenseVoiceSmall"},
            "refine": {"provider": "siliconflow", "model": "Qwen/Qwen2.5-VL-72B-Instruct"},
            "text": {"provider": "siliconflow", "model": "Qwen/Qwen3-8B"},
            "chat": {"provider": "siliconflow", "model": "Qwen/Qwen3-8B"},
        },
        "ui": {
            "subtitle_lang": "zh",
            "translation_lang": "zh",
            "term_policy": "keep_en",
            "frames_per_chunk": 4,
            "asr_chunk_seconds": 300,
            "third_lang": "",
            "font_size": 26,
            "auto_scroll": True,
        },
        "chat": {"think": True, "temperature": 0.4},
        "last_opened": "",
    }


_settings_cache: dict | None = None


def _config_path() -> Path:
    return data_dir() / "config.json"


def load_settings(force: bool = False) -> dict:
    global _settings_cache
    with _lock:
        if _settings_cache is not None and not force:
            return _settings_cache
        path = _config_path()
        base = default_settings()
        if path.exists():
            try:
                saved = json.loads(path.read_text("utf-8"))
                base = _deep_merge(base, saved)
            except Exception:
                pass
        # 升级时补齐新增的内置服务商，不覆盖用户已保存的 Key
        have = {p.get("id") for p in base.get("providers") or []}
        for p in DEFAULT_PROVIDERS:
            if p["id"] not in have:
                base.setdefault("providers", []).append(
                    json.loads(json.dumps(p, ensure_ascii=False)))
        _settings_cache = base
        return base


def save_settings(patch: dict) -> dict:
    global _settings_cache
    with _lock:
        cur = load_settings()
        merged = _deep_merge(cur, patch or {})
        _config_path().write_text(json.dumps(merged, ensure_ascii=False, indent=2), "utf-8")
        _settings_cache = merged
        return merged


def _deep_merge(a: dict, b: dict) -> dict:
    if not isinstance(b, dict):
        return b
    out = dict(a)
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def get_provider(pid: str) -> dict | None:
    for p in load_settings()["providers"]:
        if p.get("id") == pid:
            return p
    return None


def provider_ready(pid: str) -> bool:
    p = get_provider(pid)
    if not p:
        return False
    if pid == "ollama":
        return True
    return bool((p.get("api_key") or "").strip())


def resolve_role(role: str) -> tuple[dict, str]:
    """返回 (provider, model)。没配 Key 直接报错，不伪造数据。"""
    s = load_settings()
    cfg = s["roles"].get(role) or {}
    pid, model = cfg.get("provider", ""), cfg.get("model", "")
    provider = get_provider(pid)
    if not provider:
        raise RuntimeError(f"没有找到服务商「{pid}」，请到设置里配置。")
    if not provider_ready(pid):
        raise RuntimeError(f"「{provider['name']}」还没有填写 API Key，请到设置里配置后再分析。")
    if not model:
        pool = (provider.get("asr_models") if role == "asr" else provider.get("models")) or []
        if not pool:
            raise RuntimeError(f"「{provider['name']}」没有可用模型，请到设置里填写。")
        model = pool[0]
    return provider, model


def role_candidates(role: str) -> list[tuple[dict, str]]:
    """主模型 + 同服务商备用 + 其它已配置服务商，按顺序自动切换。"""
    s = load_settings()
    cfg = s["roles"].get(role) or {}
    out: list[tuple[dict, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(p: dict | None, model: str | None) -> None:
        if not p or not model or not provider_ready(p["id"]):
            return
        key = (p["id"], model)
        if key in seen:
            return
        seen.add(key)
        out.append((p, model))

    prim = get_provider(cfg.get("provider", ""))
    if prim:
        add(prim, cfg.get("model"))
    for fb in cfg.get("fallbacks") or []:
        add(get_provider(fb.get("provider", "")), fb.get("model"))
    if prim:
        pool = prim.get("asr_models") if role == "asr" else prim.get("models")
        for m in pool or []:
            add(prim, m)
    for p in s["providers"]:
        if p.get("id") == "xfyun":
            continue  # OCR 专用，不参与对话/纠错候选
        if role == "refine" and not p.get("vision"):
            continue
        pool = p.get("asr_models") if role == "asr" else p.get("models")
        for m in pool or []:
            add(p, m)
    return out


def new_id(prefix: str = "") -> str:
    return (prefix + uuid.uuid4().hex)[:16]
