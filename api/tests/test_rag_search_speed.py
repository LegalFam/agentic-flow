import inspect
from types import SimpleNamespace

import pytest
from google.genai import types

from app import gemini_client, main
from app.config import settings
from app.models import RagSearchRequest

PROMPT_ARGS = dict(
    query="pension de alimentos",
    original_message="¿Cómo se fija el monto de la pensión?",
    legal_category="pension de alimentos",
    canonical_category="Relaciones Paterno-Filiales y Menores",
    canonical_subcategories=["Pension de Alimentos"],
    metadata_hints=[],
    search_terms=["alimentos"],
)


def test_full_prompt_is_unchanged_by_default():
    prompt = gemini_client.build_rag_search_prompt(**PROMPT_ARGS)
    assert gemini_client.RAG_BRIEF_INSTRUCTIONS not in prompt
    assert prompt.endswith("No inventes articulos, fuentes ni enlaces.")


def test_brief_prompt_asks_for_a_short_synthesis():
    prompt = gemini_client.build_rag_search_prompt(**PROMPT_ARGS, answer_style="brief")
    assert prompt.endswith(gemini_client.RAG_BRIEF_INSTRUCTIONS)
    assert "150 palabras" in prompt


def test_request_defaults_to_full_and_rejects_unknown_styles():
    assert RagSearchRequest(query="x").answer_style == "full"
    assert RagSearchRequest(query="x", answer_style="brief").answer_style == "brief"
    with pytest.raises(ValueError):
        RagSearchRequest(query="x", answer_style="short")


@pytest.mark.parametrize("model", ["gemini-2.5-flash", "models/gemini-2.5-flash"])
def test_gemini_25_gets_a_token_budget(model, monkeypatch):
    monkeypatch.setattr(settings, "gemini_rag_thinking_budget", 0)
    config = gemini_client.rag_thinking_config(types, model)
    assert config.thinking_budget == 0
    assert config.thinking_level is None


def test_negative_budget_leaves_the_model_default(monkeypatch):
    monkeypatch.setattr(settings, "gemini_rag_thinking_budget", -1)
    assert gemini_client.rag_thinking_config(types, "gemini-2.5-flash") is None


@pytest.mark.parametrize("model", ["gemini-3-flash-preview", "gemini-3.5-flash-lite", "models/gemini-3.1-pro-preview"])
def test_gemini_3_and_later_get_a_level(model, monkeypatch):
    monkeypatch.setattr(settings, "gemini_rag_thinking_level", "minimal")
    config = gemini_client.rag_thinking_config(types, model)
    assert config.thinking_level == types.ThinkingLevel.MINIMAL
    assert config.thinking_budget is None


def test_empty_level_leaves_the_model_default(monkeypatch):
    monkeypatch.setattr(settings, "gemini_rag_thinking_level", "")
    assert gemini_client.rag_thinking_config(types, "gemini-3-flash-preview") is None


def test_unknown_model_family_sends_no_thinking_config():
    assert gemini_client.rag_thinking_config(types, "gemini-2.0-flash") is None


def test_search_sends_the_brief_prompt_and_thinking_config(monkeypatch):
    captured = {}

    class FakeModels:
        def generate_content(self, model, contents, config):
            captured.update(model=model, contents=contents, config=config)
            return SimpleNamespace(text="Sintesis.", candidates=[])

    class FakeClient:
        def __init__(self, api_key):
            self.models = FakeModels()

    monkeypatch.setattr(settings, "gemini_api_key", "test")
    monkeypatch.setattr(settings, "gemini_model", "gemini-2.5-flash")
    monkeypatch.setattr(settings, "gemini_rag_thinking_budget", 0)
    monkeypatch.setattr("google.genai.Client", FakeClient)

    result = gemini_client.search_legal_rag(
        **PROMPT_ARGS, file_search_store_name="fileSearchStores/test", answer_style="brief"
    )

    assert captured["contents"].endswith(gemini_client.RAG_BRIEF_INSTRUCTIONS)
    assert captured["config"].thinking_config.thinking_budget == 0
    assert result["answer"] == "Sintesis."
    assert result["valid"] is False


def test_endpoint_is_sync_so_it_runs_in_the_threadpool():
    # Un `async def` con la llamada bloqueante a Gemini adentro serializa a los usuarios.
    assert not inspect.iscoroutinefunction(main.rag_search)


def test_endpoint_forwards_answer_style(monkeypatch):
    seen = {}

    def fake_search(**kwargs):
        seen.update(kwargs)
        return {"answer": "ok", "citations": [], "valid": False, "retry_suggestion": None}

    monkeypatch.setattr(main, "search_legal_rag", fake_search)
    response = main.rag_search(RagSearchRequest(query="x", answer_style="brief"))

    assert seen["answer_style"] == "brief"
    assert response.answer == "ok"
