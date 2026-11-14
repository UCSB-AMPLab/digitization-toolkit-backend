"""The unit's device id (contract 1.11 §12.1): made once, kept, and never made again.

    python -m pytest tests/unit/test_device_id.py

Version: 0.3.3
"""

import os
import re
import stat

import pytest

CONTRACT_DEVICE_ID = re.compile(r"^[a-z0-9-]{1,32}$")
CROCKFORD_LOWER = "0123456789abcdefghjkmnpqrstvwxyz"


@pytest.fixture
def data_dir(monkeypatch, tmp_path):
    from app.core import config

    path = tmp_path / "data"
    monkeypatch.setattr(config.settings, "DATA_DIR", str(path))
    return path


def test_created_once_and_kept(data_dir):
    from app.core import device_id

    first = device_id.initialise()
    assert (data_dir / "device-id").read_text(encoding="ascii") == first
    assert device_id.initialise() == first
    assert device_id.read() == first
    assert device_id.state() == {"deviceId": first, "error": None}


def test_file_is_world_readable_and_owner_writable(data_dir):
    from app.core import device_id

    old = os.umask(0o077)
    try:
        device_id.initialise()
    finally:
        os.umask(old)
    assert stat.S_IMODE((data_dir / "device-id").stat().st_mode) == 0o644


def test_shape_matches_contract(data_dir):
    from app.core import device_id

    assert device_id.ALPHABET == CROCKFORD_LOWER
    seen = {device_id.generate() for _ in range(200)}
    for value in seen | {device_id.initialise()}:
        assert len(value) == 8
        assert set(value) <= set(CROCKFORD_LOWER)
        assert CONTRACT_DEVICE_ID.match(value)
    assert len(seen) > 190


def test_written_atomically(data_dir, monkeypatch):
    """A write that fails part way leaves neither a device-id nor a temporary file."""
    from app.core import device_id

    def broken(fd):
        raise OSError("disk full")

    monkeypatch.setattr(device_id.os, "fsync", broken)
    with pytest.raises(OSError):
        device_id.create()
    assert not (data_dir / "device-id").exists()
    assert list(data_dir.iterdir()) == []


def test_never_replaces_a_file_that_appeared_meanwhile(data_dir, monkeypatch):
    """Two writers: the second keeps the first one's id."""
    from app.core import device_id

    real_link = os.link

    def racing_link(src, dst, *args, **kwargs):
        with open(dst, "w", encoding="ascii") as f:
            f.write("abcdefgh")
        return real_link(src, dst, *args, **kwargs)

    monkeypatch.setattr(device_id.os, "link", racing_link)
    assert device_id.create() == "abcdefgh"
    assert (data_dir / "device-id").read_text(encoding="ascii") == "abcdefgh"
    assert [p.name for p in data_dir.iterdir()] == ["device-id"]


@pytest.mark.parametrize("content", ["", "ABCDEFGH", "abcdefg", "abcdefghi", "abcdefgi", "abc defg", "abcdefgh\n\n"])
def test_invalid_file_is_an_error_and_is_kept(data_dir, caplog, content):
    from app.core import device_id

    data_dir.mkdir(parents=True)
    (data_dir / "device-id").write_text(content, encoding="ascii")
    with pytest.raises(device_id.DeviceIdInvalid):
        device_id.read()
    assert device_id.state() == {"deviceId": None, "error": "invalid"}
    assert device_id.initialise() is None
    assert any(r.levelname == "ERROR" for r in caplog.records)
    assert (data_dir / "device-id").read_text(encoding="ascii") == content


def test_one_trailing_newline_is_accepted(data_dir):
    """An id written with an editor ends in a newline; it is the same id."""
    from app.core import device_id

    data_dir.mkdir(parents=True)
    (data_dir / "device-id").write_text("abcdefgh\n", encoding="ascii")
    assert device_id.read() == "abcdefgh"


def test_missing_file_reads_as_missing(data_dir):
    from app.core import device_id

    assert device_id.state() == {"deviceId": None, "error": "missing"}
    with pytest.raises(FileNotFoundError):
        device_id.read()


def test_unreadable_file_is_an_error(data_dir, monkeypatch):
    from app.core import device_id

    data_dir.mkdir(parents=True)
    (data_dir / "device-id").mkdir()
    assert device_id.state() == {"deviceId": None, "error": "unreadable"}
    assert device_id.initialise() is None


def test_lifespan_writes_the_device_id(monkeypatch):
    """The wiring: the application's registered lifespan calls initialise, after the unit's mode."""
    import asyncio

    import app.core.db as core_db
    import app.core.storage_ops as storage_ops
    from app import main as app_main
    from app.core import device_id, unit_mode
    from capture import service as capture_service

    monkeypatch.setattr(app_main, "init_db", lambda: None)
    monkeypatch.setattr(app_main, "assert_schema_at_head", lambda: None)
    monkeypatch.setattr(core_db, "SessionLocal", lambda: type("S", (), {"close": lambda self: None})())
    monkeypatch.setattr(storage_ops, "reconcile_pending_rename", lambda db: None)
    monkeypatch.setattr(capture_service, "shutdown_backend", lambda: None)
    order = []
    monkeypatch.setattr(unit_mode, "initialise", lambda: order.append("mode"))
    monkeypatch.setattr(device_id, "initialise", lambda: order.append("device_id"))

    async def serve():
        async with app_main.app.router.lifespan_context(app_main.app):
            assert order == ["mode", "device_id"]

    asyncio.run(serve())
