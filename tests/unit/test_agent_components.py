from __future__ import annotations

import bz2
import gzip
import importlib
import io
import json
import lzma
import tarfile
import zipfile
from pathlib import Path

from models.registry import ModelRegistry


ROOT = Path(__file__).resolve().parents[2]


def _agent_module(name: str):
    registry = ModelRegistry(ROOT / "models", development=True, strict_capabilities=True)
    package = registry._load("agent/v1.0")
    return importlib.import_module(f"{package.__name__}.{name}")


def test_agent_storage_initialization_is_lazy(tmp_path: Path) -> None:
    storage_mod = _agent_module("storage")
    database_path = tmp_path / "lazy-cache.sqlite3"
    storage = storage_mod.AgentStorage(database_path)

    assert not database_path.exists()
    assert storage.load_session("") == {}
    assert not database_path.exists()

    assert storage.stats() == {"page_rows": 0, "product_rows": 0, "session_rows": 0}
    assert database_path.exists()


def test_agent_storage_cache_session_and_cleanup(tmp_path: Path) -> None:
    storage_mod = _agent_module("storage")
    storage = storage_mod.AgentStorage(tmp_path / "cache.sqlite3")

    assert storage.key_for("same") == storage.key_for("same")
    assert storage.get_page("https://example.test/a") is None
    storage.put_page(
        "https://example.test/a",
        {"title": "Cached", "items": [1, 2]},
        ttl_seconds=60,
    )
    assert storage.get_page("https://example.test/a") == {
        "title": "Cached",
        "items": [1, 2],
    }

    assert storage.load_session("") == {}
    assert storage.load_session("conversation-1") == {}
    storage.save_session("", {"ignored": True})
    storage.save_session("conversation-1", {"turn": 3})
    assert storage.load_session("conversation-1") == {"turn": 3}

    with storage._lock, storage._connect() as connection:
        connection.execute(
            "UPDATE page_cache SET expires_at = 0 WHERE url = ?",
            ("https://example.test/a",),
        )
        connection.execute(
            "INSERT OR REPLACE INTO product_cache(cache_key, payload_json, created_at, expires_at) VALUES(?, ?, ?, ?)",
            ("expired", "{}", 0.0, 0.0),
        )
    storage.cleanup()
    assert storage.get_page("https://example.test/a") is None
    with storage._lock, storage._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM product_cache").fetchone()[0] == 0


def test_agent_storage_invalid_json_falls_back_safely(tmp_path: Path) -> None:
    storage_mod = _agent_module("storage")
    storage = storage_mod.AgentStorage(tmp_path / "cache.sqlite3")
    page_key = storage.key_for("https://example.test/b")
    with storage._lock, storage._connect() as connection:
        connection.execute(
            "INSERT INTO page_cache(cache_key, url, payload_json, created_at, expires_at) VALUES(?, ?, ?, ?, ?)",
            (page_key, "https://example.test/b", "{broken", 0.0, 99999999999.0),
        )
        connection.execute(
            "INSERT INTO session_state(session_key, payload_json, updated_at) VALUES(?, ?, ?)",
            ("broken", "{broken", 0.0),
        )
    assert storage.get_page("https://example.test/b") is None
    assert storage.load_session("broken") == {}


