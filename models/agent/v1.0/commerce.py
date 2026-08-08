"""Marketplace product extraction, normalization and ranking."""

from __future__ import annotations

import datetime as dt
import json
import math
import re
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from .schemas import Evidence, FetchedDocument, ProductOffer, SourceRecord


_MONEY_RE = re.compile(
    r"(?<!\d)(\d{1,3}(?:[.\s]\d{3})*(?:,\d{1,2})?|"
    r"\d+(?:\.\d{1,2})?)\s*(TL|TRY|₺|EUR|€|USD|\$)?",
    flags=re.IGNORECASE,
)

# Search snippets commonly contain model numbers such as RTX 4060,
# iPhone 16, 750W or 128 GB. Snippet prices therefore require an
# explicit currency marker.
_STRICT_MONEY_RE = re.compile(
    r"(?:(?:fiyat|indirimli|sepette|başlayan|baslayan)\s*[:\-]?\s*)?"
    r"(?P<amount>\d{1,3}(?:[.\s]\d{3})+(?:,\d{1,2})?|"
    r"\d+(?:,\d{1,2})?)\s*"
    r"(?P<currency>TL|TRY|₺|EUR|€|USD|\$)",
    flags=re.IGNORECASE,
)


def _fold(value: str) -> str:
    normalized = unicodedata.normalize(
        "NFKD",
        str(value or "")
    )
    ascii_text = "".join(
        char
        for char in normalized
        if not unicodedata.combining(char)
    )
    return re.sub(
        r"[^a-z0-9]+",
        " ",
        ascii_text.casefold(),
    ).strip()


