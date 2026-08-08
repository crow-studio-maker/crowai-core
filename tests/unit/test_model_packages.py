from __future__ import annotations

from pathlib import Path

from models.registry import ModelRegistry


ROOT = Path(__file__).resolve().parents[2]


def _registry() -> ModelRegistry:
    return ModelRegistry(ROOT / "models", development=True, strict_capabilities=True)


def test_agent_local_attachment_analysis_does_not_force_web(tmp_path: Path) -> None:
    attachment = tmp_path / "report.custom"
    attachment.write_text("Alpha finding\nBeta finding\n", encoding="utf-8")
    registry = _registry()

    plan = registry.prepare(
        model_id="agent/v1.0",
        question="Bu yüklenen dosyayı ayrıntılı incele.",
        language="tr",
        conversation=[],
        attachments=[
            {
                "name": "report.custom",
                "media_type": "application/octet-stream",
                "_internal_path": str(attachment),
            }
        ],
        snapshot={},
    )

    assert plan["metadata"]["web_access"] is False
    assert plan["metadata"]["needs_current_information"] is False
    assert plan["query_variations"] == []
    assert "Alpha finding" in plan["metadata"]["attachment_context"]
    assert "_internal_path" not in str(plan["metadata"])


def test_agent_product_lookup_requests_current_sources() -> None:
    registry = _registry()
    plan = registry.prepare(
        model_id="agent/v1.0",
        question="iPhone için güncel 20 W şarj aleti fiyatlarını bul ve karşılaştır",
        language="tr",
        conversation=[],
        attachments=[],
        snapshot={},
    )

    assert plan["metadata"]["web_access"] is True
    assert plan["metadata"]["plan"]["needs_product_normalization"] is True
    assert plan["query_variations"]


def test_code_attachment_only_request_becomes_local_review() -> None:
    registry = _registry()
    plan = registry.prepare(
        model_id="code/v1.0",
        question="",
        language="tr",
        conversation=[],
        attachments=[
            {
                "name": "main.py",
                "media_type": "text/x-python",
                "text": "def add(a, b):\n    return a + b\n",
            }
        ],
        snapshot={},
    )

    assert plan["metadata"]["web_access"] is False
    assert plan["metadata"]["task_kind"] in {"analysis", "review"}
    assert "def add" in plan["metadata"]["attachment_context"]
    assert plan["query_variations"] == []


def test_chat_stays_direct_and_offline() -> None:
    registry = _registry()
    plan = registry.prepare(
        model_id="chat/v1.0",
        question="Merhaba, kısa bir test.",
        language="tr",
        conversation=[],
        attachments=[],
        snapshot={},
    )
    assert plan["metadata"]["execution_path"] == "direct_chat"
    assert plan["metadata"]["web_access"] is False


def test_package_health_contains_no_local_paths() -> None:
    registry = _registry()
    for model_id in ("agent/v1.0", "code/v1.0", "chat/v1.0"):
        health = registry.health_check(model_id)
        assert "models/" not in str(health).replace("\\", "/")
        assert set(health["files"].values()) <= {True, False}


def _model_result(plan: dict) -> dict:
    return {
        "analysis": {},
        "sources": [],
        "meta": {"model": {"metadata": plan.get("metadata", {})}},
    }


def test_model_packages_do_not_import_core_or_walk_above_v1() -> None:
    for mode in ("agent", "code", "chat"):
        package = ROOT / "models" / mode / "v1.0"
        for path in package.rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            assert "from crowai" not in source, path
            assert "import crowai" not in source, path
            assert "PROJECT_ROOT" not in source, path
            assert ".parents[" not in source, path


def test_agent_inspector_detects_image_magic_and_hides_private_paths(tmp_path: Path) -> None:
    target = tmp_path / "picture.unknown"
    target.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    registry = _registry()

    inspected = registry.inspect_file(
        model_id="agent/v1.0",
        path=target,
        original_name="picture.unknown",
        media_type="application/octet-stream",
    )

    assert inspected["status"] == "image_ready"
    assert inspected["media_type"] == "image/png"
    serialized = str(inspected)
    assert str(tmp_path) not in serialized
    assert "image_path" not in inspected
    assert "path" not in inspected


