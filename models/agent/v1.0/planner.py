"""Bounded query planning and intent routing for Agent V1.0."""

from __future__ import annotations

import json
import re
from typing import Any

from .commerce import CommerceNormalizer
from .engine import LocalAgentError, generate_response
from .schemas import AgentPlan, SearchQuery


_PRODUCT_MARKERS = (
    "satın al", "fiyat", "en ucuz", "ürün", "trendyol", "hepsiburada", "amazon",
    "n11", "stok", "kargo", "satıcı", "yorum", "karşılaştır", "şarj aleti",
    "şarj cihazı", "adaptör", "adapter", "charger", "aksesuar", "accessory",
    "kulaklık", "headphone", "earbuds", "powerbank", "power bank", "kılıf", "case",
    "buy", "price", "cheapest", "product", "marketplace", "seller", "shipping",
)
_VISUAL_MARKERS = (
    "bu görsel", "bu resim", "fotoğraftaki", "ekran görüntüsü", "image", "photo",
    "screenshot", "visual", "görsel", "resim", "fotoğraf",
)
_CURRENT_MARKERS = (
    "web", "internette", "internet", "ara", "bul", "güncel", "şu an", "bugün",
    "latest", "current", "today", "news", "haber", "search", "lookup", "online",
    "site", "url", "link", "kaynak", "source", "doğrula", "verify",
)
_INSTRUCTION_MARKERS = (
    "şunları yap", "her ürün için", "sonunda", "kaynakları", "listele", "karşılaştır",
    "incele", "fiyatları", "kullanıcı yorumlarını",
)


def _clean(value: Any, limit: int = 8000) -> str:
    return str(value or "").replace("\x00", "").strip()[:limit]


def _json_object(raw: str) -> dict[str, Any]:
    text = raw.strip()
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            value = json.loads(text[start : end + 1])
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass
    raise LocalAgentError("The planner did not return valid JSON.")


def infer_depth(interaction_mode: str, question: str, default: str) -> str:
    text = f"{interaction_mode} {question}".casefold()
    if any(marker in text for marker in ("derin", "kapsamlı", "çok detaylı", "deep", "comprehensive")):
        return "deep"
    if any(marker in text for marker in ("hızlı", "kısaca", "quick", "brief")):
        return "quick"
    return default


def heuristic_intent(
    question: str,
    *,
    has_images: bool,
) -> tuple[str, bool, bool, bool]:
    text = question.casefold()
    product = any(marker in text for marker in _PRODUCT_MARKERS)
    visual = has_images or any(marker in text for marker in _VISUAL_MARKERS)
    direct_url = bool(re.search(r"https?://", question, flags=re.IGNORECASE))
    current = product or direct_url or any(marker in text for marker in _CURRENT_MARKERS)

    if product and visual:
        return "visual_product_lookup", True, True, True
    if product:
        return "product_lookup", True, False, True
    if visual and current:
        return "visual_lookup", False, True, True
    if visual:
        return "visual_analysis", False, True, False
    if current:
        return "web_analysis", False, False, True
    return "local_analysis", False, False, False


def _query_seed(question: str, maximum_chars: int) -> str:
    raw = " ".join(_clean(question, 4000).split())
    quoted = re.findall(r'["“”](.{3,160}?)["“”]', raw)
    candidate = max(quoted, key=len) if quoted else re.split(r"(?:\.\s+|;\s+|\n+)", raw, maxsplit=1)[0]
    candidate = re.sub(r"^\s*(?:türkiye'?de|internette|webde)\s+", "", candidate, flags=re.IGNORECASE)
    for marker in _INSTRUCTION_MARKERS:
        position = candidate.casefold().find(marker)
        if position > 12:
            candidate = candidate[:position]
            break
    candidate = re.sub(r"\b(?:1\.|2\.|3\.|4\.|5\.|6\.|7\.|8\.|9\.|10\.)\b", " ", candidate)
    candidate = re.sub(r"\s{2,}", " ", candidate).strip(" .,:;-")
    return candidate[:maximum_chars] or raw[:maximum_chars]


def _normalize_query(value: str, *, maximum_chars: int) -> str:
    text = " ".join(_clean(value, maximum_chars * 2).split())
    text = re.sub(r"^(?:arama sorgusu|search query)\s*[:\-]\s*", "", text, flags=re.IGNORECASE)
    return text[:maximum_chars].strip(" .,:;-")


def _contextual_intent_text(
    question: str,
    conversation: list[dict[str, str]],
) -> tuple[str, str]:
    """Carry a short follow-up into the immediately preceding user intent."""

    compact = " ".join(question.split()).strip()
    words = compact.split()
    if len(words) > 12:
        return compact, compact
    folded = compact.casefold().strip(" .!?,")
    if folded in {"tamam", "ok", "okay", "teşekkürler", "teşekkür", "sağ ol", "sağol", "thanks", "thank you"}:
        return compact, compact
    for item in reversed(conversation[-6:]):
        if not isinstance(item, dict) or str(item.get("role") or "").casefold() != "user":
            continue
        previous = _clean(item.get("content"), 1800)
        if not previous or previous.strip() == compact:
            continue
        combined = f"{compact} {previous}".strip()
        return combined, combined
    return compact, compact


