"""Static reference data the frontend uses to populate dropdowns.

Mirrors the choices in cli/utils.py so the web form offers the same
provider and model menu as the interactive CLI.
"""

from __future__ import annotations

import os
from typing import Any

from tradingagents.llm_clients.model_catalog import MODEL_OPTIONS


def _ollama_default_url() -> str:
    return os.environ.get("OLLAMA_BASE_URL") or "http://localhost:11434/v1"


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


def get_providers() -> list[dict[str, Any]]:
    out = []
    for p in PROVIDERS:
        entry = dict(p)
        if entry["key"] == "ollama":
            entry["base_url"] = _ollama_default_url()
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
