"""Per-role (quick vs deep) LLM provider selection.

Before this, one `provider` field drove both the Deep and Quick model
clients — always the same provider. These tests cover the three layers that
each independently collapsed quick/deep onto a single provider:

  1. web.runner.build_config() — resolves quick_llm_provider/deep_llm_provider
     (and their backend_urls) from params, falling back to the legacy single
     `provider` key so old saved preferences keep working unchanged.
  2. SwitchboardOrchestrator — builds its deep/quick LLM clients from those
     per-role keys instead of one shared config["llm_provider"].
  3. web.llm_helpers.llm_for() — the second factory used by Portfolio/SPY/QA,
     fed by the same build_config() output.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.orchestrator.switchboard_orchestrator import SwitchboardOrchestrator
from web import llm_helpers
from web.runner import build_config


@pytest.mark.unit
class TestBuildConfigPerRoleProvider:
    def test_explicit_quick_and_deep_provider_resolve_independently(self):
        cfg = build_config({"quick_provider": "ollama", "deep_provider": "switchboard"})
        assert cfg["quick_llm_provider"] == "ollama"
        assert cfg["deep_llm_provider"] == "switchboard"

    def test_legacy_single_provider_key_applies_to_both_roles(self):
        """Old saved preferences only ever set `provider` — both roles must
        keep resolving to it, unchanged, until the user explicitly diverges."""
        cfg = build_config({"provider": "openai"})
        assert cfg["quick_llm_provider"] == "openai"
        assert cfg["deep_llm_provider"] == "openai"

    def test_default_provider_is_ollama_when_nothing_set(self):
        cfg = build_config({})
        assert cfg["quick_llm_provider"] == "ollama"
        assert cfg["deep_llm_provider"] == "ollama"

    def test_llm_provider_alias_kept_for_legacy_readers(self):
        """portfolio_main.py / spy_scanner.py read cfg["llm_provider"] as a
        single display field — keep it populated, aliased to the deep role."""
        cfg = build_config({"quick_provider": "ollama", "deep_provider": "switchboard"})
        assert cfg["llm_provider"] == "switchboard"

    def test_one_role_explicit_other_falls_back_to_legacy_provider(self):
        cfg = build_config({"provider": "anthropic", "deep_provider": "switchboard"})
        assert cfg["deep_llm_provider"] == "switchboard"
        assert cfg["quick_llm_provider"] == "anthropic"


@pytest.mark.unit
class TestBuildConfigPerRoleBackendUrl:
    def test_ollama_base_url_applies_only_to_the_ollama_role(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_BASE_URL", "http://local-ollama:11434")
        cfg = build_config({"quick_provider": "ollama", "deep_provider": "switchboard"})
        assert cfg["quick_backend_url"] == "http://local-ollama:11434"
        assert cfg["deep_backend_url"] != "http://local-ollama:11434"

    def test_explicit_per_role_backend_url_override(self):
        cfg = build_config({
            "quick_provider": "openai",
            "quick_backend_url": "https://quick.example/v1",
            "deep_provider": "anthropic",
            "deep_backend_url": "https://deep.example/v1",
        })
        assert cfg["quick_backend_url"] == "https://quick.example/v1"
        assert cfg["deep_backend_url"] == "https://deep.example/v1"

    def test_backend_url_alias_kept_for_legacy_readers(self, monkeypatch):
        monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
        cfg = build_config({
            "quick_provider": "openai",
            "deep_provider": "anthropic",
            "deep_backend_url": "https://deep.example/v1",
        })
        assert cfg["backend_url"] == "https://deep.example/v1"


# ---------------------------------------------------------------------------
# SwitchboardOrchestrator: builds deep/quick clients from per-role config
# ---------------------------------------------------------------------------
#
# Patch tradingagents.orchestrator.switchboard_orchestrator.create_llm_client,
# NOT tradingagents.llm_clients.factory.create_llm_client — the orchestrator
# module does `from tradingagents.llm_clients import create_llm_client` at
# import time, binding a local name. Patching the factory module doesn't
# touch that already-bound reference.


def _orchestrator_config(tmp_path, **overrides) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    cfg["data_cache_dir"] = str(tmp_path / "cache")
    cfg["results_dir"] = str(tmp_path / "logs")
    cfg.update(overrides)
    return cfg


@pytest.mark.unit
class TestProviderKwargsIsRoleScoped:
    def _orchestrator(self, tmp_path, monkeypatch, **config_overrides):
        fake_client = MagicMock()
        fake_client.get_llm.return_value = MagicMock()
        monkeypatch.setattr(
            "tradingagents.orchestrator.switchboard_orchestrator.create_llm_client",
            MagicMock(return_value=fake_client),
        )
        config = _orchestrator_config(tmp_path, **config_overrides)
        return SwitchboardOrchestrator(config=config)

    def test_anthropic_effort_attaches_only_for_anthropic_role(self, tmp_path, monkeypatch):
        orch = self._orchestrator(
            tmp_path, monkeypatch,
            deep_llm_provider="anthropic", quick_llm_provider="ollama",
            anthropic_effort="high",
        )
        assert orch._provider_kwargs("anthropic") == {"effort": "high"}
        assert orch._provider_kwargs("ollama") == {}


@pytest.mark.unit
class TestOrchestratorBuildsPerRoleClients:
    def test_deep_and_quick_clients_get_different_providers(self, tmp_path, monkeypatch):
        fake_client = MagicMock()
        fake_client.get_llm.return_value = MagicMock()
        create_llm_client_mock = MagicMock(return_value=fake_client)
        monkeypatch.setattr(
            "tradingagents.orchestrator.switchboard_orchestrator.create_llm_client",
            create_llm_client_mock,
        )
        config = _orchestrator_config(
            tmp_path,
            deep_llm_provider="switchboard", deep_backend_url="https://bus.example",
            quick_llm_provider="ollama", quick_backend_url="http://local-ollama:11434",
        )
        SwitchboardOrchestrator(config=config)

        calls_by_provider = {c.kwargs["provider"]: c.kwargs for c in create_llm_client_mock.call_args_list}
        assert set(calls_by_provider) == {"switchboard", "ollama"}
        assert calls_by_provider["switchboard"]["base_url"] == "https://bus.example"
        assert calls_by_provider["ollama"]["base_url"] == "http://local-ollama:11434"

    def test_falls_back_to_legacy_llm_provider_when_per_role_keys_absent(self, tmp_path, monkeypatch):
        """A config built by an older code path (only `llm_provider` set, no
        quick_llm_provider/deep_llm_provider) must still work unchanged."""
        fake_client = MagicMock()
        fake_client.get_llm.return_value = MagicMock()
        create_llm_client_mock = MagicMock(return_value=fake_client)
        monkeypatch.setattr(
            "tradingagents.orchestrator.switchboard_orchestrator.create_llm_client",
            create_llm_client_mock,
        )
        config = _orchestrator_config(tmp_path, llm_provider="openai")
        SwitchboardOrchestrator(config=config)

        providers_used = {c.kwargs["provider"] for c in create_llm_client_mock.call_args_list}
        assert providers_used == {"openai"}


# ---------------------------------------------------------------------------
# web.llm_helpers.llm_for(): second factory used by Portfolio/SPY/QA, fed by
# the same build_config() output — must resolve provider by role too.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_stored_credentials(monkeypatch):
    """These tests don't care about DB-stored credentials — skip that lookup
    so they don't depend on (or write to) a real database file."""
    monkeypatch.setattr("web.db.get_credential", lambda provider: None)