def fallback_plan(
    *,
    question: str,
    depth: str,
    has_images: bool,
    visual_analysis: dict[str, Any],
    commerce: CommerceNormalizer,
    maximum_queries: int,
    query_maximum_chars: int,
    intent_text: str | None = None,
    seed_text: str | None = None,
) -> AgentPlan:
    intent, product, visual, current = heuristic_intent(intent_text or question, has_images=has_images)
    seed = _query_seed(seed_text or question, query_maximum_chars)
    queries: list[SearchQuery] = []

    if current and seed:
        queries.append(SearchQuery(query=seed, purpose="Find current relevant information", priority=100, kind="web"))
        if product:
            queries.extend(
                [
                    SearchQuery(query=f"{seed} resmi site teknik özellikler", purpose="Find official specifications", priority=94, kind="manufacturer"),
                    SearchQuery(query=f"{seed} fiyat Türkiye", purpose="Find current Turkish prices", priority=98, kind="product"),
                    SearchQuery(query=f"{seed} inceleme kullanıcı yorumları", purpose="Find independent reviews and user experience", priority=80, kind="review"),
                ]
            )

    if current and visual_analysis:
        for query in visual_analysis.get("search_queries", []):
            text = _normalize_query(str(query), maximum_chars=query_maximum_chars)
            if text:
                queries.append(SearchQuery(query=text, purpose="Find a visual or product match", priority=96, kind="image"))

    if product and current:
        remaining = max(0, maximum_queries - len(queries))
        queries.extend(
            SearchQuery(
                query=item["query"], purpose=item["purpose"], priority=item["priority"],
                kind=item["kind"], domains=item.get("domains", []),
            )
            for item in commerce.marketplace_queries(seed, limit=remaining)
        )

    return AgentPlan(
        objective=question,
        intent=intent,
        depth=depth,
        queries=queries[:maximum_queries],
        filters={},
        comparison_criteria=(
            ["exact model and variant match", "total cost", "source reliability", "availability", "seller reliability", "delivery", "warranty"]
            if product else ["relevance", "source reliability", "recency", "agreement between sources"]
        ),
        needs_visual_analysis=visual,
        needs_product_normalization=product,
        needs_current_information=current,
        expected_output="product_comparison" if product else "answer",
    )


def create_plan(
    *,
    question: str,
    language: str,
    interaction_mode: str,
    conversation: list[dict[str, str]],
    visual_analysis: dict[str, Any],
    planner_prompt: str,
    commerce: CommerceNormalizer,
    maximum_queries: int,
    default_depth: str,
    output_tokens: int,
    query_maximum_chars: int = 180,
) -> AgentPlan:
    depth = infer_depth(interaction_mode, question, default_depth)
    has_images = bool(visual_analysis.get("images"))
    intent_text, seed_text = _contextual_intent_text(question, conversation)
    fallback = fallback_plan(
        question=question, depth=depth, has_images=has_images, visual_analysis=visual_analysis,
        commerce=commerce, maximum_queries=maximum_queries, query_maximum_chars=query_maximum_chars,
        intent_text=intent_text, seed_text=seed_text,
    )
    seed = _query_seed(seed_text, query_maximum_chars)
    history = [
        {"role": item.get("role"), "content": _clean(item.get("content"), 2200)}
        for item in conversation[-4:]
        if isinstance(item, dict) and item.get("role") in {"user", "assistant"} and _clean(item.get("content"), 2200)
    ]
    user_payload = {
        "question": question,
        "search_seed": seed,
        "response_language": language,
        "analysis_depth": depth,
        "visual_analysis": visual_analysis,
        "heuristic_intent": fallback.intent,
        "heuristic_needs_current_information": fallback.needs_current_information,
        "recent_conversation": history,
        "maximum_queries": maximum_queries,
        "maximum_query_characters": query_maximum_chars,
    }

    try:
        raw = generate_response(
            [
                {"role": "system", "content": planner_prompt},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
            maximum_tokens=output_tokens,
            temperature=0.1,
            json_mode=True,
        )
        plan = AgentPlan.from_dict(_json_object(raw))
    except (LocalAgentError, ValueError, TypeError):
        return fallback

    # A local-only heuristic is authoritative: the planner may not force a web call
    # for an uploaded file/image unless the user asked for current/external data.
    if not fallback.needs_current_information:
        plan.needs_current_information = False
        plan.queries = []
    else:
        plan.needs_current_information = True
        if not plan.queries:
            plan.queries = fallback.queries

    if not plan.objective:
        plan.objective = question
    if plan.intent not in {
        "local_analysis", "web_analysis", "product_lookup", "visual_analysis",
        "visual_lookup", "visual_product_lookup", "document_analysis",
    }:
        plan.intent = fallback.intent
    if fallback.needs_product_normalization and plan.intent not in {"product_lookup", "visual_product_lookup"}:
        plan.intent = fallback.intent
    if not plan.depth:
        plan.depth = depth
    plan.needs_visual_analysis = plan.needs_visual_analysis or fallback.needs_visual_analysis
    plan.needs_product_normalization = plan.needs_product_normalization or fallback.needs_product_normalization

    if plan.needs_product_normalization and plan.needs_current_information:
        existing = {item.query.casefold() for item in plan.queries}
        remaining = max(0, maximum_queries - len(plan.queries))
        for item in commerce.marketplace_queries(seed, limit=remaining):
            if item["query"].casefold() in existing:
                continue
            plan.queries.append(
                SearchQuery(
                    query=item["query"], purpose=item["purpose"], priority=item["priority"],
                    kind=item["kind"], domains=item.get("domains", []),
                )
            )

    unique: list[SearchQuery] = []
    seen: set[str] = set()
    for item in sorted(plan.queries, key=lambda query: -query.priority):
        item.query = _normalize_query(item.query, maximum_chars=query_maximum_chars)
        key = item.query.casefold().strip()
        if not key or key in seen:
            continue
        if len(item.query.split()) > 28:
            item.query = " ".join(item.query.split()[:28])
        seen.add(key)
        unique.append(item)
        if len(unique) >= maximum_queries:
            break
    plan.queries = unique if plan.needs_current_information else []
    return plan
