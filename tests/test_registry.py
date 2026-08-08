from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from models.registry import ModelRegistry


def write_model(root: Path, mode: str = "code", version: str = "v1.0", *, contract: int = 1, callbacks: bool = True, marker: str = "one") -> Path:
    directory = root / mode / version
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {
        "id": version,
        "name": "CrowAI Code",
        "version": "1.0",
        "description": "A valid local test model.",
        "display_order": 30,
        "capabilities": ["conversation"],
        "model_contract_version": contract,
        "minimum_core_version": "4.0.0",
    }
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    if callbacks:
        source = f'''def prepare_request(**kwargs):\n    return {{"metadata": {{}}}}\n\ndef finalize_result(**kwargs):\n    return {{"answer": "{marker}", "marker": "{marker}"}}\n'''
    else:
        source = "def prepare_request(**kwargs):\n    return {}\n"
    (directory / "__init__.py").write_text(source, encoding="utf-8")
    return directory


class RegistryTests(unittest.TestCase):
    def test_zero_models_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry = ModelRegistry(Path(temporary))
            status = registry.status()
            self.assertFalse(status["models_available"])
            self.assertEqual([], status["models"])
            self.assertIn("No valid", status["model_error"])

    def test_valid_manifest_and_mode_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_model(root)
            (root / "code" / "mode.json").write_text(json.dumps({
                "id": "code", "name": "Code Studio", "description": "Code mode", "display_order": 5,
            }), encoding="utf-8")
            registry = ModelRegistry(root)
            self.assertEqual("code/v1.0", registry.default_id())
            mode = registry.list_modes()[0]
            self.assertEqual("Code Studio", mode["name"])
            self.assertEqual(1, len(mode["models"]))

    def test_invalid_contract_does_not_stop_core(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_model(root, contract=99)
            registry = ModelRegistry(root)
            self.assertEqual([], registry.list_models())
            serialized = json.dumps(registry.issues())
            self.assertIn("invalid_model_package", serialized)
            self.assertNotIn(str(root), serialized)

    def test_missing_callback_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_model(root, callbacks=False)
            registry = ModelRegistry(root)
            self.assertFalse(registry.status()["models_available"])

    def test_duplicate_public_id_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_model(root, version="v1")
            write_model(root, version="V1")
            registry = ModelRegistry(root)
            self.assertEqual(1, len(registry.list_models()))
            self.assertTrue(any(issue["code"] == "invalid_model_package" for issue in registry.issues()))

    def test_development_refresh_reloads_module(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = write_model(root, marker="one")
            registry = ModelRegistry(root, development=True)
            first = registry.finalize(model_id="code/v1.0", question="x", language="en", result={})
            self.assertEqual("one", first["marker"])
            source = (directory / "__init__.py").read_text(encoding="utf-8").replace('"one"', '"two"')
            (directory / "__init__.py").write_text(source, encoding="utf-8")
            registry.refresh()
            second = registry.finalize(model_id="code/v1.0", question="x", language="en", result={})
            self.assertEqual("two", second["marker"])

    def test_config_file_reference_cannot_escape_version_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = write_model(root)
            (directory / "config.json").write_text(
                json.dumps({"model_file": "../../outside.gguf"}), encoding="utf-8"
            )
            registry = ModelRegistry(root)
            self.assertEqual([], registry.list_models())
            self.assertTrue(any(issue["code"] == "invalid_model_package" for issue in registry.issues()))

    def test_runtime_and_model_references_must_use_package_subdirectories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = write_model(root)
            (directory / "config.json").write_text(
                json.dumps({"runtime_file": "llama-server.exe", "model_file": "weights.gguf"}),
                encoding="utf-8",
            )
            registry = ModelRegistry(root)
            self.assertEqual([], registry.list_models())

    def test_valid_package_local_runtime_and_model_references_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = write_model(root)
            (directory / "config.json").write_text(
                json.dumps({"runtime_file": "runtime/llama-server.exe", "model_file": "model/weights.gguf"}),
                encoding="utf-8",
            )
            registry = ModelRegistry(root)
            self.assertEqual(["code/v1.0"], [item["id"] for item in registry.list_models()])


if __name__ == "__main__":
    unittest.main()


def test_development_refresh_shutdowns_old_module_before_reload(tmp_path: Path) -> None:
    root = tmp_path / "models"
    directory = write_model(root, marker="old")
    shutdown_marker = tmp_path / "old-shutdown.txt"
    (directory / "__init__.py").write_text(
        "from pathlib import Path\n"
        "def prepare_request(**kwargs): return {'metadata': {}}\n"
        "def finalize_result(**kwargs): return {'answer': 'old'}\n"
        f"def shutdown(): Path({str(shutdown_marker)!r}).write_text('closed', encoding='utf-8')\n",
        encoding="utf-8",
    )
    registry = ModelRegistry(root, development=True)
    old_module = registry._load("code/v1.0")
    assert old_module.finalize_result()["answer"] == "old"

    (directory / "__init__.py").write_text(
        "from pathlib import Path\n"
        f"if not Path({str(shutdown_marker)!r}).is_file(): raise RuntimeError('old backend not shut down')\n"
        "def prepare_request(**kwargs): return {'metadata': {}}\n"
        "def finalize_result(**kwargs): return {'answer': 'new'}\n"
        "def shutdown(): return None\n",
        encoding="utf-8",
    )

    registry.refresh()
    assert shutdown_marker.read_text(encoding="utf-8") == "closed"
    new_module = registry._load("code/v1.0")
    assert new_module is not old_module
    assert new_module.finalize_result()["answer"] == "new"
    registry.shutdown()


def test_bounded_model_shutdown_blocks_hanging_reload_without_orphaning_module(tmp_path: Path) -> None:
    import time
    from types import ModuleType

    registry = ModelRegistry(tmp_path, development=True)
    module = ModuleType("hanging_model")

    def shutdown() -> None:
        time.sleep(0.5)

    module.shutdown = shutdown  # type: ignore[attr-defined]
    started = time.monotonic()
    assert registry._shutdown_module_bounded("code/v1.0", module, timeout_seconds=0.05) is False
    assert time.monotonic() - started < 0.3


def test_failed_shutdown_blocks_development_reload_and_keeps_old_module_reachable(tmp_path: Path) -> None:
    root = tmp_path / "models"
    directory = write_model(root, marker="old")
    (directory / "__init__.py").write_text(
        "def prepare_request(**kwargs): return {'metadata': {}}\n"
        "def finalize_result(**kwargs): return {'answer': 'old'}\n"
        "def shutdown(): raise RuntimeError('cannot stop backend')\n",
        encoding="utf-8",
    )
    registry = ModelRegistry(root, development=True)
    old_module = registry._load("code/v1.0")

    (directory / "__init__.py").write_text(
        "def prepare_request(**kwargs): return {'metadata': {}}\n"
        "def finalize_result(**kwargs): return {'answer': 'new'}\n"
        "def shutdown(): return None\n",
        encoding="utf-8",
    )
    registry.refresh()

    assert registry._modules["code/v1.0"][1] is old_module
    assert any(issue["code"] == "model_shutdown_blocked_reload" for issue in registry.issues())
