from __future__ import annotations

import importlib.util
import io
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _upload_service_class():
    try:
        from crowai.uploads.service import UploadService
        return UploadService
    except ModuleNotFoundError as exc:
        if exc.name != "werkzeug":
            raise

    names = ("werkzeug", "werkzeug.datastructures", "werkzeug.utils")
    previous = {name: sys.modules.get(name) for name in names}
    package = types.ModuleType("werkzeug")
    package.__path__ = []  # type: ignore[attr-defined]
    datastructures = types.ModuleType("werkzeug.datastructures")
    datastructures.FileStorage = object  # type: ignore[attr-defined]
    utils = types.ModuleType("werkzeug.utils")
    utils.secure_filename = lambda value: Path(str(value)).name.replace(" ", "_")  # type: ignore[attr-defined]
    sys.modules.update({
        "werkzeug": package,
        "werkzeug.datastructures": datastructures,
        "werkzeug.utils": utils,
    })
    try:
        spec = importlib.util.spec_from_file_location(
            "_isolated_upload_service",
            ROOT / "crowai" / "uploads" / "service.py",
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.UploadService
    finally:
        for name, value in previous.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


class FailingRepository:
    def __init__(self) -> None:
        self.deleted: list[tuple[str, str]] = []

    def create(self, **kwargs) -> None:
        raise RuntimeError("database unavailable")

    def delete(self, upload_id: str, owner_key: str) -> None:
        self.deleted.append((upload_id, owner_key))


class PassiveModelService:
    def inspect_file(self, **kwargs):
        return {"status": "inspected", "summary": "ok"}


class DummyStorage:
    def __init__(self, content: bytes, *, filename: str, mimetype: str) -> None:
        self.stream = io.BytesIO(content)
        self.filename = filename
        self.mimetype = mimetype


def test_failed_repository_write_does_not_leave_orphaned_upload(tmp_path: Path) -> None:
    UploadService = _upload_service_class()
    repository = FailingRepository()
    service = UploadService(
        repository,
        PassiveModelService(),
        root=tmp_path / "uploads",
        maximum_bytes=1024 * 1024,
    )
    storage = DummyStorage(
        b"hello upload",
        filename="notes.custom",
        mimetype="application/octet-stream",
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        service.save(files=[storage], owner_key="owner-1", model_id="chat/v1.0")

    assert repository.deleted
    assert not list((tmp_path / "uploads").rglob("*.part"))
    assert not [path for path in (tmp_path / "uploads").rglob("*") if path.is_file()]


def test_sniff_recognizes_compressed_and_archive_signatures(tmp_path: Path) -> None:
    UploadService = _upload_service_class()
    cases = [
        (b"BZh91AY&SY", "application/x-bzip2"),
        (b"\xfd7zXZ\x00" + b"0" * 8, "application/x-xz"),
        (b"Rar!\x1a\x07\x01\x00" + b"0" * 8, "application/vnd.rar"),
    ]
    for index, (content, expected) in enumerate(cases):
        path = tmp_path / f"item-{index}.bin"
        path.write_bytes(content)
        assert UploadService._sniff(path, "application/octet-stream") == expected


class RecordingRepository:
    def __init__(self) -> None:
        self.created: list[dict] = []

    def create(self, **kwargs) -> None:
        self.created.append(kwargs)

    def delete(self, upload_id: str, owner_key: str) -> None:
        return None


class ObservingStream:
    def __init__(self, content: bytes, root: Path) -> None:
        self._stream = io.BytesIO(content)
        self.root = root
        self.part_mode: int | None = None

    def read(self, size: int = -1) -> bytes:
        data = self._stream.read(size)
        if not data and self.part_mode is None:
            import stat

            parts = list(self.root.rglob("*.part"))
            assert len(parts) == 1
            self.part_mode = stat.S_IMODE(parts[0].stat().st_mode)
        return data


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX modes are not a Windows ACL guarantee")
def test_upload_root_owner_temp_and_final_files_are_private(tmp_path: Path) -> None:
    import stat

    UploadService = _upload_service_class()
    root = tmp_path / "uploads"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    repository = RecordingRepository()
    service = UploadService(
        repository,
        PassiveModelService(),
        root=root,
        maximum_bytes=1024 * 1024,
    )
    assert stat.S_IMODE(root.stat().st_mode) == 0o700

    stream = ObservingStream(b"private upload", root)
    storage = DummyStorage(b"", filename="notes.txt", mimetype="text/plain")
    storage.stream = stream
    result = service.save(files=[storage], owner_key="owner-private", model_id="chat/v1.0")

    assert result and repository.created
    assert stream.part_mode == 0o600
    final = Path(repository.created[0]["stored_path"])
    assert final.is_file()
    assert stat.S_IMODE(final.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(final.stat().st_mode) == 0o600


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX modes are not a Windows ACL guarantee")
def test_upload_service_hardens_existing_private_tree(tmp_path: Path) -> None:
    import stat

    UploadService = _upload_service_class()
    root = tmp_path / "uploads"
    owner = root / "existing-owner"
    owner.mkdir(parents=True)
    old = owner / "old.bin"
    old.write_bytes(b"old")
    root.chmod(0o755)
    owner.chmod(0o755)
    old.chmod(0o644)

    UploadService(RecordingRepository(), PassiveModelService(), root=root, maximum_bytes=1024)

    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(owner.stat().st_mode) == 0o700
    assert stat.S_IMODE(old.stat().st_mode) == 0o600


def test_failed_upload_never_deletes_another_concurrent_part_file(tmp_path: Path) -> None:
    import hashlib

    UploadService = _upload_service_class()
    root = tmp_path / "uploads"
    repository = FailingRepository()
    service = UploadService(repository, PassiveModelService(), root=root, maximum_bytes=1024 * 1024)
    owner_key = "same-owner-concurrent"
    owner_dir = root / hashlib.sha256(owner_key.encode("utf-8")).hexdigest()[:24]
    owner_dir.mkdir(parents=True, exist_ok=True)
    foreign_part = owner_dir / ".other-request.part"
    foreign_part.write_bytes(b"still-being-written")

    with pytest.raises(RuntimeError, match="database unavailable"):
        service.save(
            files=[DummyStorage(b"request-a", filename="a.txt", mimetype="text/plain")],
            owner_key=owner_key,
            model_id="chat/v1.0",
        )

    assert foreign_part.read_bytes() == b"still-being-written"
    # The failed request's own temp/final file is gone; only the simulated
    # concurrent request's part remains.
    assert [item.name for item in owner_dir.iterdir()] == [foreign_part.name]
