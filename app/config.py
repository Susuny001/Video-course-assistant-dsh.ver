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
        "name": "硅基流动 SiliconFlow（按量计费）",
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
        "notes": "按量计费，需要自己填 Key；不是长期免费模型。",
        "docs": "https://cloud.siliconflow.cn/account/ak",
    },
    {
        "id": "dashscope",
        "name": "阿里云百炼 DashScope",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key": "",
        "models": [
            "qwen3.8-flash",
            "qwen-flash",
            "qwen-plus",
            "qwen3-max",
            "qwen-vl-max",
        ],
        "asr_models": [],
        "vision": True,
        "notes": "按量计费，需要自己填 Key；qwen-vl 系列支持看图。",
        "docs": "https://bailian.console.aliyun.com/",
    },
    {
        "id": "zhipu",
        "name": "智谱 GLM（glm-4-flash 免费）",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "api_key": "",
        "models": ["glm-4.7-flash", "glm-4.6v-flash", "glm-4v-flash", "glm-4-flash", "glm-4.5-air"],
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
        "models": ["xoppaddleocrv16", "xopdeepseekocr", "xophunyuanocr",
                   "spark-x2.6-4b", "spark-x2.5-4b"],
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
        "models": ["qwen/qwen3.8-27b", "openai/gpt-oss-120b", "openai/gpt-oss-20b"],
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
            "openrouter/free",
            "google/gemma-4-31b-it:free",
            "google/gemma-4-26b-a4b-it:free",
            "nvidia/nemotron-3.5-lightning:free",
            "nvidia/nemotron-3-ultra-550b-a55b:free",
            "baidu/ernie-5.5-flash-preview:free",
            "nvidia/nemotron-3.5-super-vl:free",
        ],
        "asr_models": [],
        "vision": True,
        "notes": "需要能访问 openrouter.ai；:free 结尾的模型免费。",
        "docs": "https://openrouter.ai/keys",
    },
    {
        "id": "pollinations",
        "name": "Pollinations（免费，无需注册）",
        "base_url": "https://text.pollinations.ai/openai",
        "api_key": "unused",
        "models": ["openai"],
        "asr_models": [],
        "vision": False,
        "notes": "完全免费、不用注册也不用 Key；匿名限制约 1 次/45 秒，只作为问答的最后兜底，不参与批量分析。",
        "docs": "https://pollinations.ai",
    },
    {
        "id": "llm7",
        "name": "LLM7（免费，无需注册）",
        "base_url": "https://api.llm7.io/v1",
        "api_key": "unused",
        "models": ["codestral-latest"],
        "asr_models": [],
        "vision": False,
        "notes": "完全免费、不用注册也不用 Key；实测 codestral-latest 可用，作为文本兜底，不参与批量纠错。",
        "docs": "https://llm7.io",
    },
    {
        "id": "ollama",
        "name": "本地 Ollama（完全离线）",
        "base_url": "http://127.0.0.1:11434/v1",
        "api_key": "ollama",
        "models": ["qwen2.5:7b", "qwen2.5vl:7b", "llava:7b", "llama3.1:8b"],
        "asr_models": [],
        "vision": True,
        "notes": "接口保留，默认不参与自动调用；需要时先 pull 模型再手动选。",
        "docs": "https://ollama.com/download",
    },
    {
        "id": "cerebras",
        "name": "Cerebras（免费额度，速度极快）",
        "base_url": "https://api.cerebras.ai/v1",
        "api_key": "",
        "models": ["llama-3.3-70b", "llama-4-scout-17b-16e-instruct", "qwen-3-32b"],
        "asr_models": [],
        "vision": False,
        "notes": "注册即送免费额度，推理速度非常快，适合批量纠错和翻译；端点已实测存活。",
        "docs": "https://cloud.cerebras.ai/",
    },
    {
        "id": "mistral",
        "name": "Mistral 官方（免费 Experiment 额度）",
        "base_url": "https://api.mistral.ai/v1",
        "api_key": "",
        "models": ["mistral-small-latest", "open-mistral-nemo", "mistral-large-latest"],
        "asr_models": [],
        "vision": False,
        "notes": "免费 Experiment 套餐够日常翻译/分段用；端点已实测存活。",
        "docs": "https://console.mistral.ai/api-keys",
    },
    {
        "id": "sambanova",
        "name": "SambaNova（免费额度）",
        "base_url": "https://api.sambanova.ai/v1",
        "api_key": "",
        "models": ["Meta-Llama-3.3-70B-Instruct", "gpt-oss-120b", "GLM-5.3"],
        "asr_models": [],
        "vision": False,
        "notes": "免费额度较大，模型列表已实测可公开读取。",
        "docs": "https://cloud.sambanova.ai/apis",
    },
    {
        "id": "huggingface",
        "name": "Hugging Face Router（每月免费额度）",
        "base_url": "https://router.huggingface.co/v1",
        "api_key": "",
        "models": ["meta-llama/Llama-3.1-8B-Instruct", "Qwen/Qwen3.8-27B",
                   "deepseek-ai/DeepSeek-V4.1-Flash"],
        "asr_models": [],
        "vision": False,
        "notes": "用 HF Token 调用，每月有免费推理额度；模型 ID 已从端点实读。",
        "docs": "https://huggingface.co/settings/tokens",
    },
    {
        "id": "chutes",
        "name": "Chutes（免费额度）",
        "base_url": "https://llm.chutes.ai/v1",
        "api_key": "",
        "models": ["deepseek-ai/DeepSeek-V3.2-TEE", "Qwen/Qwen3.8-27B-TEE", "zai-org/GLM-5.3"],
        "asr_models": [],
        "vision": False,
        "notes": "去中心化推理，注册送免费额度；模型 ID 已从端点实读。",
        "docs": "https://chutes.ai/app",
    },
    {
        "id": "novita",
        "name": "Novita AI（免费额度）",
        "base_url": "https://api.novita.ai/v3/openai",
        "api_key": "",
        "models": ["deepseek/deepseek-v4.1-flash-1017", "zai-org/glm-5.3-flash",
                   "moonshotai/kimi-k3.1-pro"],
        "asr_models": [],
        "vision": False,
        "notes": "注册送免费额度；模型 ID 已从端点实读。",
        "docs": "https://novita.ai/settings/key-management",
    },
    {
        "id": "deepinfra",
        "name": "DeepInfra（免费额度）",
        "base_url": "https://api.deepinfra.com/v1/openai",
        "api_key": "",
        "models": ["meta-llama/Llama-3.3-70B-Instruct-Turbo", "Qwen/Qwen3.5-9B",
                   "google/gemma-3-4b-it"],
        "asr_models": [],
        "vision": False,
        "notes": "注册送免费额度；模型 ID 已从端点实读。",
        "docs": "https://deepinfra.com/dash/api_keys",
    },
    {
        "id": "together",
        "name": "Together AI（有免费模型）",
        "base_url": "https://api.together.xyz/v1",
        "api_key": "",
        "models": ["meta-llama/Llama-3.3-70B-Instruct-Turbo", "Qwen/Qwen2.5-72B-Instruct-Turbo"],
        "asr_models": [],
        "vision": False,
        "notes": "部分模型标价 0，可直接免费用；端点已实测存活。",
        "docs": "https://api.together.ai/settings/api-keys",
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
    "pollinations": 1,
    "llm7": 2,
    "cerebras": 4,
    "mistral": 2,
    "sambanova": 2,
    "huggingface": 2,
    "chutes": 2,
    "novita": 2,
    "deepinfra": 2,
    "together": 2,
}
MODEL_CONCURRENCY = {
    "glm-4.7-flash": 6,
    "glm-4v-flash": 4,
    "glm-4.5-flash": 10,
    "glm-4-flash": 12,
    "spark-x2.6-4b": 6,
    "xoppaddleocrv16": 6,
    "xopdeepseekocr": 6,
    "xophunyuanocr": 6,
    "whisper-large-v3": 4,
    "whisper-large-v3-turbo": 4,
    "openai": 1,
    "qwen/qwen3.8-27b": 4,
    "openai/gpt-oss-120b": 2,
    "openai/gpt-oss-20b": 4,
}

