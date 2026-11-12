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

_lock = threading.Lock()


def app_root() -> Path:
    """程序根目录：源码运行时是仓库根目录；打包后是 exe 所在目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    """数据目录，默认在程序目录下（不占 C 盘）。可用环境变量 YKZS_DATA_DIR 覆盖。"""
    env = os.environ.get("YKZS_DATA_DIR")
    root = Path(env) if env else app_root() / "data"
    root.mkdir(parents=True, exist_ok=True)
    return root


def video_dir(vid: str) -> Path:
    p = data_dir() / "videos" / vid
    p.mkdir(parents=True, exist_ok=True)
    return p


def web_dir() -> Path:
    return app_root() / "web"


def samples_dir() -> Path:
    return app_root() / "samples"


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
        "models": ["glm-4-flash", "glm-4-air", "glm-4-plus", "glm-4v-flash", "glm-4.5-air"],
        "asr_models": [],
        "vision": True,
        "notes": "glm-4-flash 与 glm-4v-flash 目前免费，适合白嫖。",
        "docs": "https://open.bigmodel.cn/usercenter/apikeys",
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
    {
        "id": "demo",
        "name": "内置演示模式（不用 API）",
        "base_url": "demo://",
        "api_key": "",
        "models": ["demo-chat"],
        "asr_models": ["demo-asr"],
        "vision": True,
        "notes": "不联网的假数据，用参考文稿铺满时间轴，方便先体验界面与流程。",
        "docs": "",
    },
]


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
            "translation_lang": "en",
            "term_policy": "keep_en",
            "segment_minutes": 8,
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
    if pid == "demo":
        return True
    p = get_provider(pid)
    if not p:
        return False
    if pid == "ollama":
        return True
    return bool((p.get("api_key") or "").strip())


def resolve_role(role: str) -> tuple[dict, str, bool]:
    """返回 (provider, model, 是否降级到演示模式)。"""
    s = load_settings()
    cfg = s["roles"].get(role) or {}
    pid, model = cfg.get("provider", "demo"), cfg.get("model", "")
    provider = get_provider(pid)
    if not provider or not provider_ready(pid):
        demo = get_provider("demo")
        assert demo
        return demo, "demo-asr" if role == "asr" else "demo-chat", True
    if not model:
        model = (provider.get("asr_models") or provider.get("models") or ["demo"])[0]
    return provider, model, False


def is_demo(provider: dict) -> bool:
    return provider.get("base_url", "").startswith("demo://")


def new_id(prefix: str = "") -> str:
    return (prefix + uuid.uuid4().hex)[:16]