def test_commerce_jsonld_fallback_snippets_dedupe_and_rank(tmp_path: Path) -> None:
    commerce = _agent_module("commerce")
    schemas = _agent_module("schemas")
    sites = tmp_path / "sites.json"
    sites.write_text(
        json.dumps(
            {
                "sites": [
                    {
                        "domain": "shop.test",
                        "name": "Shop",
                        "type": "marketplace",
                        "trust_base": 0.91,
                        "search_query_template": "site:{domain} {query}",
                    },
                    {
                        "domain": "compare.test",
                        "name": "Compare",
                        "type": "price_comparison",
                        "trust_base": 0.8,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    normalizer = commerce.CommerceNormalizer(sites)

    assert normalizer.site_info("www.shop.test")["name"] == "Shop"
    assert normalizer.site_info("unknown.test")["trust_base"] == 0.55
    queries = normalizer.marketplace_queries("iphone charger", limit=2)
    assert len(queries) == 2
    assert queries[1]["kind"] == "price_comparison"

    document = schemas.FetchedDocument(
        url="https://shop.test/p/1",
        final_url="https://shop.test/p/1",
        status_code=200,
        content_type="text/html",
        title="20W USB-C Charger",
        text="20W hızlı şarj",
        json_ld=[
            {
                "@context": "https://schema.org",
                "@graph": [
                    {
                        "@type": ["Thing", "Product"],
                        "name": "20W USB-C Charger",
                        "brand": {"name": "Acme"},
                        "model": "C20",
                        "sku": "SKU-20",
                        "image": {"url": "/img/c20.jpg"},
                        "aggregateRating": {"ratingValue": "4,8", "reviewCount": "1.234"},
                        "offers": [
                            {
                                "price": "499,90",
                                "priceCurrency": "TRY",
                                "availability": "https://schema.org/InStock",
                                "seller": {"name": "Official Shop"},
                                "url": "/buy/c20",
                            }
                        ],
                    }
                ],
            }
        ],
        fetched_at="2026-08-08T00:00:00+00:00",
    )
    offers = normalizer.offers_from_document(document)
    assert len(offers) == 1
    offer = offers[0]
    assert offer.price == 499.9
    assert offer.currency == "TRY"
    assert offer.url == "https://shop.test/buy/c20"
    assert offer.image_url == "https://shop.test/img/c20.jpg"
    assert offer.availability == "InStock"
    assert offer.total_cost == 499.9
    assert offer.match_key == "sku 20"

    fallback_doc = schemas.FetchedDocument(
        url="https://unknown.test/item",
        final_url="https://unknown.test/item",
        status_code=200,
        content_type="text/html",
        title="Fallback Adapter",
        text="Kampanya fiyatı 1.299,00 TL",
        meta={"og:image": "/fallback.png", "product:price:currency": "TL"},
    )
    fallback = normalizer.offers_from_document(fallback_doc)
    assert fallback[0].price == 1299.0
    assert fallback[0].image_url == "https://unknown.test/fallback.png"

    sources = [
        schemas.SourceRecord(
            url="https://shop.test/s/1",
            title="Adapter kampanya",
            snippet="Sepette 899 TL ücretsiz kargo",
            domain="shop.test",
        ),
        schemas.SourceRecord(
            url="https://shop.test/s/no-price",
            title="Adapter 20W",
            snippet="Fiyat bilgisi yok",
            domain="shop.test",
        ),
    ]
    snippet_offers = normalizer.offers_from_sources(sources)
    assert len(snippet_offers) == 1
    assert snippet_offers[0].price == 899.0
    assert snippet_offers[0].evidence[0].field == "price"

    weaker = schemas.ProductOffer(
        product_name="Acme C20 kampanya",
        url="https://shop.test/weak",
        domain="shop.test",
        seller="Official Shop",
        sku="SKU-20",
        price=550.0,
        trust_score=0.2,
    )
    weaker.match_key = normalizer.match_key(weaker)
    weaker.ensure_total()
    deduped = normalizer.deduplicate([weaker, offer])
    assert len(deduped) == 1
    assert deduped[0].url == offer.url

    cheap = schemas.ProductOffer(
        product_name="Cheap",
        url="https://compare.test/cheap",
        domain="compare.test",
        price=350.0,
        rating=4.9,
        review_count=10000,
        official_store=True,
        availability="InStock",
        trust_score=0.85,
    )
    cheap.ensure_total()
    expensive = schemas.ProductOffer(
        product_name="Expensive",
        url="https://shop.test/expensive",
        domain="shop.test",
        price=900.0,
        rating=3.0,
        official_store=False,
        availability="OutOfStock",
        trust_score=0.95,
    )
    expensive.ensure_total()
    ranked = normalizer.rank(
        [expensive, cheap],
        filters={"max_price": 700, "min_rating": 4, "available_only": True},
    )
    assert ranked == [cheap]
    assert normalizer.rank([cheap], filters={"official_store_only": True}) == [cheap]

    # Helper branches: locale parsing, invalid values and strong-ID matching.
    assert commerce._float("1.234,56 TL") == 1234.56
    assert commerce._float("not-a-number") is None
    assert commerce._currency("€") == "EUR"
    assert commerce._brand("Acme") == "Acme"
    assert commerce._offer_candidates(None) == []


def _write_zip(path: Path, members: dict[str, bytes | str]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in members.items():
            archive.writestr(name, value.encode("utf-8") if isinstance(value, str) else value)


def test_document_inspector_handles_text_office_archives_and_compression(tmp_path: Path) -> None:
    documents = _agent_module("document_tools")

    html = tmp_path / "page.html"
    html.write_text("<html><head><script>ignore()</script></head><body><h1>Hello</h1><p>World</p></body></html>", encoding="utf-8")
    inspected = documents.inspect_document(path=html, original_name=html.name, media_type="text/html")
    assert inspected["status"] == "inspected"
    assert "Hello" in inspected["text"] and "ignore()" not in inspected["text"]

    csv_path = tmp_path / "data.csv"
    csv_path.write_text("name,value\nalpha,42\n", encoding="utf-8")
    csv_result = documents.inspect_document(path=csv_path, original_name=csv_path.name, media_type="text/csv")
    assert "alpha\t42" in csv_result["text"]

    json_path = tmp_path / "data.json"
    json_path.write_text('{"ok":true,"nested":{"n":2}}', encoding="utf-8")
    json_result = documents.inspect_document(path=json_path, original_name=json_path.name, media_type="application/json")
    assert '"ok": true' in json_result["text"]

    docx = tmp_path / "renamed.bin"
    _write_zip(
        docx,
        {
            "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
            "word/document.xml": '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Embedded report text</w:t></w:r></w:p></w:body></w:document>',
            "word/media/image1.png": b"\x89PNG\r\n\x1a\n" + b"\x00" * 40,
        },
    )
    docx_result = documents.inspect_document(path=docx, original_name=docx.name, media_type="application/octet-stream")
    assert docx_result["detected_format"] == "docx"
    assert "Embedded report text" in docx_result["text"]
    assert docx_result["derived_images"][0]["data_url"].startswith("data:image/png;base64,")

    pptx = tmp_path / "slides.pptx"
    _write_zip(
        pptx,
        {
            "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/></Types>',
            "ppt/slides/slide1.xml": '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>Slide finding</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>',
        },
    )
    assert "Slide finding" in documents.inspect_document(path=pptx, original_name=pptx.name)["text"]

    odt = tmp_path / "notes.odt"
    _write_zip(
        odt,
        {
            "mimetype": "application/vnd.oasis.opendocument.text",
            "content.xml": '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"><office:body><office:text><text:p>ODF finding</text:p></office:text></office:body></office:document-content>',
        },
    )
    assert "ODF finding" in documents.inspect_document(path=odt, original_name=odt.name)["text"]

    generic_zip = tmp_path / "project.zip"
    _write_zip(generic_zip, {"src/main.py": "print('zip marker')\n", "README.md": "archive docs"})
    zip_result = documents.inspect_document(path=generic_zip, original_name=generic_zip.name)
    assert "zip marker" in zip_result["text"]
    assert "src/main.py" in zip_result["archive_members"]

    tar_path = tmp_path / "bundle.tar"
    payload = b"tar marker\n"
    with tarfile.open(tar_path, "w") as archive:
        info = tarfile.TarInfo("src/info.txt")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    tar_result = documents.inspect_document(path=tar_path, original_name=tar_path.name)
    assert "tar marker" in tar_result["text"]

    for suffix, encoder in ((".gz", gzip.compress), (".bz2", bz2.compress), (".xz", lzma.compress)):
        packed = tmp_path / f"compressed{suffix}"
        packed.write_bytes(encoder(b"compressed marker\n"))
        packed_result = documents.inspect_document(path=packed, original_name=packed.name)
        assert "compressed marker" in packed_result["text"]


def test_document_inspector_rejects_unsafe_zip_and_handles_unknown_binary(tmp_path: Path) -> None:
    documents = _agent_module("document_tools")
    missing = documents.inspect_document(path=tmp_path / "missing.bin", original_name="missing.bin")
    assert missing["status"] == "error"

    unsafe = tmp_path / "unsafe.zip"
    _write_zip(unsafe, {"../escape.txt": "nope"})
    unsafe_result = documents.inspect_document(path=unsafe, original_name=unsafe.name)
    assert unsafe_result["status"] == "error"
    assert "Unsafe" in unsafe_result["summary"] or "archive" in unsafe_result["summary"].casefold()

    binary = tmp_path / "opaque.dat"
    binary.write_bytes(b"\x00\xff\x01secret-marker\x00" + "wide-marker".encode("utf-16le"))
    binary_result = documents.inspect_document(path=binary, original_name=binary.name)
    assert binary_result["binary_inspection"] is True
    assert "marker" in binary_result["text"]