# 长期免费模型白名单：自动轮换只在这些模型里挑，避免误用收费模型。
# 用户在设置里显式指定的主模型/备用模型不受此表限制。
FREE_MODELS = {
    "zhipu": {"glm-4.7-flash", "glm-4-flash", "glm-4.6v-flash", "glm-4v-flash", "glm-4.5-flash"},
    "xfyun": {"spark-x2.6-4b", "spark-x2.5-4b"},
    "groq": {"whisper-large-v3", "whisper-large-v3-turbo",
             "qwen/qwen3.8-27b", "openai/gpt-oss-120b", "openai/gpt-oss-20b"},
    "pollinations": {"openai"},
    "llm7": {"codestral-latest"},
    "cerebras": {"llama-3.3-70b", "llama-4-scout-17b-16e-instruct", "qwen-3-32b"},
    "mistral": {"mistral-small-latest", "open-mistral-nemo"},
    "sambanova": {"Meta-Llama-3.3-70B-Instruct", "gpt-oss-120b", "GLM-5.3"},
    "huggingface": {"meta-llama/Llama-3.1-8B-Instruct", "Qwen/Qwen3.8-27B",
                    "deepseek-ai/DeepSeek-V4.1-Flash"},
    "chutes": {"deepseek-ai/DeepSeek-V3.2-TEE", "Qwen/Qwen3.8-27B-TEE", "zai-org/GLM-5.3"},
    "novita": {"deepseek/deepseek-v4.1-flash-1017", "zai-org/glm-5.3-flash",
               "moonshotai/kimi-k3.1-pro"},
    "deepinfra": {"meta-llama/Llama-3.3-70B-Instruct-Turbo", "Qwen/Qwen3.5-9B",
                  "google/gemma-3-4b-it"},
    "together": {"meta-llama/Llama-3.3-70B-Instruct-Turbo", "Qwen/Qwen2.5-72B-Instruct-Turbo"},
    "openrouter": {
        "openrouter/free",
        "google/gemma-4-31b-it:free",
        "google/gemma-4-26b-a4b-it:free",
        "nvidia/nemotron-3.5-lightning:free",
        "nvidia/nemotron-3-ultra-550b-a55b:free",
        "baidu/ernie-5.5-flash-preview:free",
        "nvidia/nemotron-3.5-super-vl:free",
    },
}


