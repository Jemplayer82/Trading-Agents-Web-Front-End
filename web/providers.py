"""Static reference data the frontend uses to populate dropdowns.

Mirrors the choices in cli/utils.py so the web form offers the same
provider and model menu as the interactive CLI — with one override:
when OLLAMA_BASE_URL points to Ollama Cloud, we surface the cloud
model names (suffixed `-cloud`) instead of the local-Ollama tags.
"""

from __future__ import annotations

import os
from typing import Any

from tradingagents.llm_clients.model_catalog import MODEL_OPTIONS


def _ollama_default_url() -> str:
    return os.environ.get("OLLAMA_BASE_URL") or "http://localhost:11434/v1"


def _is_ollama_cloud(url: str | None) -> bool:
    return bool(url) and "ollama.com" in url


# Ollama Cloud catalog — names that actually resolve at https://ollama.com/v1.
# Order matters: the first entry is the default in the dropdown for a fresh
# session (no saved preferences yet). We default to a deliberate mixed pair
# — gpt-oss for quick + kimi-k2 for deep — to keep the analyst and
# decision-maker stages on different model lineages.
_OLLAMA_CLOUD_MODELS = {
    "quick": [
        ("GPT-OSS 20B (cloud) — default", "gpt-oss:20b-cloud"),
        ("GPT-OSS 120B (cloud)", "gpt-oss:120b-cloud"),
        ("Kimi K2 1T (cloud)", "kimi-k2:1t-cloud"),
        ("GLM-4.6 (cloud)", "glm-4.6:cloud"),
        ("Custom model ID", "custom"),
    ],
    "deep": [
        ("Kimi K2 1T (cloud) — default", "kimi-k2:1t-cloud"),
        ("GPT-OSS 120B (cloud)", "gpt-oss:120b-cloud"),
        ("GPT-OSS 20B (cloud)", "gpt-oss:20b-cloud"),
        ("DeepSeek V3.1 671B (cloud)", "deepseek-v3.1:671b-cloud"),
        ("Qwen3-Coder 480B (cloud)", "qwen3-coder:480b-cloud"),
        ("GLM-4.6 (cloud)", "glm-4.6:cloud"),
        ("Custom model ID", "custom"),
    ],
}


PROVIDERS = [
    {"key": "openai", "label": "OpenAI", "base_url": "https://api.openai.com/v1"},
    {"key": "google", "label": "Google", "base_url": None},
    {"key": "anthropic", "label": "Anthropic", "base_url": "https://api.anthropic.com/"},
    {"key": "xai", "label": "xAI", "base_url": "https://api.x.ai/v1"},
    {"key": "deepseek", "label": "DeepSeek", "base_url": "https://api.deepseek.com"},
    {"key": "qwen", "label": "Qwen (Global)", "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"},
    {"key": "qwen-cn", "label": "Qwen (China)", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
    {"key": "glm", "label": "GLM (Z.AI)", "base_url": "https://api.z.ai/api/paas/v4/"},
    {"key": "glm-cn", "label": "GLM (BigModel)", "base_url": "https://open.bigmodel.cn/api/paas/v4/"},
    {"key": "minimax", "label": "MiniMax (Global)", "base_url": "https://api.minimax.io/v1"},
    {"key": "minimax-cn", "label": "MiniMax (China)", "base_url": "https://api.minimaxi.com/v1"},
    {"key": "ollama", "label": "Ollama", "base_url": None},
]


def _ollama_models() -> dict[str, list[tuple[str, str]]]:
    url = _ollama_default_url()
    if _is_ollama_cloud(url):
        return _OLLAMA_CLOUD_MODELS
    return MODEL_OPTIONS.get("ollama", {"quick": [], "deep": []})


def get_providers() -> list[dict[str, Any]]:
    out = []
    for p in PROVIDERS:
        entry = dict(p)
        if entry["key"] == "ollama":
            entry["base_url"] = _ollama_default_url()
            models = _ollama_models()
        else:
            models = MODEL_OPTIONS.get(entry["key"], {})
        entry["models"] = {
            "quick": [{"label": display, "value": value} for display, value in models.get("quick", [])],
            "deep": [{"label": display, "value": value} for display, value in models.get("deep", [])],
        }
        out.append(entry)
    return out


ANALYSTS = [
    {"key": "market", "label": "Market Analyst"},
    {"key": "social", "label": "Sentiment Analyst"},
    {"key": "news", "label": "News Analyst"},
    {"key": "fundamentals", "label": "Fundamentals Analyst"},
]


LANGUAGES = [
    "English", "Chinese", "Japanese", "Korean", "Hindi",
    "Spanish", "Portuguese", "French", "German", "Arabic", "Russian",
]


DEPTH_PRESETS = [
    {"value": 1, "label": "Shallow — quick"},
    {"value": 3, "label": "Medium — balanced"},
    {"value": 5, "label": "Deep — comprehensive"},
]
