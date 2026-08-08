"""Shared schemas for CrowAI Agent V1.0."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class SearchQuery:
    query: str
    purpose: str
    priority: int = 50
    kind: str = "web"
    domains: list[str] = field(default_factory=list)
    recency_days: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class AgentPlan:
    objective: str
    intent: str
    depth: str
    queries: list[SearchQuery]
    filters: dict[str, Any] = field(default_factory=dict)
    comparison_criteria: list[str] = field(default_factory=list)
    needs_visual_analysis: bool = False
    needs_product_normalization: bool = False
    needs_current_information: bool = False
    expected_output: str = "answer"
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["queries"] = [item.to_dict() for item in self.queries]
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AgentPlan":
        queries: list[SearchQuery] = []

        for item in value.get("queries", []):
            if not isinstance(item, dict):
                continue

            query = str(item.get("query") or "").strip()

            if not query:
                continue

            queries.append(
                SearchQuery(
                    query=query,
                    purpose=str(
                        item.get("purpose") or "analysis"
                    ).strip(),
                    priority=int(item.get("priority", 50)),
                    kind=str(item.get("kind") or "web").strip(),
                    domains=[
                        str(domain).strip()
                        for domain in item.get("domains", [])
                        if str(domain).strip()
                    ],
                    recency_days=(
                        int(item["recency_days"])
                        if item.get("recency_days") is not None
                        else None
                    ),
                )
            )

        return cls(
            objective=str(value.get("objective") or "").strip(),
            intent=str(value.get("intent") or "local_analysis").strip(),
            depth=str(value.get("depth") or "balanced").strip(),
            queries=queries,
            filters=(
                value.get("filters")
                if isinstance(value.get("filters"), dict)
                else {}
            ),
            comparison_criteria=[
                str(item).strip()
                for item in value.get("comparison_criteria", [])
                if str(item).strip()
            ],
            needs_visual_analysis=bool(
                value.get("needs_visual_analysis")
            ),
            needs_product_normalization=bool(
                value.get("needs_product_normalization")
            ),
            needs_current_information=bool(
                value.get("needs_current_information", False)
            ),
            expected_output=str(
                value.get("expected_output") or "answer"
            ).strip(),
            warnings=[
                str(item).strip()
                for item in value.get("warnings", [])
                if str(item).strip()
            ],
        )


@dataclass(slots=True)
class SourceRecord:
    url: str
    source_id: str = ""
    title: str = ""
    snippet: str = ""
    domain: str = ""
    source_type: str = "web"
    provider: str = ""
    published_at: str = ""
    fetched_at: str = ""
    query: str = ""
    rank: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class FetchedDocument:
    url: str
    final_url: str
    status_code: int
    content_type: str
    title: str
    text: str
    canonical_url: str = ""
    description: str = ""
    images: list[str] = field(default_factory=list)
    json_ld: list[Any] = field(default_factory=list)
    meta: dict[str, str] = field(default_factory=dict)
    fetched_at: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Evidence:
    source_url: str
    source_title: str
    claim: str
    value: Any = None
    field: str = ""
    confidence: float = 0.5
    observed_at: str = ""
    excerpt: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ProductOffer:
    product_name: str
    url: str
    domain: str
    seller: str = ""
    brand: str = ""
    model: str = ""
    variant: str = ""
    sku: str = ""
    gtin: str = ""
    price: float | None = None
    currency: str = "TRY"
    shipping_cost: float | None = None
    total_cost: float | None = None
    old_price: float | None = None
    discount_percent: float | None = None
    availability: str = ""
    rating: float | None = None
    review_count: int | None = None
    seller_rating: float | None = None
    official_store: bool | None = None
    warranty: str = ""
    delivery: str = ""
    image_url: str = ""
    trust_score: float = 0.5
    match_key: str = ""
    observed_at: str = ""
    evidence: list[Evidence] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def ensure_total(self) -> None:
        if self.price is None:
            return
        self.total_cost = round(
            self.price + (self.shipping_cost or 0.0),
            2,
        )

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["evidence"] = [item.to_dict() for item in self.evidence]
        return value