def is_free_model(pid: str, model: str) -> bool:
    return model in (FREE_MODELS.get(pid) or set())


def _fits_role(pid: str, model: str, role: str) -> bool:
    """自动轮换时按环节挑模型：纠错只用视觉模型，文本/问答不用视觉模型。"""
    if not is_free_model(pid, model):
        return False
    low = (model or "").lower()
    vision = "4.6v" in low or "4v" in low or "vl" in low or "vision" in low
    if role == "refine":
        return vision
    if role in ("text", "chat"):
        return not vision
    return True


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
        "chat": {"think": True, "temperature": 0.4, "system_prompt": ""},
        "last_opened": "",
    }


_settings_cache: dict | None = None


def _config_path() -> Path:
    return data_dir() / "config.json"


def _keys_path() -> Path:
    """API Key 单独存放，打包时可以不带这个文件。可用 YKZS_KEYS_FILE 覆盖路径。"""
    env = os.environ.get("YKZS_KEYS_FILE")
    return Path(env) if env else data_dir() / "keys.json"


def _key_file_path() -> Path:
    """主目录下的 key/key.txt：开发者直接改这里即可，代码里不出现任何 Key。"""
    env = os.environ.get("YKZS_KEY_FILE")
    if env:
        return Path(env)
    return app_root() / "key" / "key.txt"