def _float(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None

    raw = str(value or "").strip()

    if not raw:
        return None

    match = _MONEY_RE.search(raw)

    if not match:
        return None

    token = match.group(1).replace(" ", "")

    if "," in token:
        token = token.replace(".", "").replace(",", ".")
    elif token.count(".") > 1:
        token = token.replace(".", "")

    try:
        number = float(token)
    except ValueError:
        return None

    return number if math.isfinite(number) else None


def _integer(value: Any) -> int | None:
    number = _float(value)
    return int(number) if number is not None else None


def _currency(value: Any, default: str = "TRY") -> str:
    raw = str(value or "").upper().strip()
    return {
        "₺": "TRY", "TL": "TRY", "TRY": "TRY",
        "€": "EUR", "EUR": "EUR",
        "$": "USD", "USD": "USD",
    }.get(raw, default)


def _iter_json_objects(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            if isinstance(child, (dict, list)):
                yield from _iter_json_objects(child)

    elif isinstance(value, list):
        for item in value:
            yield from _iter_json_objects(item)


def _type_matches(value: Any, expected: str) -> bool:
    if isinstance(value, str):
        return value.casefold() == expected.casefold()

    if isinstance(value, list):
        return any(
            str(item).casefold() == expected.casefold()
            for item in value
        )

    return False


def _brand(value: Any) -> str:
    if isinstance(value, dict):
        return str(
            value.get("name")
            or value.get("@id")
            or ""
        ).strip()

    return str(value or "").strip()


def _offer_candidates(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        return [value]

    if isinstance(value, list):
        return [
            item
            for item in value
            if isinstance(item, dict)
        ]

    return []


class CommerceNormalizer:
    def __init__(self, sites_path: Path) -> None:
        self.sites_path = sites_path.resolve()
        self.sites = self._load_sites()
        self.site_by_domain = {
            item["domain"]: item
            for item in self.sites
        }

    def _load_sites(self) -> list[dict[str, Any]]:
        try:
            value = json.loads(
                self.sites_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            return []

        sites = value.get("sites")

        return [
            item
            for item in sites
            if isinstance(item, dict)
            and item.get("enabled", True)
        ] if isinstance(sites, list) else []

    def site_info(self, domain: str) -> dict[str, Any]:
        folded = domain.casefold().removeprefix("www.")

        for configured, item in self.site_by_domain.items():
            if (
                folded == configured
                or folded.endswith("." + configured)
            ):
                return item

        return {
            "domain": folded,
            "name": folded,
            "type": "unknown",
            "trust_base": 0.55,
        }

    def marketplace_queries(
        self,
        query: str,
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []

        for index, site in enumerate(self.sites):
            template = str(
                site.get("search_query_template")
                or "site:{domain} {query}"
            )
            output.append(
                {
                    "query": template.format(
                        domain=site["domain"],
                        query=query,
                    ),
                    "purpose": (
                        f"Search {site.get('name', site['domain'])} "
                        "for matching offers"
                    ),
                    "priority": max(20, 90 - index),
                    "kind": (
                        "price_comparison"
                        if site.get("type") == "price_comparison"
                        else "marketplace"
                    ),
                    "domains": [site["domain"]],
                }
            )

            if len(output) >= limit:
                break

        return output

    def offers_from_document(
        self,
        document: FetchedDocument,
    ) -> list[ProductOffer]:
        base_url = document.final_url or document.url
        domain = (
            urlsplit(base_url).hostname
            or ""
        ).casefold()
        site = self.site_info(domain)
        observed_at = document.fetched_at or (
            dt.datetime.now(dt.timezone.utc).isoformat()
        )
        offers: list[ProductOffer] = []

        for root in document.json_ld:
            for item in _iter_json_objects(root):
                if not _type_matches(
                    item.get("@type"),
                    "Product",
                ):
                    continue

                product_name = str(
                    item.get("name")
                    or document.title
                    or ""
                ).strip()

                if not product_name:
                    continue

                brand = _brand(item.get("brand"))
                model = str(
                    item.get("model")
                    or item.get("mpn")
                    or ""
                ).strip()
                sku = str(item.get("sku") or "").strip()
                gtin = str(
                    item.get("gtin13")
                    or item.get("gtin14")
                    or item.get("gtin")
                    or ""
                ).strip()
                image = item.get("image")

                if isinstance(image, list):
                    image_url = str(image[0] if image else "")
                elif isinstance(image, dict):
                    image_url = str(
                        image.get("url")
                        or image.get("contentUrl")
                        or ""
                    )
                else:
                    image_url = str(image or "")
                if image_url:
                    image_url = urljoin(base_url, image_url)

                aggregate_rating = item.get("aggregateRating")
                rating = None
                review_count = None

                if isinstance(aggregate_rating, dict):
                    rating = _float(
                        aggregate_rating.get("ratingValue")
                    )
                    review_count = _integer(
                        aggregate_rating.get("reviewCount")
                        or aggregate_rating.get("ratingCount")
                    )

                for offer_data in _offer_candidates(
                    item.get("offers")
                ):
                    price = _float(
                        offer_data.get("price")
                        or offer_data.get("lowPrice")
                    )
                    currency = _currency(
                        offer_data.get("priceCurrency")
                    )
                    availability = str(
                        offer_data.get("availability")
                        or ""
                    ).rsplit("/", 1)[-1]
                    seller_data = offer_data.get("seller")
                    seller = ""

                    if isinstance(seller_data, dict):
                        seller = str(
                            seller_data.get("name")
                            or ""
                        ).strip()
                    else:
                        seller = str(
                            seller_data or ""
                        ).strip()

                    url = str(
                        offer_data.get("url")
                        or item.get("url")
                        or document.canonical_url
                        or document.final_url
                        or document.url
                    ).strip()
                    url = urljoin(base_url, url) if url else base_url

                    offer = ProductOffer(
                        product_name=product_name,
                        url=url,
                        domain=domain,
                        seller=seller,
                        brand=brand,
                        model=model,
                        sku=sku,
                        gtin=gtin,
                        price=price,
                        currency=currency,
                        availability=availability,
                        rating=rating,
                        review_count=review_count,
                        image_url=image_url,
                        trust_score=float(
                            site.get("trust_base", 0.55)
                        ),
                        observed_at=observed_at,
                    )
                    offer.match_key = self.match_key(offer)
                    offer.ensure_total()
                    offers.append(offer)

        if offers:
            return offers

        meta = document.meta
        price = _float(
            meta.get("product:price:amount")
            or meta.get("og:price:amount")
        )

        if price is None:
            price = self._price_from_text(
                document.text[:4000]
            )

        if price is not None and document.title:
            offer = ProductOffer(
                product_name=document.title,
                url=(
                    document.canonical_url
                    or document.final_url
                    or document.url
                ),
                domain=domain,
                price=price,
                currency=_currency(
                    meta.get("product:price:currency")
                    or meta.get("og:price:currency")
                ),
                image_url=urljoin(
                    base_url,
                    meta.get("og:image")
                    or (
                        document.images[0]
                        if document.images
                        else ""
                    ),
                ),
                trust_score=float(
                    site.get("trust_base", 0.55)
                ),
                observed_at=observed_at,
            )
            offer.match_key = self.match_key(offer)
            offer.ensure_total()
            offers.append(offer)

        return offers

    def offers_from_sources(
        self,
        sources: list[SourceRecord],
    ) -> list[ProductOffer]:
        offers: list[ProductOffer] = []
        now = dt.datetime.now(dt.timezone.utc).isoformat()

        for source in sources:
            price = self._price_from_text(
                f"{source.title}\n{source.snippet}"
            )

            if price is None:
                continue

            domain = source.domain.casefold()
            site = self.site_info(domain)
            offer = ProductOffer(
                product_name=source.title or source.snippet[:120],
                url=source.url,
                domain=domain,
                price=price,
                currency="TRY",
                trust_score=float(
                    site.get("trust_base", 0.55)
                ) - 0.08,
                observed_at=now,
                evidence=[
                    Evidence(
                        source_url=source.url,
                        source_title=source.title,
                        claim="Price extracted from search snippet",
                        value=price,
                        field="price",
                        confidence=0.45,
                        observed_at=now,
                        excerpt=source.snippet[:500],
                    )
                ],
            )
            offer.match_key = self.match_key(offer)
            offer.ensure_total()
            offers.append(offer)

        return offers

    @staticmethod
    def _price_from_text(text: str) -> float | None:
        candidates: list[float] = []

        for match in _STRICT_MONEY_RE.finditer(str(text or "")):
            token = match.group("amount").replace(" ", "")

            if re.fullmatch(
                r"\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?",
                token,
            ):
                token = token.replace(".", "").replace(",", ".")
            else:
                token = token.replace(",", ".")

            try:
                value = float(token)
            except ValueError:
                continue

            if 1.0 <= value <= 100_000_000:
                candidates.append(value)

        return candidates[0] if candidates else None

    @staticmethod
    def match_key(offer: ProductOffer) -> str:
        strong = offer.gtin or offer.sku

        if strong:
            return _fold(strong)

        tokens = _fold(
            " ".join(
                [
                    offer.brand,
                    offer.model,
                    offer.product_name,
                    offer.variant,
                ]
            )
        ).split()

        filtered = [
            token
            for token in tokens
            if token not in {
                "indirim", "kampanya", "ucretsiz",
                "kargo", "resmi", "magaza", "yeni",
            }
        ]
        return " ".join(filtered[:18])

    def deduplicate(
        self,
        offers: list[ProductOffer],
    ) -> list[ProductOffer]:
        best: dict[tuple[str, str, str], ProductOffer] = {}

        for offer in offers:
            key = (
                offer.domain,
                offer.seller.casefold(),
                offer.match_key or self.match_key(offer),
            )
            current = best.get(key)

            if current is None:
                best[key] = offer
                continue

            current_score = (
                (1 if current.price is not None else 0)
                + len(current.evidence)
                + current.trust_score
            )
            new_score = (
                (1 if offer.price is not None else 0)
                + len(offer.evidence)
                + offer.trust_score
            )

            if new_score > current_score:
                best[key] = offer

        return list(best.values())

    def rank(
        self,
        offers: list[ProductOffer],
        *,
        filters: dict[str, Any],
    ) -> list[ProductOffer]:
        maximum_price = _float(
            filters.get("maximum_price")
            or filters.get("max_price")
            or filters.get("budget")
        )
        minimum_rating = _float(
            filters.get("minimum_rating")
            or filters.get("min_rating")
        )
        official_only = bool(
            filters.get("official_store_only")
        )
        available_only = bool(
            filters.get("available_only", True)
        )

        filtered: list[ProductOffer] = []

        for offer in offers:
            if (
                maximum_price is not None
                and offer.total_cost is not None
                and offer.total_cost > maximum_price
            ):
                continue

            if (
                minimum_rating is not None
                and offer.rating is not None
                and offer.rating < minimum_rating
            ):
                continue

            if official_only and offer.official_store is not True:
                continue

            if available_only and offer.availability.casefold() in {
                "outofstock",
                "soldout",
                "discontinued",
            }:
                continue

            filtered.append(offer)

        prices = [
            offer.total_cost
            for offer in filtered
            if offer.total_cost is not None
        ]
        minimum = min(prices) if prices else None
        maximum = max(prices) if prices else None

        def score(offer: ProductOffer) -> float:
            value = offer.trust_score * 45

            if offer.rating is not None:
                value += min(offer.rating / 5.0, 1.0) * 20

            if offer.review_count:
                value += min(
                    math.log10(max(1, offer.review_count)) / 5.0,
                    1.0,
                ) * 10

            if offer.official_store:
                value += 8

            if (
                offer.total_cost is not None
                and minimum is not None
                and maximum is not None
            ):
                spread = maximum - minimum
                price_quality = (
                    1.0
                    if spread <= 0
                    else 1.0 - (
                        (offer.total_cost - minimum) / spread
                    )
                )
                value += price_quality * 17

            return value

        return sorted(
            filtered,
            key=lambda item: (
                -score(item),
                item.total_cost
                if item.total_cost is not None
                else float("inf"),
            ),
        )
