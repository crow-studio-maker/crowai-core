from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from models.registry import ModelRegistry

ROOT = Path(__file__).resolve().parents[2]


def _planner():
    registry = ModelRegistry(ROOT / "models", development=True, strict_capabilities=True)
    package = registry._load("agent/v1.0")
    return importlib.import_module(f"{package.__name__}.planner")


class CommerceStub:
    def marketplace_queries(self, seed: str, *, limit: int):
        values = [
            {"query": f"{seed} marketplace one", "purpose": "shop", "priority": 88, "kind": "product", "domains": ["shop.example"]},
            {"query": f"{seed} marketplace two", "purpose": "shop2", "priority": 70, "kind": "product"},
        ]
        return values[:limit]


def test_agent_planner_depth_intent_and_query_helpers() -> None:
    p = _planner()
    assert p.infer_depth("conversation", "çok detaylı incele", "balanced") == "deep"
    assert p.infer_depth("conversation", "quick answer", "balanced") == "quick"
    assert p.infer_depth("conversation", "normal", "balanced") == "balanced"
    assert p.heuristic_intent("iphone fiyat", has_images=True) == ("visual_product_lookup", True, True, True)
    assert p.heuristic_intent("charger price", has_images=False)[0] == "product_lookup"
    assert p.heuristic_intent("bu görsel nedir", has_images=False)[0] == "visual_analysis"
    assert p.heuristic_intent("latest news", has_images=True)[0] == "visual_lookup"
    assert p.heuristic_intent("https://example.com", has_images=False)[0] == "web_analysis"
    assert p.heuristic_intent("explain local file", has_images=False)[0] == "local_analysis"
    assert p._normalize_query("Search Query:  hello   world  ", maximum_chars=30) == "hello world"
    assert p._query_seed('internette "iPhone 15 charger" şunları yap listele', 50) == "iPhone 15 charger"


def test_agent_contextual_followup_uses_previous_user_intent_but_not_acknowledgements() -> None:
    p = _planner()
    history = [
        {"role": "user", "content": "iPhone 15 için şarj cihazı bul"},
        {"role": "assistant", "content": "ok"},
    ]
    combined, seed = p._contextual_intent_text("iphone", history)
    assert "iPhone 15" in combined and seed == combined
    ack, ack_seed = p._contextual_intent_text("tamam", history)
    assert ack == "tamam" and ack_seed == "tamam"
    long = " ".join(["word"] * 13)
    assert p._contextual_intent_text(long, history) == (long, long)


def test_agent_fallback_plan_product_and_visual_queries_are_bounded() -> None:
    p = _planner()
    plan = p.fallback_plan(
        question="iPhone charger fiyat",
        depth="deep",
        has_images=True,
        visual_analysis={"search_queries": ["visual match", "", "visual match 2"]},
        commerce=CommerceStub(),
        maximum_queries=6,
        query_maximum_chars=60,
    )
    assert plan.intent == "visual_product_lookup"
    assert plan.needs_current_information is True
    assert plan.needs_product_normalization is True
    assert plan.needs_visual_analysis is True
    assert len(plan.queries) <= 6
    assert any(item.kind == "manufacturer" for item in plan.queries)
    assert plan.expected_output == "product_comparison"

    local = p.fallback_plan(
        question="summarize this local document", depth="balanced", has_images=False,
        visual_analysis={}, commerce=CommerceStub(), maximum_queries=4, query_maximum_chars=80,
    )
    assert local.needs_current_information is False
    assert local.queries == []


def test_agent_create_plan_falls_back_on_invalid_model_json(monkeypatch: pytest.MonkeyPatch) -> None:
    p = _planner()
    monkeypatch.setattr(p, "generate_response", lambda *args, **kwargs: "not-json")
    plan = p.create_plan(
        question="latest python release", language="en", interaction_mode="conversation",
        conversation=[], visual_analysis={}, planner_prompt="plan", commerce=CommerceStub(),
        maximum_queries=5, default_depth="balanced", output_tokens=300,
    )
    assert plan.intent == "web_analysis"
    assert plan.needs_current_information is True
    assert plan.queries


def test_agent_create_plan_does_not_allow_model_to_force_web_for_local_request(monkeypatch: pytest.MonkeyPatch) -> None:
    p = _planner()
    model_plan = {
        "objective": "",
        "intent": "made_up_intent",
        "depth": "",
        "needs_current_information": True,
        "queries": [{"query": "forced web", "purpose": "bad", "priority": 99}],
        "needs_visual_analysis": False,
        "needs_product_normalization": False,
    }
    monkeypatch.setattr(p, "generate_response", lambda *args, **kwargs: json.dumps(model_plan))
    plan = p.create_plan(
        question="explain this local concept", language="en", interaction_mode="conversation",
        conversation=[{"role": "assistant", "content": "context"}], visual_analysis={},
        planner_prompt="plan", commerce=CommerceStub(), maximum_queries=4,
        default_depth="balanced", output_tokens=300,
    )
    assert plan.needs_current_information is False
    assert plan.queries == []
    assert plan.objective == "explain this local concept"
    assert plan.intent == "local_analysis"
    assert plan.depth == "balanced"


def test_agent_create_plan_repairs_product_intent_deduplicates_and_limits_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    p = _planner()
    model_plan = {
        "objective": "compare",
        "intent": "web_analysis",
        "depth": "deep",
        "needs_current_information": True,
        "needs_product_normalization": False,
        "queries": [
            {"query": "Search Query: iPhone charger fiyat", "purpose": "a", "priority": 100},
            {"query": "iphone charger fiyat", "purpose": "dup", "priority": 90},
            {"query": " ".join(["long"] * 40), "purpose": "long", "priority": 80},
        ],
    }
    monkeypatch.setattr(p, "generate_response", lambda *args, **kwargs: json.dumps(model_plan))
    plan = p.create_plan(
        question="iPhone charger fiyat karşılaştır", language="tr", interaction_mode="deep",
        conversation=[], visual_analysis={}, planner_prompt="plan", commerce=CommerceStub(),
        maximum_queries=4, default_depth="balanced", output_tokens=300, query_maximum_chars=180,
    )
    assert plan.intent == "product_lookup"
    assert plan.needs_product_normalization is True
    assert plan.needs_current_information is True
    assert len(plan.queries) <= 4
    assert len({item.query.casefold() for item in plan.queries}) == len(plan.queries)
    assert all(len(item.query.split()) <= 28 for item in plan.queries)
