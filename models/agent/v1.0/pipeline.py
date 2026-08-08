"""Multimodal document, visual, web and commerce pipeline for Agent V1.0."""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .commerce import CommerceNormalizer
from .document_tools import inspect_document
from .engine import LocalAgentError, _cancel_active, begin_request, generate_response
from .planner import create_plan
from .schemas import AgentPlan, ProductOffer, SearchQuery, SourceRecord
from .search_backends import search_plan
from .security import sanitize_untrusted_text
from .storage import AgentStorage
from .vision import analyze_images
from .web_tools import HttpFetcher, source_records_from_result
from models.runtime_state import model_state_dir


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
_URL_RE = re.compile(
    r"https?://[^\s<>\]\[(){}\"']+",
    flags=re.IGNORECASE,
)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return {}

    return value if isinstance(value, dict) else {}


CONFIG = _load_json(CONFIG_PATH)


def _read_prompt(key: str) -> str:
    relative = str(CONFIG.get(key) or "").strip()

    if not relative:
        return ""

    try:
        return (
            BASE_DIR / relative
        ).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


PLANNER_PROMPT = _read_prompt("planner_prompt_file")
SYNTHESIZER_PROMPT = _read_prompt("synthesizer_prompt_file")
VISION_PROMPT = _read_prompt("vision_prompt_file")

STATE_DIR = model_state_dir(BASE_DIR, "agent", "v1.0")
_STATE_DATABASE_NAME = str(CONFIG.get("state_database_name", "agent_cache.sqlite3")).strip()
if (
    not _STATE_DATABASE_NAME
    or Path(_STATE_DATABASE_NAME).name != _STATE_DATABASE_NAME
    or _STATE_DATABASE_NAME in {".", ".."}
):
    raise RuntimeError("Agent V1.0 state database name must be a package-declared filename, not a path.")
STORAGE = AgentStorage(
    STATE_DIR / _STATE_DATABASE_NAME,
    max_page_rows=int(CONFIG.get("cache_max_page_rows", 500)),
    max_product_rows=int(CONFIG.get("cache_max_product_rows", 500)),
    max_session_rows=int(CONFIG.get("session_max_rows", 500)),
    session_ttl_seconds=int(CONFIG.get("session_ttl_seconds", 604800)),
    maintenance_interval=int(CONFIG.get("cache_maintenance_interval", 32)),
)

COMMERCE = CommerceNormalizer(
    BASE_DIR / str(
        CONFIG.get("sites_file", "sites.json")
    )
)

FETCHER = HttpFetcher(
    storage=STORAGE,
    user_agent=str(
        CONFIG.get(
            "generic_user_agent",
            "CrowAI-Agent/1.0",
        )
    ),
    timeout_seconds=int(
        CONFIG.get("page_timeout_seconds", 18)
    ),
    maximum_bytes=int(
        CONFIG.get("maximum_page_bytes", 3_000_000)
    ),
    maximum_source_chars=int(
        CONFIG.get("maximum_source_chars", 5500)
    ),
    cache_ttl_seconds=int(
        CONFIG.get("page_cache_ttl_seconds", 1200)
    ),
    respect_robots_txt=bool(
        CONFIG.get("respect_robots_txt", True)
    ),
    allow_private_network=bool(
        CONFIG.get("allow_private_network_fetch", False)
    ),
    request_interval_seconds=float(
        CONFIG.get(
            "domain_request_interval_seconds",
            1.2,
        )
    ),
)


def _clean(value: Any, limit: int = 8000) -> str:
    return str(value or "").replace("\x00", "").strip()[:limit]


def _conversation_messages(
    value: Any,
) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []

    maximum = int(
        CONFIG.get("maximum_history_messages", 12)
    )
    output: list[dict[str, str]] = []

    for item in value[-maximum:]:
        if not isinstance(item, dict):
            continue

        role = str(item.get("role") or "").strip().lower()
        content = _clean(item.get("content"), 4200)

        if role not in {"user", "assistant"} or not content:
            continue

        output.append(
            {
                "role": role,
                "content": content,
            }
        )

    return output