def test_agent_local_finalize_uses_attachment_evidence_without_web(monkeypatch, tmp_path: Path) -> None:
    target = tmp_path / "report.custom"
    target.write_text("Confirmed local fact: 42\n", encoding="utf-8")
    registry = _registry()
    plan = registry.prepare(
        model_id="agent/v1.0",
        question="Bu dosyadaki doğrulanmış bulguyu özetle.",
        language="tr",
        conversation=[],
        attachments=[{
            "name": "report.custom",
            "media_type": "application/octet-stream",
            "_internal_path": str(target),
        }],
        snapshot={},
    )
    module = registry._load("agent/v1.0")
    globals_ = module.finalize_result.__globals__
    calls: list[dict] = []

    def fake_generate(messages, **kwargs):
        calls.append({"messages": messages, "kwargs": kwargs})
        payload = messages[-1]["content"]
        assert "Confirmed local fact: 42" in payload
        return '{"answer":"Dosyadaki doğrulanmış bulgu 42.","recommendations":[],"warnings":[],"follow_up_options":[]}'

    monkeypatch.setitem(globals_, "generate_response", fake_generate)
    result = registry.finalize(
        model_id="agent/v1.0",
        question="Bu dosyadaki doğrulanmış bulguyu özetle.",
        language="tr",
        result=_model_result(plan),
    )

    assert result["success"] is True
    assert result["answer"] == "Dosyadaki doğrulanmış bulgu 42."
    assert result["sources"] == []
    assert len(calls) == 1


def test_code_helpers_reject_unsafe_paths_and_validate_python() -> None:
    registry = _registry()
    module = registry._load("code/v1.0")
    globals_ = module.prepare_request.__globals__

    assert globals_["_safe_path"]("../../outside.py") is None
    assert globals_["_safe_path"]("C:/outside.py") is None
    assert globals_["_safe_path"]("src/main.py") == "src/main.py"
    assert "Python syntax error" in globals_["_validate_source"]("main.py", "def broken(:\n")
    assert globals_["_validate_source"]("main.py", "def ok():\n    return 1\n") is None


def test_code_single_file_generation_repairs_invalid_python(monkeypatch) -> None:
    registry = _registry()
    plan = registry.prepare(
        model_id="code/v1.0",
        question="main.py içinde küçük bir Python fonksiyonu oluştur",
        language="tr",
        conversation=[],
        attachments=[],
        snapshot={},
    )
    module = registry._load("code/v1.0")
    globals_ = module.finalize_result.__globals__
    responses = iter(["def broken(:\n", "def answer():\n    return 42\n"])
    calls: list[dict] = []

    def fake_generate(messages, **kwargs):
        calls.append({"messages": messages, "kwargs": kwargs})
        return next(responses)

    monkeypatch.setitem(globals_, "generate_response", fake_generate)
    result = registry.finalize(
        model_id="code/v1.0",
        question="main.py içinde küçük bir Python fonksiyonu oluştur",
        language="tr",
        result=_model_result(plan),
    )

    assert result["success"] is True
    assert len(result["artifacts"]) == 1
    assert result["artifacts"][0]["filename"] == "main.py"
    assert "return 42" in result["artifacts"][0]["code"]
    assert result["warnings"] == []
    assert len(calls) == 2


def test_chat_finalize_preserves_direct_local_reply(monkeypatch) -> None:
    registry = _registry()
    plan = registry.prepare(
        model_id="chat/v1.0",
        question="Merhaba",
        language="tr",
        conversation=[],
        attachments=[],
        snapshot={},
    )
    module = registry._load("chat/v1.0")
    globals_ = module.finalize_result.__globals__
    monkeypatch.setitem(globals_, "generate_reply", lambda messages: "Merhaba! Nasıl yardımcı olabilirim?")

    result = registry.finalize(
        model_id="chat/v1.0",
        question="Merhaba",
        language="tr",
        result=_model_result(plan),
    )

    assert result["success"] is True
    assert result["answer"] == "Merhaba! Nasıl yardımcı olabilirim?"
    assert result["sources"] == []


def test_agent_vision_does_not_follow_public_path_fields(tmp_path: Path) -> None:
    secret = tmp_path / "secret.png"
    secret.write_bytes(b"\x89PNG\r\n\x1a\n" + b"private")
    registry = _registry()
    module = registry._load("agent/v1.0")
    globals_ = module.prepare_request.__globals__
    analyze_images = globals_["analyze_images"]
    collect_visual_inputs = analyze_images.__globals__["collect_visual_inputs"]

    visual = collect_visual_inputs([{
        "name": "looks-safe.png",
        "media_type": "image/png",
        "path": str(secret),
        "local_path": str(secret),
    }])

    assert visual == []


