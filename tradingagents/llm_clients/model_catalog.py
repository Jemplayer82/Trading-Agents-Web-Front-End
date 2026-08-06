"""Shared model catalog for CLI selections and validation."""

from __future__ import annotations

from typing import Dict, List, Tuple

ModelOption = Tuple[str, str]
ProviderModeOptions = Dict[str, Dict[str, List[ModelOption]]]


# Shared model list for GLM via Z.AI (international) and BigModel (China).
# Source: docs.z.ai (GLM Coding Plan supported models + LLM guides).
# All GLM 4.7+ entries support thinking mode via thinking={"type":"enabled"}.
_GLM_MODELS: Dict[str, List[ModelOption]] = {
    "quick": [
        ("GLM-5-Turbo - Fast, switchable thinking modes", "glm-5-turbo"),
        ("GLM-4.7 - Previous-gen flagship", "glm-4.7"),
        ("GLM-4.5-Air - Lightweight, cost-efficient", "glm-4.5-air"),
        ("Custom model ID", "custom"),
    ],
    "deep": [
        ("GLM-5.1 - Latest flagship, 204K ctx", "glm-5.1"),
        ("GLM-5 - Flagship, 204K ctx", "glm-5"),
        ("GLM-4.7 - Previous-gen flagship", "glm-4.7"),
        ("Custom model ID", "custom"),
    ],
}


# Shared model list for Qwen's global (dashscope-intl) and CN (dashscope) endpoints.
# Source: modelstudio.console.alibabacloud.com (Featured Models — Flagship + Cost-optimized).
#
# Only versioned IDs are exposed in the dropdown. The version-less aliases
# (qwen-plus, qwen-flash) are documented by Alibaba as auto-upgrading
# pointers ("backbone, latest, and snapshot ... have been upgraded to the
# Qwen3 series"), which means their behavior shifts when Alibaba rotates
# the backing model. Users who want a specific generation pick it
# explicitly; users who really want auto-latest can enter the alias via
# "Custom model ID".
_QWEN_MODELS: Dict[str, List[ModelOption]] = {
    "quick": [
        ("Qwen 3.6 Flash - Latest fast, agentic coding + vision-language", "qwen3.6-flash"),
        ("Qwen 3.5 Flash - Previous-gen fast", "qwen3.5-flash"),
        ("Custom model ID", "custom"),
    ],
    "deep": [
        ("Qwen 3.6 Plus - Flagship vision-language, agentic coding SOTA", "qwen3.6-plus"),
        ("Qwen 3.5 Plus - Previous-gen flagship", "qwen3.5-plus"),
        ("Qwen 3 Max - Specialized for agent programming + tool use", "qwen3-max"),
        ("Custom model ID", "custom"),
    ],
}


# Shared model list for MiniMax's global and CN endpoints (same IDs).
# Full official lineup per platform.minimax.io/docs/api-reference/text-openai-api.
# All M2.x models share a 204,800-token context window.
_MINIMAX_MODELS: Dict[str, List[ModelOption]] = {
    "quick": [
        ("MiniMax-M2.7-highspeed - Faster M2.7, 204K ctx, ~100 TPS", "MiniMax-M2.7-highspeed"),
        ("MiniMax-M2.5-highspeed - Previous-gen highspeed, 204K ctx", "MiniMax-M2.5-highspeed"),
        ("MiniMax-M2.1-highspeed - M2.1 highspeed, 204K ctx", "MiniMax-M2.1-highspeed"),
        ("Custom model ID", "custom"),
    ],
    "deep": [
        ("MiniMax-M2.7 - Flagship, SOTA on coding/agent benchmarks, 204K ctx", "MiniMax-M2.7"),
        ("MiniMax-M2.7-highspeed - Same quality as M2.7, ~100 TPS", "MiniMax-M2.7-highspeed"),
        ("MiniMax-M2.5 - Previous-gen flagship, 204K ctx", "MiniMax-M2.5"),
        ("MiniMax-M2.1 - Earlier M2 line, 204K ctx", "MiniMax-M2.1"),
        ("MiniMax-M2 - Base M2, 204K ctx", "MiniMax-M2"),
        ("Custom model ID", "custom"),
    ],
}