def _read_key_file() -> dict:
    p = _key_file_path()
    if not p.exists():
        return {}
    out: dict[str, str] = {}
    try:
        for line in p.read_text("utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k:
                out[k] = v
    except Exception:
        return {}
    return out


def _write_key_file(keys: dict) -> None:
    p = _key_file_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# 云课助手 API Key 配置",
        "# 格式：provider_id=key（等号两边可以有空格）",
        "# 在这里改 Key 后重启应用即可；在软件设置里填写也会写回本文件。",
        "# 本文件不要提交到代码仓库。",
        "",
    ]
    for k in sorted(keys):
        lines.append(f"{k}={keys.get(k, '')}")
    p.write_text("\n".join(lines) + "\n", "utf-8")
    try:
        os.chmod(p, 0o600)
    except Exception:
        pass


def _read_keys() -> dict:
    p = _keys_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_keys(keys: dict) -> None:
    _write_key_file(keys)


def _migrate_keys(base: dict) -> None:
    """首次升级：把 keys.json / config.json 里的 Key 汇总到 key/key.txt。"""
    kp = _key_file_path()
    if kp.exists():
        return
    keys: dict[str, str] = {}
    for k, v in (_read_keys() or {}).items():
        if (v or "").strip():
            keys[k] = v.strip()
    for prov in base.get("providers") or []:
        k = (prov.get("api_key") or "").strip()
        if k and k not in ("unused", "ollama"):
            keys.setdefault(prov.get("id", ""), k)
        prov["api_key"] = ""
    if not keys:
        keys = {p.get("id", ""): "" for p in base.get("providers") or []}
    _write_key_file(keys)
    # 旧文件清空，避免两处 Key 不一致
    legacy = _keys_path()
    if legacy.exists():
        try:
            legacy.write_text("{}\n", "utf-8")
        except Exception:
            pass
    try:
        _config_path().write_text(json.dumps(base, ensure_ascii=False, indent=2), "utf-8")
    except Exception:
        pass


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
        _migrate_keys(base)
        keys = _read_key_file()
        for k, v in (_read_keys() or {}).items():
            if not keys.get(k) and (v or "").strip():
                keys[k] = v.strip()
        for prov in base.get("providers") or []:
            k = keys.get(prov.get("id", ""))
            if k:
                prov["api_key"] = k
        _settings_cache = base
        return base


def save_settings(patch: dict) -> dict:
    global _settings_cache
    with _lock:
        cur = load_settings()
        merged = _deep_merge(cur, patch or {})
        # 自定义助教提示词服务端兜底：最多 4000 字，防止前端绕过
        chat = merged.get("chat")
        if isinstance(chat, dict):
            chat["system_prompt"] = str(chat.get("system_prompt") or "")[:4000]
        # Key 写入独立文件，config.json 只保留非敏感配置，方便打包分发
        keys = {
            p.get("id", ""): p.get("api_key", "")
            for p in merged.get("providers") or []
            if p.get("api_key")
        }
        _write_keys(keys)
        legacy = _keys_path()
        if legacy.exists():
            try:
                legacy.write_text("{}\n", "utf-8")
            except Exception:
                pass
        clean = json.loads(json.dumps(merged, ensure_ascii=False))
        for prov in clean.get("providers") or []:
            prov["api_key"] = ""
        _config_path().write_text(json.dumps(clean, ensure_ascii=False, indent=2), "utf-8")
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
            if _fits_role(prim.get("id", ""), m, role):
                add(prim, m)
    for p in s["providers"]:
        if p.get("id") in ("xfyun", "ollama"):
            continue  # xfyun 是 OCR 专用；本地 7B 效率低，保留接口但不自动参与
        if p.get("id") in ("pollinations", "llm7") and role != "chat":
            continue  # 免注册免费源限速，只给问答兜底，不参与批量纠错
        pool = p.get("asr_models") if role == "asr" else p.get("models")
        for m in pool or []:
            if _fits_role(p.get("id", ""), m, role):
                add(p, m)
    return out


def new_id(prefix: str = "") -> str:
    return (prefix + uuid.uuid4().hex)[:16]
