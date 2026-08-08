"""Best-effort search fallbacks for Agent V1.0.

API-backed providers are preferred. Public HTML/RSS providers are only
used as fallbacks and are never used to bypass CAPTCHA, login, or access
controls.
"""

from __future__ import annotations

import concurrent.futures
import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit
from xml.etree import ElementTree


@dataclass(slots=True)
class SearchHit:
    title: str
    url: str
    snippet: str = ""
    source_type: str = "web"
    provider: str = ""
    query: str = ""
    rank: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class SearchDiagnostic:
    provider: str
    query: str
    ok: bool
    result_count: int = 0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class _DdgLiteParser(HTMLParser):
    """Parse DuckDuckGo Lite result pages conservatively."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._anchor_href = ""
        self._anchor_class = ""
        self._anchor_text: list[str] = []
        self._in_anchor = False
        self._in_snippet = False
        self._snippet_text: list[str] = []
        self.results: list[dict[str, str]] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = {
            key.casefold(): value or ""
            for key, value in attrs
        }
        name = tag.casefold()

        if name == "a":
            css = attributes.get("class", "")
            href = attributes.get("href", "")

            if (
                "result-link" in css
                or "result__a" in css
                or (
                    href
                    and "duckduckgo.com/l/?" in href
                )
            ):
                self._in_anchor = True
                self._anchor_href = href
                self._anchor_class = css
                self._anchor_text = []

        if name in {"td", "div", "span"}:
            css = attributes.get("class", "")

            if (
                "result-snippet" in css
                or "result__snippet" in css
            ):
                self._in_snippet = True
                self._snippet_text = []

    def handle_endtag(self, tag: str) -> None:
        name = tag.casefold()

        if name == "a" and self._in_anchor:
            title = " ".join(self._anchor_text).strip()
            url = self._decode_redirect(self._anchor_href)

            if title and url:
                self.results.append(
                    {
                        "title": title,
                        "url": url,
                        "snippet": "",
                    }
                )

            self._in_anchor = False
            self._anchor_href = ""
            self._anchor_text = []

        if name in {"td", "div", "span"} and self._in_snippet:
            snippet = " ".join(self._snippet_text).strip()

            if snippet and self.results:
                if not self.results[-1]["snippet"]:
                    self.results[-1]["snippet"] = snippet

            self._in_snippet = False
            self._snippet_text = []

    def handle_data(self, data: str) -> None:
        value = " ".join(data.split())

        if not value:
            return

        if self._in_anchor:
            self._anchor_text.append(value)

        if self._in_snippet:
            self._snippet_text.append(value)

    @staticmethod
    def _decode_redirect(value: str) -> str:
        href = html.unescape(str(value or "").strip())

        if not href:
            return ""

        parsed = urllib.parse.urlsplit(href)
        query = urllib.parse.parse_qs(parsed.query)

        for key in ("uddg", "u"):
            values = query.get(key)

            if values:
                candidate = urllib.parse.unquote(values[0])

                if candidate.startswith(("http://", "https://")):
                    return candidate

        if href.startswith("//"):
            return "https:" + href

        return href if href.startswith(("http://", "https://")) else ""


def _clean_query(value: str, maximum_chars: int) -> str:
    text = " ".join(str(value or "").replace("\x00", " ").split())
    text = re.sub(
        r"^(araştır|arastir|bul|incele|karşılaştır|karsilastir)\s*[:\-]?\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    return text[:maximum_chars].strip()


def _normalize_url(value: str) -> str:
    raw = html.unescape(str(value or "").strip())
    lowered = raw.casefold()

    if not lowered.startswith(("http://", "https://")):
        return ""

    parsed = urlsplit(raw)

    if not parsed.hostname:
        return ""

    return urllib.parse.urlunsplit(
        (
            parsed.scheme.casefold(),
            parsed.netloc,
            parsed.path or "/",
            parsed.query,
            "",
        )
    )


def _request_json(
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any] | None,
    timeout: int,
) -> dict[str, Any]:
    body = None
    method = "GET"

    if payload is not None:
        body = json.dumps(
            payload,
            ensure_ascii=False,
        ).encode("utf-8")
        method = "POST"

    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            **headers,
        },
    )

    with urllib.request.urlopen(
        request,
        timeout=timeout,
    ) as response:
        raw = response.read().decode(
            "utf-8",
            errors="replace",
        )

    value = json.loads(raw)

    if not isinstance(value, dict):
        raise ValueError("Search provider returned non-object JSON.")

    return value


def _request_text(
    url: str,
    *,
    headers: dict[str, str],
    timeout: int,
    maximum_bytes: int = 2_000_000,
) -> str:
    request = urllib.request.Request(
        url,
        headers=headers,
    )

    with urllib.request.urlopen(
        request,
        timeout=timeout,
    ) as response:
        raw = response.read(maximum_bytes + 1)

    if len(raw) > maximum_bytes:
        raise ValueError("Search response exceeded safety limit.")

    charset = (
        response.headers.get_content_charset()
        or "utf-8"
    )

    return raw.decode(
        charset,
        errors="replace",
    )


class SearchClient:
    """Run configured search providers with graceful fallback."""

    def __init__(
        self,
        *,
        provider_order: list[str],
        timeout_seconds: int,
        user_agent: str,
        query_maximum_chars: int,
    ) -> None:
        self.provider_order = [
            str(item).strip()
            for item in provider_order
            if str(item).strip()
        ]
        self.timeout_seconds = max(4, int(timeout_seconds))
        self.user_agent = user_agent
        self.query_maximum_chars = max(
            32,
            int(query_maximum_chars),
        )

    def search(
        self,
        *,
        query: str,
        limit: int,
        domains: list[str] | None = None,
        kind: str = "web",
    ) -> tuple[list[SearchHit], list[SearchDiagnostic]]:
        clean = _clean_query(
            query,
            self.query_maximum_chars,
        )

        if not clean:
            return [], []

        if domains:
            domain_terms = " OR ".join(
                f"site:{domain}"
                for domain in domains[:4]
                if str(domain).strip()
            )

            if domain_terms and "site:" not in clean.casefold():
                clean = (
                    f"({domain_terms}) {clean}"
                )[:self.query_maximum_chars]

        diagnostics: list[SearchDiagnostic] = []

        for provider in self.provider_order:
            method = getattr(
                self,
                f"_search_{provider}",
                None,
            )

            if method is None:
                diagnostics.append(
                    SearchDiagnostic(
                        provider=provider,
                        query=clean,
                        ok=False,
                        error="Unknown provider.",
                    )
                )
                continue

            try:
                hits = method(
                    clean,
                    limit=max(1, limit),
                    kind=kind,
                )
                hits = self._deduplicate(
                    hits,
                    limit=limit,
                )
                diagnostics.append(
                    SearchDiagnostic(
                        provider=provider,
                        query=clean,
                        ok=bool(hits),
                        result_count=len(hits),
                        error="" if hits else "No results.",
                    )
                )

                if hits:
                    return hits, diagnostics

            except (
                urllib.error.HTTPError,
                urllib.error.URLError,
                TimeoutError,
                OSError,
                ValueError,
                json.JSONDecodeError,
                ElementTree.ParseError,
            ) as exc:
                diagnostics.append(
                    SearchDiagnostic(
                        provider=provider,
                        query=clean,
                        ok=False,
                        error=str(exc)[:500],
                    )
                )

        return [], diagnostics

    @staticmethod
    def _deduplicate(
        hits: list[SearchHit],
        *,
        limit: int,
    ) -> list[SearchHit]:
        output: list[SearchHit] = []
        seen: set[str] = set()

        for hit in hits:
            url = _normalize_url(hit.url)

            if not url:
                continue

            key = url.casefold()

            if key in seen:
                continue

            seen.add(key)
            hit.url = url
            hit.rank = len(output) + 1
            output.append(hit)

            if len(output) >= limit:
                break

        return output

    def _search_brave(
        self,
        query: str,
        *,
        limit: int,
        kind: str,
    ) -> list[SearchHit]:
        key = os.environ.get("BRAVE_SEARCH_API_KEY", "").strip()

        if not key:
            raise ValueError("BRAVE_SEARCH_API_KEY is not configured.")

        parameters = urllib.parse.urlencode(
            {
                "q": query,
                "count": min(limit, 20),
                "search_lang": "tr",
                "country": "TR",
                "safesearch": "moderate",
            }
        )
        value = _request_json(
            (
                "https://api.search.brave.com/res/v1/web/search?"
                + parameters
            ),
            headers={
                "X-Subscription-Token": key,
                "User-Agent": self.user_agent,
            },
            payload=None,
            timeout=self.timeout_seconds,
        )
        results = (
            value.get("web", {}).get("results", [])
            if isinstance(value.get("web"), dict)
            else []
        )

        return [
            SearchHit(
                title=str(item.get("title") or "").strip(),
                url=str(item.get("url") or "").strip(),
                snippet=str(
                    item.get("description") or ""
                ).strip(),
                source_type=kind,
                provider="brave",
                query=query,
            )
            for item in results
            if isinstance(item, dict)
        ]

    def _search_serper(
        self,
        query: str,
        *,
        limit: int,
        kind: str,
    ) -> list[SearchHit]:
        key = os.environ.get("SERPER_API_KEY", "").strip()

        if not key:
            raise ValueError("SERPER_API_KEY is not configured.")

        value = _request_json(
            "https://google.serper.dev/search",
            headers={
                "X-API-KEY": key,
                "User-Agent": self.user_agent,
            },
            payload={
                "q": query,
                "gl": "tr",
                "hl": "tr",
                "num": min(limit, 20),
            },
            timeout=self.timeout_seconds,
        )

        return [
            SearchHit(
                title=str(item.get("title") or "").strip(),
                url=str(item.get("link") or "").strip(),
                snippet=str(item.get("snippet") or "").strip(),
                source_type=kind,
                provider="serper",
                query=query,
            )
            for item in value.get("organic", [])
            if isinstance(item, dict)
        ]

    def _search_bing_api(
        self,
        query: str,
        *,
        limit: int,
        kind: str,
    ) -> list[SearchHit]:
        key = os.environ.get("BING_SEARCH_API_KEY", "").strip()

        if not key:
            raise ValueError("BING_SEARCH_API_KEY is not configured.")

        parameters = urllib.parse.urlencode(
            {
                "q": query,
                "count": min(limit, 50),
                "mkt": "tr-TR",
                "safeSearch": "Moderate",
                "textDecorations": "false",
                "textFormat": "Raw",
            }
        )
        value = _request_json(
            (
                "https://api.bing.microsoft.com/v7.0/search?"
                + parameters
            ),
            headers={
                "Ocp-Apim-Subscription-Key": key,
                "User-Agent": self.user_agent,
            },
            payload=None,
            timeout=self.timeout_seconds,
        )
        web_pages = value.get("webPages")
        results = (
            web_pages.get("value", [])
            if isinstance(web_pages, dict)
            else []
        )

        return [
            SearchHit(
                title=str(item.get("name") or "").strip(),
                url=str(item.get("url") or "").strip(),
                snippet=str(item.get("snippet") or "").strip(),
                source_type=kind,
                provider="bing_api",
                query=query,
            )
            for item in results
            if isinstance(item, dict)
        ]

    def _search_duckduckgo_lite(
        self,
        query: str,
        *,
        limit: int,
        kind: str,
    ) -> list[SearchHit]:
        url = (
            "https://lite.duckduckgo.com/lite/?"
            + urllib.parse.urlencode({"q": query})
        )
        raw = _request_text(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.7",
            },
            timeout=self.timeout_seconds,
        )

        lower = raw.casefold()

        if (
            "anomaly-modal" in lower
            or "bots use duckduckgo" in lower
            or "challenge-form" in lower
        ):
            raise ValueError(
                "DuckDuckGo returned an anti-automation page."
            )

        parser = _DdgLiteParser()
        parser.feed(raw)

        return [
            SearchHit(
                title=item["title"],
                url=item["url"],
                snippet=item["snippet"],
                source_type=kind,
                provider="duckduckgo_lite",
                query=query,
            )
            for item in parser.results[:limit]
        ]

    def _search_bing_rss(
        self,
        query: str,
        *,
        limit: int,
        kind: str,
    ) -> list[SearchHit]:
        url = (
            "https://www.bing.com/search?"
            + urllib.parse.urlencode(
                {
                    "q": query,
                    "format": "rss",
                    "setlang": "tr",
                }
            )
        )
        raw = _request_text(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/rss+xml,application/xml,text/xml",
            },
            timeout=self.timeout_seconds,
        )
        root = ElementTree.fromstring(raw)
        hits: list[SearchHit] = []

        for item in root.findall(".//item"):
            title = (
                item.findtext("title")
                or ""
            ).strip()
            link = (
                item.findtext("link")
                or ""
            ).strip()
            description = (
                item.findtext("description")
                or ""
            )
            parser = _VisibleTextParser()
            parser.feed(description)

            if title and link:
                hits.append(
                    SearchHit(
                        title=title,
                        url=link,
                        snippet=parser.text,
                        source_type=kind,
                        provider="bing_rss",
                        query=query,
                    )
                )

            if len(hits) >= limit:
                break

        return hits


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        value = " ".join(data.split())

        if value:
            self.parts.append(value)

    @property
    def text(self) -> str:
        return " ".join(self.parts).strip()


def search_plan(
    *,
    queries: list[dict[str, Any]],
    provider_order: list[str],
    timeout_seconds: int,
    user_agent: str,
    query_maximum_chars: int,
    maximum_queries: int,
    results_per_query: int,
    workers: int = 3,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Execute a small analysis-plan subset concurrently."""

    client = SearchClient(
        provider_order=provider_order,
        timeout_seconds=timeout_seconds,
        user_agent=user_agent,
        query_maximum_chars=query_maximum_chars,
    )
    ordered = sorted(
        (
            item
            for item in queries
            if isinstance(item, dict)
        ),
        key=lambda item: -int(item.get("priority", 50)),
    )
    selected: list[tuple[int, str, list[str], str]] = []
    seen_queries: set[str] = set()

    for item in ordered:
        query = _clean_query(
            str(item.get("query") or ""),
            query_maximum_chars,
        )
        key = query.casefold()

        if not query or key in seen_queries:
            continue

        seen_queries.add(key)
        selected.append(
            (
                len(selected),
                query,
                [
                    str(domain)
                    for domain in item.get("domains", [])
                    if str(domain).strip()
                ],
                str(item.get("kind") or "web"),
            )
        )

        if len(selected) >= maximum_queries:
            break

    if not selected:
        return [], []

    def execute(
        item: tuple[int, str, list[str], str],
    ) -> tuple[int, list[SearchHit], list[SearchDiagnostic]]:
        index, query, domains, kind = item
        hits, details = client.search(
            query=query,
            limit=results_per_query,
            domains=domains,
            kind=kind,
        )
        return index, hits, details

    completed: list[
        tuple[int, list[SearchHit], list[SearchDiagnostic]]
    ] = []

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=max(1, min(int(workers), len(selected)))
    ) as executor:
        futures = [
            executor.submit(execute, item)
            for item in selected
        ]

        for future in concurrent.futures.as_completed(futures):
            try:
                completed.append(future.result())
            except Exception as exc:
                completed.append(
                    (
                        9999,
                        [],
                        [
                            SearchDiagnostic(
                                provider="search_plan",
                                query="",
                                ok=False,
                                error=str(exc)[:500],
                            )
                        ],
                    )
                )

    completed.sort(key=lambda item: item[0])
    all_hits: list[SearchHit] = []
    diagnostics: list[SearchDiagnostic] = []

    for _, hits, details in completed:
        all_hits.extend(hits)
        diagnostics.extend(details)

    deduplicated = SearchClient._deduplicate(
        all_hits,
        limit=maximum_queries * results_per_query,
    )

    return (
        [hit.to_dict() for hit in deduplicated],
        [item.to_dict() for item in diagnostics],
    )
