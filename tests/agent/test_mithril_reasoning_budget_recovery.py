"""Mithril / reasoning-budget starvation must not burn empty retries at the same max_tokens.

api.mithril.fund returns HTTP 200 with empty visible content when reasoning consumes the
token budget (reasoning stripped unless include_reasoning; x-mithril-notice:
reasoning_budget_exhausted). The empty-response ladder used to re-bill the same starved
budget and surface "No reply: … didn't produce a reply".
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

sys.modules.setdefault("fire", types.SimpleNamespace(Fire=lambda *a, **k: None))
sys.modules.setdefault("firecrawl", types.SimpleNamespace(Firecrawl=object))
sys.modules.setdefault("fal_client", types.SimpleNamespace())


def _build_agent(tmp_path, monkeypatch, *, base_url: str, max_tokens: int = 1024):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / ".env").write_text("", encoding="utf-8")
    (tmp_path / "config.yaml").write_text("{}\n", encoding="utf-8")
    from run_agent import AIAgent

    agent = AIAgent(
        model="qwen/qwen3.8-27b",
        api_key="mf_dummy",
        base_url=base_url,
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        platform="cli",
        max_tokens=max_tokens,
    )
    agent._disable_streaming = True
    return agent


def _empty_length_response(*, completion_tokens: int = 20):
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(
                content="",
                reasoning=None,
                reasoning_content=None,
                reasoning_details=None,
                tool_calls=None,
            ),
            finish_reason="length",
        )],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=completion_tokens, total_tokens=30),
        model="qwen/qwen3.8-27b",
    )


def _empty_stop_response(*, completion_tokens: int = 20):
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(
                content="",
                reasoning=None,
                reasoning_content=None,
                reasoning_details=None,
                tool_calls=None,
            ),
            finish_reason="stop",
        )],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=completion_tokens, total_tokens=30),
        model="qwen/qwen3.8-27b",
    )


def _text_response(content: str):
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(
                content=content,
                reasoning=None,
                reasoning_content=None,
                reasoning_details=None,
                tool_calls=None,
            ),
            finish_reason="stop",
        )],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        model="qwen/qwen3.8-27b",
    )


def test_mithril_empty_stop_retries_once_with_higher_max_tokens(tmp_path, monkeypatch):
    """Mithril empty+billed completion: one recovery with boosted max_tokens, then answer."""
    agent = _build_agent(tmp_path, monkeypatch, base_url="https://api.mithril.fund/v1")
    seen_caps: list[int | None] = []

    responses = [_empty_stop_response(), _text_response("Visible answer after budget recovery.")]

    def _fake_api(api_kwargs):
        seen_caps.append(api_kwargs.get("max_tokens") or api_kwargs.get("max_completion_tokens"))
        return responses.pop(0)

    monkeypatch.setattr(agent, "_interruptible_api_call", _fake_api)

    result = agent.run_conversation("hello?")

    assert "Visible answer" in (result["final_response"] or "")
    assert result["api_calls"] == 2
    assert not (result["final_response"] or "").startswith("⚠️ No reply:")
    assert agent._reasoning_budget_recovery_attempted is True
    # Second call must carry a larger completion budget than the configured 1024.
    assert any(cap is not None and cap > 1024 for cap in seen_caps)


def test_mithril_true_empty_still_reaches_explainer_after_recovery(tmp_path, monkeypatch):
    """If recovery still returns empty, the existing No-reply explainer is kept."""
    agent = _build_agent(tmp_path, monkeypatch, base_url="https://api.mithril.fund/v1")
    # Non-Mithril-shaped true empty (no billed completion tokens) on a non-mithril URL
    # still uses the empty ladder / explainer — covered elsewhere. Here: recovery burns
    # one attempt then the ladder terminates with the explainer.
    monkeypatch.setattr(
        agent, "_interruptible_api_call",
        lambda api_kwargs: _empty_stop_response(completion_tokens=0),
    )

    result = agent.run_conversation("hello?")

    final = result["final_response"] or ""
    assert final == "(empty)" or "No reply:" in final