def _hydrate_attachments(attachments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Re-inspect trusted Core-owned files inside this package for the current turn."""

    output: list[dict[str, Any]] = []
    for item in attachments[:40]:
        if not isinstance(item, dict):
            continue
        merged = dict(item)
        local_path = str(item.get("_internal_path") or "").strip()
        if local_path:
            try:
                inspected = inspect_document(
                    path=Path(local_path),
                    original_name=_clean(item.get("name") or item.get("filename") or Path(local_path).name, 240),
                    media_type=_clean(item.get("media_type") or item.get("content_type"), 120),
                    maximum_chars=int(CONFIG.get("document_maximum_chars", 240000)),
                    maximum_pages=int(CONFIG.get("document_maximum_pages", 250)),
                    maximum_archive_members=int(CONFIG.get("archive_maximum_members", 100)),
                    maximum_archive_bytes=int(CONFIG.get("archive_maximum_uncompressed_bytes", 25000000)),
                )
                merged.update(inspected)
                merged["_internal_path"] = local_path
            except (OSError, ValueError):
                pass
        output.append(merged)
    return output


def _attachment_context(
    attachments: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    remaining = int(CONFIG.get("maximum_attachment_chars", 18000))
    blocks: list[str] = []
    metadata: list[dict[str, Any]] = []

    for item in attachments:
        if not isinstance(item, dict):
            continue
        nested = item.get("model_inspection") if isinstance(item.get("model_inspection"), dict) else {}
        name = _clean(item.get("name") or item.get("filename") or nested.get("name") or "attachment", 240)
        media_type = _clean(item.get("media_type") or item.get("content_type") or nested.get("media_type"), 120)
        status = _clean(item.get("status") or nested.get("status"), 80)
        summary = _clean(item.get("summary") or nested.get("summary"), 700)
        page_count = item.get("page_count") if item.get("page_count") is not None else nested.get("page_count")
        archive_members = item.get("archive_members") if isinstance(item.get("archive_members"), list) else nested.get("archive_members")
        text = _clean(
            item.get("text") or item.get("content") or item.get("extracted_text") or item.get("excerpt")
            or nested.get("text") or nested.get("content") or nested.get("extracted_text") or nested.get("excerpt"),
            min(remaining, 60000),
        )

        safe_meta: dict[str, Any] = {
            "name": name, "media_type": media_type, "status": status, "summary": summary,
            "size_bytes": item.get("size_bytes") or nested.get("size_bytes"),
            "page_count": page_count,
        }
        if isinstance(archive_members, list):
            safe_meta["archive_members"] = [str(value)[:300] for value in archive_members[:100]]
        if item.get("binary_inspection") or nested.get("binary_inspection"):
            safe_meta["binary_inspection"] = True
        derived_images = item.get("derived_images")
        if isinstance(derived_images, list):
            safe_meta["derived_image_count"] = len(derived_images)
        metadata.append(safe_meta)

        if not text:
            continue
        block = (
            f"ATTACHMENT START: {name}\n"
            f"Media type: {media_type or 'unknown'}\n"
            f"Status: {status or 'stored'}\n"
            f"Summary: {summary}\n"
            f"{text}\n"
            f"ATTACHMENT END: {name}"
        )[:remaining]
        if not block:
            break
        blocks.append(block)
        remaining -= len(block)
        if remaining <= 0:
            break

    return "\n\n".join(blocks), metadata

def _model_metadata(result: dict[str, Any]) -> dict[str, Any]:
    meta = result.get("meta")

    if isinstance(meta, dict):
        model = meta.get("model")

        if isinstance(model, dict):
            metadata = model.get("metadata")

            if isinstance(metadata, dict):
                return metadata

    metadata = result.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _depth_limits(depth: str) -> dict[str, int]:
    values = CONFIG.get("search_depth_limits", {})
    selected = (
        values.get(depth)
        if isinstance(values, dict)
        else None
    )

    if not isinstance(selected, dict):
        selected = {
            "queries": 12,
            "fetches": 10,
        }

    return {
        "queries": int(selected.get("queries", 12)),
        "fetches": int(selected.get("fetches", 10)),
    }


def _direct_url_queries(question: str) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []

    for index, url in enumerate(
        dict.fromkeys(_URL_RE.findall(question)),
        start=1,
    ):
        output.append(
            {
                "query": url,
                "purpose": "Inspect the URL supplied by the user",
                "priority": 110 - index,
                "kind": "web",
                "domains": [
                    urlsplit(url).hostname or ""
                ],
            }
        )

    return output


def prepare_request(
    *,
    question: str,
    language: str,
    interaction_mode: str,
    conversation: list[dict[str, str]],
    attachments: list[dict[str, Any]],
    memory_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Prepare local analysis and only request web access when it is needed."""

    begin_request()
    clean_question = _clean(question, 12000)
    hydrated_attachments = _hydrate_attachments(attachments)
    if len(clean_question) < 2:
        if not hydrated_attachments:
            raise ValueError("Agent request is too short.")
        clean_question = (
            "Yüklenen dosyayı ayrıntılı biçimde incele; içeriğini, önemli bulguları, riskleri ve uygulanabilir sonuçları açıkla."
            if str(language).casefold().startswith("tr")
            else "Analyze the uploaded file thoroughly. Explain its contents, important findings, risks, and actionable conclusions."
        )

    snapshot = memory_snapshot if isinstance(memory_snapshot, dict) else {}
    session_key = str(snapshot.get("conversation_id") or snapshot.get("session_id") or "").strip()
    agent_session = STORAGE.load_session(session_key) if session_key else {}
    recent = _conversation_messages(snapshot.get("recent_messages") or conversation)
    if not recent and agent_session:
        last_question = _clean(agent_session.get("last_question"), 3000)
        last_answer = _clean(agent_session.get("last_answer"), 5000)
        if last_question:
            recent.append({"role": "user", "content": last_question})
        if last_answer:
            recent.append({"role": "assistant", "content": last_answer})
    attachment_text, attachment_metadata = _attachment_context(hydrated_attachments)

    visual_analysis: dict[str, Any] = {}
    if bool(CONFIG.get("enable_visual_analysis", True)) and hydrated_attachments:
        try:
            visual_analysis = analyze_images(
                question=clean_question,
                attachments=hydrated_attachments,
                prompt=VISION_PROMPT,
                maximum_tokens=int(CONFIG.get("vision_output_tokens", 900)),
            )
        except LocalAgentError as exc:
            visual_analysis = {"error": str(exc), "search_queries": []}

    default_depth = str(CONFIG.get("search_depth_default", "balanced"))
    maximum_queries = int(CONFIG.get("maximum_query_variations", 18))
    plan = create_plan(
        question=clean_question,
        language=language,
        interaction_mode=interaction_mode,
        conversation=recent,
        visual_analysis=visual_analysis,
        planner_prompt=PLANNER_PROMPT,
        commerce=COMMERCE,
        maximum_queries=maximum_queries,
        default_depth=default_depth,
        output_tokens=int(CONFIG.get("planner_output_tokens", 900)),
        query_maximum_chars=int(CONFIG.get("query_maximum_chars", 180)),
    )

    if attachment_metadata and plan.intent == "local_analysis":
        plan.intent = "document_analysis"

    if plan.needs_current_information:
        direct_queries = _direct_url_queries(clean_question)
        if direct_queries:
            plan.queries = [
                *[
                    SearchQuery(
                        query=item["query"], purpose=item["purpose"], priority=item["priority"],
                        kind=item["kind"], domains=item["domains"],
                    )
                    for item in direct_queries
                ],
                *plan.queries,
            ][:maximum_queries]

    limits = _depth_limits(plan.depth)
    queries = plan.queries[:limits["queries"]] if plan.needs_current_information else []
    started_at = dt.datetime.now(dt.timezone.utc).isoformat()

    metadata = {
        "mode_id": "agent",
        "request_question": clean_question,
        "execution_path": "agent_v1_local_multimodal",
        "web_access": bool(plan.needs_current_information and queries),
        "needs_current_information": bool(plan.needs_current_information),
        "package_managed_search": True,
        "source_policy": "current_web_sources" if plan.needs_current_information else "local_evidence_only",
        "plan": plan.to_dict(),
        "visual_analysis": visual_analysis,
        "attachment_context": attachment_text,
        "attachment_metadata": attachment_metadata,
        "conversation_messages": recent,
        "memory_summary": _clean(snapshot.get("summary"), 5000),
        "memory_facts": snapshot.get("relevant_facts", [])[:24] if isinstance(snapshot.get("relevant_facts"), list) else [],
        "mode_state": snapshot.get("mode_state") if isinstance(snapshot.get("mode_state"), dict) else {},
        "agent_session_state": agent_session,
        "session_key": session_key,
        "fetch_limit": limits["fetches"],
        "started_at": started_at,
        "execution_policy": {
            "network": bool(plan.needs_current_information),
            "allow_purchase": False,
            "allow_login": False,
            "allow_cart_changes": False,
            "allow_payment": False,
            "respect_robots_txt": bool(CONFIG.get("respect_robots_txt", True)),
        },
    }

    return {
        "request_question": clean_question,
        "query_variations": [item.to_dict() for item in queries],
        "metadata": metadata,
    }

def _source_key(source: SourceRecord) -> str:
    parsed = urlsplit(source.url)
    host = (parsed.hostname or "").casefold().rstrip(".")
    path = re.sub(r"/{2,}", "/", parsed.path or "/").rstrip("/") or "/"
    query = "&".join(
        part for part in parsed.query.split("&")
        if part and not part.casefold().startswith(("utm_", "fbclid=", "gclid="))
    )
    return f"{host}{path}?{query}".rstrip("?")


def _assign_source_ids(sources: list[SourceRecord]) -> None:
    for index, source in enumerate(sources, start=1):
        source.source_id = f"S{index}"


def _merge_sources(
    primary: list[SourceRecord],
    fallback_items: list[dict[str, Any]],
) -> list[SourceRecord]:
    output = list(primary)
    seen = {
        _source_key(item)
        for item in output
        if item.url
    }

    for index, item in enumerate(fallback_items, start=1):
        if not isinstance(item, dict):
            continue

        url = _clean(item.get("url"), 3000)

        if not url:
            continue

        source = SourceRecord(
            url=url,
            title=_clean(item.get("title"), 1000),
            snippet=_clean(item.get("snippet"), 3000),
            domain=urlsplit(url).hostname or "",
            source_type=_clean(
                item.get("source_type") or "web",
                100,
            ),
            provider=_clean(item.get("provider") or "agent_fallback", 100),
            query=_clean(item.get("query"), 600),
            rank=int(item.get("rank", index)),
            raw=dict(item),
        )
        key = _source_key(source)

        if key in seen:
            continue

        seen.add(key)
        output.append(source)

    return output


def _deduplicate_sources(sources: list[SourceRecord]) -> list[SourceRecord]:
    """Deduplicate canonical URLs while preserving the strongest provenance fields."""
    output: list[SourceRecord] = []
    by_key: dict[str, SourceRecord] = {}
    for source in sources:
        key = _source_key(source)
        if not key:
            continue
        current = by_key.get(key)
        if current is None:
            by_key[key] = source
            output.append(source)
            continue
        if not current.title and source.title:
            current.title = source.title
        if len(source.snippet) > len(current.snippet):
            current.snippet = source.snippet
        if not current.query and source.query:
            current.query = source.query
        if not current.provider and source.provider:
            current.provider = source.provider
        if not current.published_at and source.published_at:
            current.published_at = source.published_at
    return output


def _normalize_recommendations(value: Any, sources: list[SourceRecord]) -> list[Any]:
    if not isinstance(value, list):
        return []
    valid_ids = {item.source_id for item in sources if item.source_id}
    by_url = {_source_key(item): item.source_id for item in sources if item.url and item.source_id}
    output: list[Any] = []
    for item in value[:24]:
        if not isinstance(item, dict):
            output.append(_clean(item, 1500))
            continue
        normalized = dict(item)
        raw_ids = normalized.get("source_ids")
        ids = [str(candidate) for candidate in raw_ids] if isinstance(raw_ids, list) else []
        ids = list(dict.fromkeys(candidate for candidate in ids if candidate in valid_ids))
        support_url = _clean(normalized.get("url"), 3000)
        if support_url:
            key = _source_key(SourceRecord(url=support_url))
            mapped = by_url.get(key)
            if mapped and mapped not in ids:
                ids.append(mapped)
        normalized["source_ids"] = ids
        output.append(normalized)
    return output


def _build_evidence_payload(
    sources: list[SourceRecord],
    documents: list[Any],
    *,
    maximum_total_chars: int,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    remaining = maximum_total_chars
    document_by_url = {
        item.url: item
        for item in documents
    }

    for source in sources:
        document = document_by_url.get(source.url)
        text = source.snippet

        if document is not None and document.text:
            text = document.text

        clean_text, warnings = sanitize_untrusted_text(
            text,
            maximum_chars=min(remaining, 5500),
        )

        if not clean_text:
            continue

        output.append(
            {
                "source_id": source.source_id,
                "title": (
                    document.title
                    if document is not None and document.title
                    else source.title
                ),
                "url": source.url,
                "domain": source.domain,
                "source_type": source.source_type,
                "provider": source.provider,
                "query": source.query,
                "published_at": source.published_at,
                "fetched_at": (
                    document.fetched_at
                    if document is not None
                    else ""
                ),
                "text": clean_text,
                "security_warnings": warnings,
                "fetch_error": (
                    document.error
                    if document is not None
                    else ""
                ),
            }
        )
        remaining -= len(clean_text)

        if remaining <= 0:
            break

    return output


def _evidence_assessment(
    *,
    sources: list[SourceRecord],
    documents: list[Any],
    evidence: list[dict[str, Any]],
    attachment_context: str,
    network_requested: bool,
    network_allowed: bool,
) -> tuple[str, list[str], list[str]]:
    """Derive quality/limitations from observable evidence rather than model confidence."""

    usable_documents = [
        item for item in documents
        if _clean(getattr(item, "text", ""), 80) and not _clean(getattr(item, "error", ""), 240)
    ]
    injection_warnings = list(
        dict.fromkeys(
            _clean(warning, 500)
            for item in evidence
            if isinstance(item, dict)
            for warning in (item.get("security_warnings") or [])
            if _clean(warning, 500)
        )
    )
    limitations: list[str] = []
    if network_requested and not network_allowed:
        limitations.append("Web access was disabled by Core configuration.")
    elif network_requested and not sources:
        limitations.append("No usable web sources were available for the current-information request.")
    elif network_requested and sources and not usable_documents:
        limitations.append("Search-result metadata was available, but no source page yielded usable fetched text.")
    failed_fetches = sum(1 for item in documents if _clean(getattr(item, "error", ""), 240))
    if failed_fetches:
        limitations.append(f"{failed_fetches} source page(s) could not be fetched or parsed.")
    if injection_warnings:
        limitations.append("One or more evidence pages contained prompt-injection-like text and were treated only as untrusted data.")

    if len(usable_documents) >= 3 and len(evidence) >= 3:
        quality = "high"
    elif usable_documents or len(evidence) >= 2:
        quality = "medium"
    elif sources or attachment_context:
        quality = "limited"
    else:
        quality = "insufficient"
    return quality, list(dict.fromkeys(limitations)), injection_warnings


def _structured_answer(raw: str) -> dict[str, Any]:
    text = raw.strip()

    try:
        value = json.loads(text)

        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")

    if start >= 0 and end > start:
        try:
            value = json.loads(text[start : end + 1])

            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass

    return {
        "answer": raw,
        "recommendations": [],
        "warnings": [],
        "follow_up_options": [],
    }


def _product_payload(
    offers: list[ProductOffer],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []

    for offer in offers[:limit]:
        output.append(
            {
                "product_name": offer.product_name,
                "url": offer.url,
                "domain": offer.domain,
                "seller": offer.seller,
                "brand": offer.brand,
                "model": offer.model,
                "variant": offer.variant,
                "sku": offer.sku,
                "gtin": offer.gtin,
                "price": offer.price,
                "currency": offer.currency,
                "shipping_cost": offer.shipping_cost,
                "total_cost": offer.total_cost,
                "old_price": offer.old_price,
                "discount_percent": offer.discount_percent,
                "availability": offer.availability,
                "rating": offer.rating,
                "review_count": offer.review_count,
                "seller_rating": offer.seller_rating,
                "official_store": offer.official_store,
                "warranty": offer.warranty,
                "delivery": offer.delivery,
                "image_url": offer.image_url,
                "trust_score": offer.trust_score,
                "match_key": offer.match_key,
                "observed_at": offer.observed_at,
                "evidence": [
                    evidence.to_dict()
                    for evidence in offer.evidence[:2]
                ],
            }
        )

    return output


def _fallback_search(
    plan: AgentPlan,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not bool(
        CONFIG.get("fallback_search_enabled", True)
    ):
        return [], []

    return search_plan(
        queries=[
            item.to_dict()
            for item in plan.queries
        ],
        provider_order=[
            str(item)
            for item in CONFIG.get(
                "search_provider_order",
                [
                    "brave",
                    "serper",
                    "bing_api",
                    "duckduckgo_lite",
                    "bing_rss",
                ],
            )
        ],
        timeout_seconds=int(
            CONFIG.get(
                "fallback_search_timeout_seconds",
                16,
            )
        ),
        user_agent=str(
            CONFIG.get(
                "generic_user_agent",
                "CrowAI-Agent/1.0",
            )
        ),
        query_maximum_chars=int(
            CONFIG.get("query_maximum_chars", 180)
        ),
        maximum_queries=int(
            CONFIG.get("fallback_maximum_queries", 8)
        ),
        results_per_query=int(
            CONFIG.get("fallback_results_per_query", 5)
        ),
        workers=int(
            CONFIG.get("fallback_search_workers", 3)
        ),
    )


def finalize_result(
    *,
    question: str,
    language: str,
    interaction_mode: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Enrich optional web output and synthesize the final Agent answer."""

    metadata = _model_metadata(result)
    effective_question = _clean(question, 12000) or _clean(metadata.get("request_question"), 12000)
    plan_value = metadata.get("plan")
    plan = (
        AgentPlan.from_dict(plan_value)
        if isinstance(plan_value, dict)
        else AgentPlan(
            objective=effective_question,
            intent="local_analysis",
            depth="balanced",
            queries=[],
        )
    )

    sources = source_records_from_result(result)

    for index, url in enumerate(
        dict.fromkeys(_URL_RE.findall(effective_question)),
        start=1,
    ):
        if any(
            existing.url.casefold().rstrip("/")
            == url.casefold().rstrip("/")
            for existing in sources
        ):
            continue

        sources.insert(
            0,
            SourceRecord(
                url=url,
                title=urlsplit(url).hostname or url,
                snippet="URL supplied directly by the user.",
                domain=urlsplit(url).hostname or "",
                source_type="user_url",
                provider="user",
                query=url,
                rank=index,
                raw={"direct_user_url": True},
            ),
        )

    search_diagnostics: list[dict[str, Any]] = []
    minimum_sources = int(
        CONFIG.get("minimum_usable_sources", 5)
    )
    network_allowed = not bool(metadata.get("network_disabled_by_core"))

    if (
        plan.needs_current_information
        and network_allowed
        and len(sources) < minimum_sources
    ):
        fallback_hits, search_diagnostics = _fallback_search(plan)
        sources = _merge_sources(
            sources,
            fallback_hits,
        )

    sources = _deduplicate_sources(sources)
    maximum_sources = int(
        CONFIG.get("maximum_sources", 48)
    )
    sources = sources[:maximum_sources]
    _assign_source_ids(sources)
    fetch_limit = int(
        metadata.get(
            "fetch_limit",
            CONFIG.get("maximum_fetch_documents", 16),
        )
    )
    documents = []

    if (
        plan.needs_current_information
        and network_allowed
        and bool(CONFIG.get("enable_page_fetch", True))
        and sources
    ):
        documents = FETCHER.fetch_many(
            [source.url for source in sources],
            maximum_documents=fetch_limit,
            workers=int(
                CONFIG.get("fetch_workers", 4)
            ),
        )

    offers: list[ProductOffer] = []

    if plan.needs_product_normalization:
        offers.extend(
            COMMERCE.offers_from_sources(sources)
        )

        for document in documents:
            offers.extend(
                COMMERCE.offers_from_document(document)
            )

        offers = COMMERCE.deduplicate(offers)
        offers = COMMERCE.rank(
            offers,
            filters=plan.filters,
        )

    product_limit = int(
        CONFIG.get("product_result_limit", 14)
    )
    attachment_context = _clean(
        metadata.get("attachment_context"),
        int(
            CONFIG.get(
                "maximum_attachment_chars",
                18000,
            )
        ),
    )

    # Keep the synthesis request comfortably inside the configured context
    # window even when a large attachment and web evidence are both present.
    input_char_budget = max(24000, int(CONFIG.get("synthesis_input_char_budget", 82000)))
    evidence_budget = max(4000, input_char_budget - len(attachment_context) - 18000)
    evidence = _build_evidence_payload(
        sources,
        documents,
        maximum_total_chars=min(
            int(CONFIG.get("maximum_total_evidence_chars", 26000)),
            evidence_budget,
        ),
    )
    document_by_url = {item.url: item for item in documents}
    for source in sources:
        document = document_by_url.get(source.url)
        if document is not None and getattr(document, "fetched_at", ""):
            source.fetched_at = _clean(document.fetched_at, 100)

    evidence_quality, evidence_limitations, injection_warnings = _evidence_assessment(
        sources=sources,
        documents=documents,
        evidence=evidence,
        attachment_context=attachment_context,
        network_requested=bool(plan.needs_current_information),
        network_allowed=network_allowed,
    )

    payload = {
        "user_question": effective_question,
        "response_language": language,
        "analysis_plan": plan.to_dict(),
        "visual_analysis": metadata.get(
            "visual_analysis",
            {},
        ),
        "attachment_metadata": metadata.get(
            "attachment_metadata",
            [],
        ),
        "attachment_text": attachment_context,
        "sources": evidence,
        "product_offers": _product_payload(
            offers,
            limit=product_limit,
        ),
        "search_diagnostics": search_diagnostics,
        "rules": {
            "cite_source_urls_used": True,
            "cite_stable_source_ids_for_important_claims": True,
            "do_not_invent_prices": True,
            "state_conflicts": True,
            "state_uncertainty": True,
            "state_observation_time": True,
            "do_not_claim_purchase_or_cart_actions": True,
            "distinguish_attachment_facts_from_web_facts": True,
        },
    }

    warnings: list[str] = list(plan.warnings)
    warnings.extend(f"Evidence security warning: {item}" for item in injection_warnings)
    success = False
    answer = ""
    recommendations: list[Any] = []
    follow_up_options: list[Any] = []

    failed_diagnostics = [
        item
        for item in search_diagnostics
        if isinstance(item, dict)
        and not item.get("ok")
    ]

    if (
        not sources
        and not attachment_context
        and not metadata.get("visual_analysis")
    ):
        warnings.append("No usable local attachment evidence or web source was available.")
    else:
        try:
            raw = generate_response(
                [
                    {
                        "role": "system",
                        "content": SYNTHESIZER_PROMPT,
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            payload,
                            ensure_ascii=False,
                        ),
                    },
                ],
                maximum_tokens=int(
                    CONFIG.get(
                        "synthesis_output_tokens",
                        1800,
                    )
                ),
                temperature=0.18,
                json_mode=True,
            )
            structured = _structured_answer(raw)
            answer = _clean(
                structured.get("answer"),
                18000,
            )
            recommendations = _normalize_recommendations(
                structured.get("recommendations"),
                sources,
            )
            model_warnings = structured.get("warnings")

            if isinstance(model_warnings, list):
                warnings.extend(
                    _clean(item, 1000)
                    for item in model_warnings
                    if _clean(item, 1000)
                )

            follow_up_options = (
                structured.get("follow_up_options")
                if isinstance(
                    structured.get("follow_up_options"),
                    list,
                )
                else []
            )
            success = bool(answer)
        except LocalAgentError as exc:
            warnings.append(str(exc))

    if plan.needs_current_information and not network_allowed:
        warnings.append("Web access is disabled by Core configuration for this request.")

    if plan.needs_current_information and network_allowed and failed_diagnostics and len(sources) < minimum_sources:
        providers = ", ".join(
            dict.fromkeys(
                _clean(item.get("provider"), 60)
                for item in failed_diagnostics
                if _clean(item.get("provider"), 60)
            )
        )

        if providers:
            warnings.append(
                f"Some fallback search providers failed: {providers}."
            )

    if not answer:
        answer = (
            "İstek tamamlanamadı. Kullanılabilir yerel veri veya kaynak yetersizdi."
            if str(language).casefold().startswith("tr")
            else "The request could not be completed because usable local evidence or sources were insufficient."
        )

    source_payload = [
        {
            "id": source.source_id,
            "url": source.url,
            "title": source.title or source.domain,
            "snippet": source.snippet,
            "domain": source.domain,
            "source_type": source.source_type,
            "provider": source.provider,
            "published_at": source.published_at,
            "fetched_at": source.fetched_at,
            "query": source.query,
            "rank": source.rank,
        }
        for source in sources
    ]
    product_payload = _product_payload(
        offers,
        limit=product_limit,
    )

    analysis = result.get("analysis")

    if not isinstance(analysis, dict):
        analysis = {}

    analysis.update(
        {
            "overview": answer,
            "conclusion": answer,
            "important_findings": recommendations,
            "evidence": evidence,
            "contradictions": [],
            "artifacts": [],
            "products": product_payload,
            "analysis_plan": plan.to_dict(),
            "search_diagnostics": search_diagnostics,
            "attachment_metadata": metadata.get(
                "attachment_metadata",
                [],
            ),
        }
    )

    result.update(
        {
            "answer": answer,
            "analysis": analysis,
            "sources": source_payload,
            "products": product_payload,
            "recommendations": recommendations,
            "follow_up_options": follow_up_options,
            "mode": {
                "id": "agent",
                "name": "Agent",
            },
            "mode_id": "agent",
            "model_id": "agent/v1.0",
            "model_name": "V1.0",
            "success": success,
            "status": "complete" if success else "partial",
            "warnings": list(
                dict.fromkeys(
                    warning
                    for warning in warnings
                    if str(warning).strip()
                )
            ),
            "metadata": {
                "contract_version": 1,
                "intent": plan.intent,
                "depth": plan.depth,
                "query_count": len(plan.queries),
                "source_count": len(sources),
                "fetched_document_count": len(documents),
                "product_offer_count": len(offers),
                "attachment_count": len(
                    metadata.get("attachment_metadata", [])
                    if isinstance(
                        metadata.get("attachment_metadata"),
                        list,
                    )
                    else []
                ),
                "visual_analysis_used": bool(
                    metadata.get("visual_analysis")
                ),
                "fallback_search_used": bool(
                    search_diagnostics
                ),
                "completed_at": (
                    dt.datetime.now(dt.timezone.utc)
                    .isoformat()
                ),
            },
        }
    )

    result["metadata"]["evidence_quality"] = evidence_quality
    result["metadata"]["limitations"] = evidence_limitations
    result["metadata"]["evidence_security_warning_count"] = len(injection_warnings)
    result["memory_update"] = {
        "mode_state": {
            "last_intent": plan.intent,
            "last_depth": plan.depth,
            "last_product_names": [item.get("product_name") for item in product_payload[:8]],
            "last_source_domains": list(dict.fromkeys(item.get("domain") for item in source_payload if item.get("domain")))[:12],
        }
    }

    session_key = str(
        metadata.get("session_key")
        or ""
    ).strip()

    if session_key:
        STORAGE.save_session(
            session_key,
            {
                "last_question": effective_question,
                "last_answer": answer,
                "plan": plan.to_dict(),
                "products": product_payload,
                "sources": source_payload[:24],
                "attachment_metadata": metadata.get(
                    "attachment_metadata",
                    [],
                ),
                "updated_at": result["metadata"]["completed_at"],
            },
        )

    return result


def cancel_conversation(*, conversation_id: str) -> None:
    """Cancel active Agent inference for a deleted Core conversation."""
    del conversation_id
    _cancel_active()


def delete_conversation(*, conversation_id: str) -> None:
    """Delete package-local follow-up state for a Core conversation."""
    STORAGE.delete_session(conversation_id)


def maintenance() -> dict[str, int]:
    return STORAGE.cleanup()
