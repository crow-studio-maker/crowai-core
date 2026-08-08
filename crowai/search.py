from __future__ import annotations

import html
import os
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any


class _Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.active = False
        self.href = ""
        self.parts: list[str] = []
        self.results: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs):
        data = dict(attrs)
        if tag == "a" and ("result-link" in data.get("class", "") or "result__a" in data.get("class", "")):
            self.active = True
            self.href = data.get("href", "")
            self.parts = []

    def handle_data(self, data: str):
        if self.active:
            self.parts.append(data)

    def handle_endtag(self, tag: str):
        if tag != "a" or not self.active:
            return
        title = " ".join(" ".join(self.parts).split())
        href = html.unescape(self.href)
        parsed = urllib.parse.urlsplit(href)
        values = urllib.parse.parse_qs(parsed.query).get("uddg")
        if values:
            href = urllib.parse.unquote(values[0])
        if title and href.startswith(("http://", "https://")):
            self.results.append({"title": title, "url": href, "snippet": "", "domain": urllib.parse.urlsplit(href).hostname or ""})
        self.active = False


def search(queries: list[dict[str, Any]], maximum: int = 12) -> list[dict[str, Any]]:
    endpoint = os.getenv("CROWAI_SEARCH_URL", "").strip()
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in queries[:6]:
        query = " ".join(str(item.get("query") or "").split())[:240]
        if not query:
            continue
        try:
            if endpoint:
                url = endpoint.rstrip("/") + "/search?" + urllib.parse.urlencode({"q": query, "format": "json"})
                req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "CrowAI/1.0"})
                import json
                with urllib.request.urlopen(req, timeout=10) as response:
                    value = json.loads(response.read(2_000_000).decode("utf-8", "replace"))
                candidates = value.get("results", []) if isinstance(value, dict) else []
                rows = [{"title": str(x.get("title") or ""), "url": str(x.get("url") or ""), "snippet": str(x.get("content") or ""), "domain": urllib.parse.urlsplit(str(x.get("url") or "")).hostname or ""} for x in candidates if isinstance(x, dict)]
            else:
                url = "https://lite.duckduckgo.com/lite/?" + urllib.parse.urlencode({"q": query})
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.8"})
                with urllib.request.urlopen(req, timeout=10) as response:
                    raw = response.read(1_500_000).decode("utf-8", "replace")
                parser = _Parser(); parser.feed(raw); rows = parser.results
        except Exception:
            rows = []
        for row in rows:
            key = row.get("url", "").casefold().rstrip("/")
            if not key or key in seen:
                continue
            seen.add(key)
            row["query"] = query
            output.append(row)
            if len(output) >= maximum:
                return output
    return output