MODEL_OPTIONS: ProviderModeOptions = {
    "openai": {
        "quick": [
            ("GPT-5.4 Mini - Fast, strong coding and tool use", "gpt-5.4-mini"),
            ("GPT-5.4 Nano - Cheapest, high-volume tasks", "gpt-5.4-nano"),
            ("GPT-5.5 - Latest frontier, 1M context", "gpt-5.5"),
            ("GPT-4.1 - Smartest non-reasoning model", "gpt-4.1"),
        ],
        "deep": [
            ("GPT-5.5 - Latest frontier, 1M context", "gpt-5.5"),
            ("GPT-5.4 - Previous-gen frontier, 1M context, cost-effective", "gpt-5.4"),
            ("GPT-5.2 - Strong reasoning, cost-effective", "gpt-5.2"),
            ("GPT-5.5 Pro - Most capable, expensive ($30/$180 per 1M tokens)", "gpt-5.5-pro"),
        ],
    },
    # Anthropic. Source: the "Claude API alias" row of
    # https://platform.claude.com/docs/en/about-claude/models/overview.md
    #
    # From the 4.6 generation on, those dateless IDs are pinned snapshots, not
    # evergreen pointers — a stale entry here quietly runs an older model rather
    # than 404ing, so drift is silent. Check it (free, no API key needed) with:
    #
    #     python scripts/check_model_catalog.py
    #
    # There is no Sonnet 4.8: the Sonnet line runs 4.5 → 4.6 → 5, and only Opus
    # has a 4.8. Claude Fable 5 is deliberately absent — it is double Opus
    # pricing, thinking is always on (an explicit thinking=disabled is a 400),
    # it requires 30-day data retention, and it can answer with
    # stop_reason="refusal". AnthropicClient handles none of those yet.
    #
    # Last verified 2026-08-06.
    "anthropic": {
        "quick": [
            ("Claude Sonnet 5 - Best speed and intelligence balance", "claude-sonnet-5"),
            ("Claude Haiku 4.5 - Fastest with near-frontier intelligence", "claude-haiku-4-5"),
            ("Claude Sonnet 4.6 - Previous-gen balanced", "claude-sonnet-4-6"),
        ],
        "deep": [
            ("Claude Opus 5 - Latest frontier, complex agentic and enterprise work", "claude-opus-5"),
            ("Claude Opus 4.8 - Previous frontier Opus, long-running agents", "claude-opus-4-8"),
            ("Claude Sonnet 5 - Near-Opus quality at Sonnet cost", "claude-sonnet-5"),
            ("Claude Opus 4.7 - Older Opus, still available", "claude-opus-4-7"),
        ],
    },
    "google": {
        "quick": [
            ("Gemini 3 Flash - Next-gen fast (preview)", "gemini-3-flash-preview"),
            ("Gemini 2.5 Flash - Balanced, stable", "gemini-2.5-flash"),
            ("Gemini 3.1 Flash Lite - Most cost-efficient (GA)", "gemini-3.1-flash-lite"),
            ("Gemini 2.5 Flash Lite - Fast, low-cost", "gemini-2.5-flash-lite"),
        ],
        "deep": [
            ("Gemini 3.1 Pro - Reasoning-first, complex workflows (preview)", "gemini-3.1-pro-preview"),
            ("Gemini 3 Flash - Next-gen fast (preview)", "gemini-3-flash-preview"),
            ("Gemini 2.5 Pro - Stable pro model", "gemini-2.5-pro"),
            ("Gemini 2.5 Flash - Balanced, stable", "gemini-2.5-flash"),
        ],
    },
    "xai": {
        "quick": [
            ("Grok 4.20 (Non-Reasoning) - Latest, speed-optimized", "grok-4.20-non-reasoning"),
            ("Grok 4 Fast (Non-Reasoning) - Speed optimized", "grok-4-fast-non-reasoning"),
            ("Grok 4 Fast (Reasoning) - High-performance", "grok-4-fast-reasoning"),
        ],
        "deep": [
            ("Grok 4.20 (Reasoning) - Latest frontier reasoning model", "grok-4.20-reasoning"),
            ("Grok 4 - Flagship (dated build)", "grok-4-0709"),
            ("Grok 4 Fast (Reasoning) - High-performance", "grok-4-fast-reasoning"),
            ("Grok 4.20 - Auto-select reasoning behavior", "grok-4.20"),
        ],
    },
    "deepseek": {
        "quick": [
            ("DeepSeek V4 Flash - Latest V4 fast model", "deepseek-v4-flash"),
            ("DeepSeek V3.2", "deepseek-chat"),
            ("Custom model ID", "custom"),
        ],
        "deep": [
            ("DeepSeek V4 Pro - Latest V4 flagship model", "deepseek-v4-pro"),
            ("DeepSeek V3.2 (thinking)", "deepseek-reasoner"),
            ("DeepSeek V3.2", "deepseek-chat"),
            ("Custom model ID", "custom"),
        ],
    },
    # Qwen: same model IDs across global (dashscope-intl) and China
    # (dashscope) endpoints, so the two provider keys share one model list.
    "qwen": _QWEN_MODELS,
    "qwen-cn": _QWEN_MODELS,
    # GLM: Z.AI (international) and BigModel (China) host the same model
    # IDs; the two provider keys share one model list.
    "glm": _GLM_MODELS,
    "glm-cn": _GLM_MODELS,
    # MiniMax: same model IDs across global (.io) and China (.com) regions,
    # so the two provider keys share one model list.
    "minimax": _MINIMAX_MODELS,
    "minimax-cn": _MINIMAX_MODELS,
    # OpenRouter: fetched dynamically. Azure: any deployed model name.
    # Ollama display labels intentionally omit a "local" marker — the
    # endpoint is now configurable via OLLAMA_BASE_URL, so the same labels
    # apply whether the user runs ollama-serve on localhost or against a
    # remote host. The actual resolved endpoint is surfaced separately by
    # cli.utils.confirm_ollama_endpoint() right after provider selection.
    # "Custom model ID" lets users pick any model they have pulled via
    # `ollama pull` beyond the three suggested defaults.
    # Revalidate against the live list before editing — Ollama Cloud retires
    # tags without notice and a stale entry here is a hard 404 at scan time,
    # not a graceful fallback (the previous qwen3:latest / gpt-oss:latest /
    # glm-4.7-flash:latest set all went dead this way). To refresh:
    #   curl -sH "Authorization: Bearer $OLLAMA_API_KEY" \
    #        https://ollama.com/v1/models | jq -r '.data[].id' | sort
    # Last verified 2026-07-25 (every id below smoke-tested with a live
    # chat.completions call, not just presence in the list).
    "ollama": {
        "quick": [
            ("GPT-OSS 20B", "gpt-oss:20b"),
            ("GLM-5.2", "glm-5.2"),
            ("DeepSeek V4 Flash", "deepseek-v4-flash"),
            ("Custom model ID", "custom"),
        ],
        "deep": [
            ("GPT-OSS 120B", "gpt-oss:120b"),
            ("Qwen3.5 397B", "qwen3.5:397b"),
            ("GLM-5.2", "glm-5.2"),
            ("Custom model ID", "custom"),
        ],
    },
    # Switchboard reaches Claude through Cleo, which shells out to `claude -p`
    # against the host user's CLI subscription — so these cost nothing per token
    # and are the route the deployed stack actually uses. Model IDs are handed
    # to `claude --model` verbatim, so they track the same catalog as the
    # "anthropic" provider above; non-Claude entries fall through to llm-router.
    #
    # Haiku keeps its dated ID here because that exact string is what the
    # scheduler's fallback config sends (web/scheduler.py) and what the running
    # deployment has been exercising.
    "switchboard": {
        "quick": [
            ("Claude Sonnet 5 (via bus)", "claude-sonnet-5"),
            ("Claude Haiku 4.5 (via bus)", "claude-haiku-4-5-20251001"),
            ("Claude Sonnet 4.6 (via bus)", "claude-sonnet-4-6"),
            ("Llama 3 (via bus)", "llama3"),
            ("Custom model ID", "custom"),
        ],
        "deep": [
            ("Claude Opus 5 (via bus)", "claude-opus-5"),
            ("Claude Opus 4.8 (via bus)", "claude-opus-4-8"),
            ("Claude Sonnet 5 (via bus)", "claude-sonnet-5"),
            ("Custom model ID", "custom"),
        ],
    },
}


def get_model_options(provider: str, mode: str) -> List[ModelOption]:
    """Return shared model options for a provider and selection mode."""
    return MODEL_OPTIONS[provider.lower()][mode]


def get_known_models() -> Dict[str, List[str]]:
    """Build known model names from the shared CLI catalog."""
    return {
        provider: sorted(
            {
                value
                for options in mode_options.values()
                for _, value in options
            }
        )
        for provider, mode_options in MODEL_OPTIONS.items()
    }