def test_agent_office_document_exposes_embedded_images_only_inside_pipeline(tmp_path: Path) -> None:
    import zipfile

    target = tmp_path / "visual.docx"
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "word/document.xml",
            '<?xml version="1.0"?><w:document xmlns:w="urn:test"><w:t>Visible text</w:t></w:document>',
        )
        archive.writestr("word/media/picture.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)

    registry = _registry()
    module = registry._load("agent/v1.0")
    inspect_document = module.prepare_request.__globals__["inspect_document"]
    internal = inspect_document(
        path=target,
        original_name="visual.docx",
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    public = registry.inspect_file(
        model_id="agent/v1.0",
        path=target,
        original_name="visual.docx",
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )

    assert "Visible text" in internal["text"]
    assert internal["derived_images"][0]["data_url"].startswith("data:image/png;base64,")
    assert public["derived_image_count"] == 1
    assert "derived_images" not in public


def test_agent_recognizes_renamed_docx_by_container_structure(tmp_path: Path) -> None:
    import zipfile

    target = tmp_path / "renamed.payload"
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "word/document.xml",
            '<?xml version="1.0"?><w:document xmlns:w="urn:test"><w:t>Agent renamed DOCX</w:t></w:document>',
        )

    registry = _registry()
    inspected = registry.inspect_file(
        model_id="agent/v1.0",
        path=target,
        original_name="renamed.payload",
        media_type="application/octet-stream",
    )

    assert inspected["detected_format"] == "docx"
    assert "Agent renamed DOCX" in inspected["text"]
    assert inspected["media_type"].endswith("wordprocessingml.document")


def test_agent_product_normalizer_resolves_relative_product_urls() -> None:
    registry = _registry()
    module = registry._load("agent/v1.0")
    commerce = module.prepare_request.__globals__["COMMERCE"]
    FetchedDocument = commerce.offers_from_document.__globals__["FetchedDocument"]
    document = FetchedDocument(
        url="https://shop.example/products/phone",
        final_url="https://shop.example/products/phone",
        status_code=200,
        content_type="text/html",
        title="Phone",
        text="Phone 9999 TRY",
        json_ld=[{
            "@type": "Product",
            "name": "Phone X",
            "image": "/media/phone.jpg",
            "offers": {"price": "9999", "priceCurrency": "TRY", "url": "./buy"},
        }],
    )

    offers = commerce.offers_from_document(document)

    assert offers
    assert offers[0].image_url == "https://shop.example/media/phone.jpg"
    assert offers[0].url == "https://shop.example/products/buy"


def test_code_inspector_handles_renamed_compressed_source(tmp_path: Path) -> None:
    import gzip

    target = tmp_path / "source.payload"
    with gzip.open(target, "wb") as handle:
        handle.write(b"def compressed_value():\n    return 7\n")

    registry = _registry()
    inspected = registry.inspect_file(
        model_id="code/v1.0",
        path=target,
        original_name="source.payload",
        media_type="application/octet-stream",
    )

    assert inspected["status"] == "inspected"
    assert "compressed_value" in inspected["text"]


def test_code_inspector_indexes_utf16_strings_from_unknown_binary(tmp_path: Path) -> None:
    target = tmp_path / "binary.unknown"
    target.write_bytes(b"\x00\xff\x01" + "CrowAI UTF16 marker".encode("utf-16-le") + b"\x02")
    registry = _registry()

    inspected = registry.inspect_file(
        model_id="code/v1.0",
        path=target,
        original_name="binary.unknown",
        media_type="application/octet-stream",
    )

    assert "CrowAI UTF16 marker" in inspected["text"]


def test_code_test_request_generates_test_filename_instead_of_overwriting_source() -> None:
    registry = _registry()
    module = registry._load("code/v1.0")
    globals_ = module.prepare_request.__globals__
    guess = globals_["_guess_single_filename"]

    assert guess("main.py için pytest test yaz", [], task_kind="tests") == "test_main.py"
    assert guess("src/app.ts için unit test yaz", [], task_kind="tests") == "src/app.test.ts"
    assert guess("service.go için test yaz", [], task_kind="tests") == "service_test.go"


def test_agent_short_follow_up_inherits_previous_product_lookup_intent() -> None:
    registry = _registry()
    plan = registry.prepare(
        model_id="agent/v1.0",
        question="iphone",
        language="tr",
        conversation=[
            {"role": "user", "content": "Bana uygun bir şarj aleti bulur musun?"},
            {"role": "assistant", "content": "Hangi cihaz için?"},
        ],
        attachments=[],
        snapshot={},
    )

    assert plan["metadata"]["web_access"] is True
    assert plan["metadata"]["plan"]["needs_product_normalization"] is True
    assert plan["metadata"]["plan"]["intent"] == "product_lookup"
    assert any("iphone" in item["query"].casefold() and "şarj" in item["query"].casefold() for item in plan["query_variations"])


def test_agent_acknowledgement_does_not_repeat_previous_product_search() -> None:
    registry = _registry()
    plan = registry.prepare(
        model_id="agent/v1.0",
        question="teşekkürler",
        language="tr",
        conversation=[{"role": "user", "content": "iPhone şarj aleti fiyatlarını bul"}],
        attachments=[],
        snapshot={},
    )

    assert plan["metadata"]["web_access"] is False
    assert plan["metadata"]["needs_current_information"] is False
