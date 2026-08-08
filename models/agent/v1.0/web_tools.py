"""HTTP fetching, HTML extraction and source normalization."""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import html
import json
import urllib.error
import urllib.request
import urllib.robotparser
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit

from .schemas import FetchedDocument, SourceRecord
from .security import (
    DomainRateLimiter,
    UnsafeUrlError,
    normalize_http_url,
    sanitize_untrusted_text,
)
from .storage import AgentStorage


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Validate every redirect target against the local-network policy."""

    def __init__(self, allow_private_network: bool) -> None:
        super().__init__()
        self.allow_private_network = allow_private_network

    def redirect_request(
        self,
        req,
        fp,
        code,
        msg,
        headers,
        newurl,
    ):
        safe_url = normalize_http_url(
            newurl,
            allow_private_network=self.allow_private_network,
        )
        return super().redirect_request(
            req,
            fp,
            code,
            msg,
            headers,
            safe_url,
        )


class _PageParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.title = ""
        self._in_title = False
        self._skip_depth = 0
        self._text_parts: list[str] = []
        self.meta: dict[str, str] = {}
        self.images: list[str] = []
        self.canonical_url = ""
        self.json_ld: list[Any] = []
        self._in_json_ld = False
        self._json_buffer: list[str] = []

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

        if name in {"script", "style", "noscript", "svg"}:
            self._skip_depth += 1

        if name == "title":
            self._in_title = True

        if name == "script":
            content_type = attributes.get("type", "").casefold()
            if "ld+json" in content_type:
                self._in_json_ld = True
                self._json_buffer = []

        if name == "meta":
            key = (
                attributes.get("property")
                or attributes.get("name")
                or attributes.get("itemprop")
            ).strip()
            content = attributes.get("content", "").strip()

            if key and content:
                self.meta[key] = content

        if name == "link":
            relation = attributes.get("rel", "").casefold()
            href = attributes.get("href", "").strip()

            if href and "canonical" in relation:
                self.canonical_url = urljoin(
                    self.base_url,
                    href,
                )

        if name == "img":
            source = (
                attributes.get("src")
                or attributes.get("data-src")
                or attributes.get("data-original")
                or ""
            ).strip()

            if source:
                self.images.append(
                    urljoin(self.base_url, source)
                )

    def handle_endtag(self, tag: str) -> None:
        name = tag.casefold()

        if name == "title":
            self._in_title = False

        if name == "script":
            if self._in_json_ld:
                raw = "".join(self._json_buffer).strip()

                if raw:
                    try:
                        self.json_ld.append(json.loads(raw))
                    except json.JSONDecodeError:
                        pass

                self._in_json_ld = False
                self._json_buffer = []

            if self._skip_depth > 0:
                self._skip_depth -= 1

        elif name in {"style", "noscript", "svg"}:
            if self._skip_depth > 0:
                self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._in_json_ld:
            self._json_buffer.append(data)

        cleaned = " ".join(data.split())

        if not cleaned:
            return

        if self._in_title:
            self.title = (
                f"{self.title} {cleaned}".strip()
            )

        if self._skip_depth <= 0:
            self._text_parts.append(cleaned)

    @property
    def text(self) -> str:
        return "\n".join(self._text_parts)


def source_records_from_result(
    result: dict[str, Any],
) -> list[SourceRecord]:
    candidates: list[Any] = []

    for key in (
        "sources",
        "results",
        "search_results",
        "evidence",
        "documents",
    ):
        value = result.get(key)

        if isinstance(value, list):
            candidates.extend(value)

    analysis = result.get("analysis")

    if isinstance(analysis, dict):
        for key in ("sources", "evidence", "results"):
            value = analysis.get(key)

            if isinstance(value, list):
                candidates.extend(value)

    output: list[SourceRecord] = []
    seen: set[str] = set()

    for index, item in enumerate(candidates, start=1):
        if not isinstance(item, dict):
            continue

        url = str(
            item.get("url")
            or item.get("link")
            or item.get("source_url")
            or ""
        ).strip()

        if not url or url in seen:
            continue

        seen.add(url)
        domain = urlsplit(url).hostname or ""

        output.append(
            SourceRecord(
                url=url,
                title=str(
                    item.get("title")
                    or item.get("name")
                    or item.get("source_title")
                    or ""
                ).strip(),
                snippet=str(
                    item.get("snippet")
                    or item.get("description")
                    or item.get("text")
                    or item.get("excerpt")
                    or ""
                ).strip(),
                domain=domain,
                source_type=str(
                    item.get("type")
                    or item.get("source_type")
                    or "web"
                ).strip(),
                provider=str(item.get("provider") or item.get("source_provider") or "core").strip(),
                published_at=str(
                    item.get("published_at")
                    or item.get("date")
                    or ""
                ).strip(),
                query=str(
                    item.get("query")
                    or ""
                ).strip(),
                rank=int(item.get("rank", index)),
                raw=dict(item),
            )
        )

    return output


class HttpFetcher:
    def __init__(
        self,
        *,
        storage: AgentStorage,
        user_agent: str,
        timeout_seconds: int,
        maximum_bytes: int,
        maximum_source_chars: int,
        cache_ttl_seconds: int,
        respect_robots_txt: bool,
        allow_private_network: bool,
        request_interval_seconds: float,
    ) -> None:
        self.storage = storage
        self.user_agent = user_agent
        self.timeout_seconds = timeout_seconds
        self.maximum_bytes = maximum_bytes
        self.maximum_source_chars = maximum_source_chars
        self.cache_ttl_seconds = cache_ttl_seconds
        self.respect_robots_txt = respect_robots_txt
        self.allow_private_network = allow_private_network
        self.rate_limiter = DomainRateLimiter(
            request_interval_seconds
        )
        self._robots: dict[str, urllib.robotparser.RobotFileParser] = {}

    def _robots_allowed(self, url: str) -> bool:
        if not self.respect_robots_txt:
            return True

        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        parser = self._robots.get(origin)

        if parser is None:
            robots_url = f"{origin}/robots.txt"
            parser = urllib.robotparser.RobotFileParser()
            parser.set_url(robots_url)

            try:
                request = urllib.request.Request(
                    robots_url,
                    headers={
                        "User-Agent": self.user_agent,
                        "Accept": "text/plain,*/*;q=0.2",
                    },
                )
                opener = urllib.request.build_opener(
                    _SafeRedirectHandler(
                        self.allow_private_network
                    )
                )

                with opener.open(
                    request,
                    timeout=min(6, self.timeout_seconds),
                ) as response:
                    normalize_http_url(
                        response.geturl(),
                        allow_private_network=self.allow_private_network,
                    )
                    raw = response.read(512_001)

                if len(raw) > 512_000:
                    return True

                parser.parse(
                    raw.decode(
                        "utf-8",
                        errors="replace",
                    ).splitlines()
                )
            except (
                urllib.error.URLError,
                urllib.error.HTTPError,
                TimeoutError,
                OSError,
                UnsafeUrlError,
            ):
                return True

            self._robots[origin] = parser

        return parser.can_fetch(self.user_agent, url)

    def fetch(self, url: str) -> FetchedDocument:
        try:
            safe_url = normalize_http_url(
                url,
                allow_private_network=self.allow_private_network,
            )
        except UnsafeUrlError as exc:
            return FetchedDocument(
                url=url,
                final_url=url,
                status_code=0,
                content_type="",
                title="",
                text="",
                error=str(exc),
            )

        cached = self.storage.get_page(safe_url)

        if cached:
            try:
                return FetchedDocument(**cached)
            except TypeError:
                pass

        if not self._robots_allowed(safe_url):
            return FetchedDocument(
                url=safe_url,
                final_url=safe_url,
                status_code=0,
                content_type="",
                title="",
                text="",
                error="Blocked by robots.txt policy.",
            )

        domain = urlsplit(safe_url).hostname or ""
        self.rate_limiter.wait(domain)

        request = urllib.request.Request(
            safe_url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": (
                    "text/html,application/xhtml+xml,"
                    "application/json;q=0.9,*/*;q=0.5"
                ),
                "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.7",
            },
        )

        try:
            opener = urllib.request.build_opener(
                _SafeRedirectHandler(
                    self.allow_private_network
                )
            )

            with opener.open(
                request,
                timeout=self.timeout_seconds,
            ) as response:
                status = int(
                    getattr(response, "status", 200)
                )
                final_url = normalize_http_url(
                    response.geturl(),
                    allow_private_network=self.allow_private_network,
                )
                content_type = response.headers.get_content_type()
                charset = (
                    response.headers.get_content_charset()
                    or "utf-8"
                )
                raw = response.read(
                    self.maximum_bytes + 1
                )
        except urllib.error.HTTPError as exc:
            return FetchedDocument(
                url=safe_url,
                final_url=safe_url,
                status_code=exc.code,
                content_type="",
                title="",
                text="",
                error=f"HTTP {exc.code}",
            )
        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
            UnsafeUrlError,
        ) as exc:
            return FetchedDocument(
                url=safe_url,
                final_url=safe_url,
                status_code=0,
                content_type="",
                title="",
                text="",
                error=str(exc),
            )

        if len(raw) > self.maximum_bytes:
            return FetchedDocument(
                url=safe_url,
                final_url=final_url,
                status_code=status,
                content_type=content_type,
                title="",
                text="",
                error="Page exceeded the maximum download size.",
            )

        decoded = raw.decode(
            charset,
            errors="replace",
        )

        fetched_at = (
            dt.datetime.now(dt.timezone.utc)
            .isoformat()
        )

        if content_type == "application/json":
            try:
                parsed_json = json.loads(decoded)
                text = json.dumps(
                    parsed_json,
                    ensure_ascii=False,
                    indent=2,
                )
            except json.JSONDecodeError:
                text = decoded

            clean_text, _ = sanitize_untrusted_text(
                text,
                maximum_chars=self.maximum_source_chars,
            )

            document = FetchedDocument(
                url=safe_url,
                final_url=final_url,
                status_code=status,
                content_type=content_type,
                title="",
                text=clean_text,
                fetched_at=fetched_at,
            )
        else:
            parser = _PageParser(final_url)

            try:
                parser.feed(decoded)
            except Exception:
                pass

            title = html.unescape(
                parser.title
                or parser.meta.get("og:title", "")
                or parser.meta.get("twitter:title", "")
            ).strip()
            description = html.unescape(
                parser.meta.get("description", "")
                or parser.meta.get("og:description", "")
            ).strip()
            clean_text, _ = sanitize_untrusted_text(
                parser.text,
                maximum_chars=self.maximum_source_chars,
            )

            document = FetchedDocument(
                url=safe_url,
                final_url=final_url,
                status_code=status,
                content_type=content_type,
                title=title,
                text=clean_text,
                canonical_url=parser.canonical_url,
                description=description,
                images=list(dict.fromkeys(parser.images))[:20],
                json_ld=parser.json_ld,
                meta=parser.meta,
                fetched_at=fetched_at,
            )

        self.storage.put_page(
            safe_url,
            document.to_dict(),
            ttl_seconds=self.cache_ttl_seconds,
        )

        return document

    def fetch_many(
        self,
        urls: list[str],
        *,
        maximum_documents: int,
        workers: int,
    ) -> list[FetchedDocument]:
        selected = list(dict.fromkeys(urls))[:maximum_documents]

        if not selected:
            return []

        output: list[FetchedDocument] = []

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, workers)
        ) as executor:
            futures = {
                executor.submit(self.fetch, url): url
                for url in selected
            }

            for future in concurrent.futures.as_completed(futures):
                try:
                    output.append(future.result())
                except Exception as exc:
                    url = futures[future]
                    output.append(
                        FetchedDocument(
                            url=url,
                            final_url=url,
                            status_code=0,
                            content_type="",
                            title="",
                            text="",
                            error=str(exc),
                        )
                    )

        order = {
            url: index
            for index, url in enumerate(selected)
        }

        output.sort(
            key=lambda item: order.get(item.url, 9999)
        )
        return output