@pytest.mark.unit
class TestLlmForRoleResolution:
    def test_deep_and_quick_use_their_own_provider(self, monkeypatch):
        """Model selection was already role-aware (the `deep` flag) — the bug
        was PROVIDER always resolving to one shared value. Assert on api_key,
        which is resolved from the provider string and differs meaningfully
        between openai (env var) and ollama (dummy fallback "ollama")."""
        chat_openai_mock = MagicMock()
        monkeypatch.setattr(llm_helpers, "ChatOpenAI", chat_openai_mock)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-openai-key")
        monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
        config = {
            "deep_llm_provider": "openai",
            "quick_llm_provider": "ollama",
            "deep_think_llm": "gpt-5.4",
            "quick_think_llm": "llama3",
        }

        llm_helpers.llm_for(config, deep=True)
        llm_helpers.llm_for(config, deep=False)

        deep_kwargs, quick_kwargs = (c.kwargs for c in chat_openai_mock.call_args_list)
        assert deep_kwargs["model"] == "gpt-5.4"
        assert deep_kwargs["api_key"] == "sk-test-openai-key"
        assert quick_kwargs["model"] == "llama3"
        assert quick_kwargs["api_key"] == "ollama"

    def test_falls_back_to_legacy_llm_provider(self, monkeypatch):
        chat_openai_mock = MagicMock()
        monkeypatch.setattr(llm_helpers, "ChatOpenAI", chat_openai_mock)
        config = {"llm_provider": "openai", "deep_think_llm": "gpt-5.4"}

        llm_helpers.llm_for(config, deep=True)

        assert chat_openai_mock.call_args.kwargs["model"] == "gpt-5.4"

    def test_switchboard_role_routes_through_create_llm_client(self, monkeypatch):
        fake_client = MagicMock()
        fake_client.get_llm.return_value = "the-llm"
        create_llm_client_mock = MagicMock(return_value=fake_client)
        monkeypatch.setattr(
            "tradingagents.llm_clients.create_llm_client", create_llm_client_mock
        )
        config = {"deep_llm_provider": "switchboard", "quick_llm_provider": "ollama", "deep_think_llm": "claude-opus-4-8"}

        result = llm_helpers.llm_for(config, deep=True)

        assert result == "the-llm"
        create_llm_client_mock.assert_called_once_with(provider="switchboard", model="claude-opus-4-8")


@pytest.mark.unit
class TestResolveBaseUrlPerRole:
    def test_prefers_role_specific_backend_url(self):
        config = {"quick_backend_url": "http://local-ollama:11434", "deep_backend_url": "https://deep.example"}
        assert llm_helpers._resolve_base_url("ollama", config, deep=False) == "http://local-ollama:11434"
        assert llm_helpers._resolve_base_url("anthropic", config, deep=True) == "https://deep.example"

    def test_falls_back_to_legacy_backend_url(self):
        config = {"backend_url": "https://legacy.example"}
        assert llm_helpers._resolve_base_url("openai", config, deep=True) == "https://legacy.example"
        assert llm_helpers._resolve_base_url("openai", config, deep=False) == "https://legacy.example"
