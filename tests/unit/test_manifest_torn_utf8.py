"""A manifest line torn inside a multibyte UTF-8 character is skipped like any other torn line.

Copies are written with ensure_ascii=False, so a power cut can cut a line
inside an accented character. Every reader decodes a manifest line by line:
a line that does not decode, like one that does not parse, is skipped and
logged, and the lines around it are read. Before, the whole-file decode
raised UnicodeDecodeError, and with a journal pending every startup refused
to serve.
"""

import json

import pytest

from tests.unit.test_volume_layout_by_stem import (  # noqa: F401
    STEMS, _capture_records, capture_settings, data_dir, legacy,
)

TORN = '{"capture_id": "cap-torn", "project_name": "Prueba v'.encode() + "á".encode()[:1]


def _write(path, *parts: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(parts))


@pytest.mark.unit
def test_the_index_and_the_bag_read_around_a_torn_multibyte_line(override_projects_root, capture_settings, caplog):
    from app.core.integrity import build_fixity_index, capture_record_lines

    m = override_projects_root / "xx-test-u8" / "metadata" / "manifest.jsonl"
    good1 = json.dumps({"capture_id": "cap-1", "files": [], "project_name": "Prueba válida"}, ensure_ascii=False)
    good2 = json.dumps({"capture_id": "cap-2", "files": []})
    _write(m, good1.encode() + b"\n", TORN + b"\n", good2.encode() + b"\n")

    with caplog.at_level("WARNING", logger="app.core.integrity"):
        index = build_fixity_index(override_projects_root.resolve())

    assert index.captured == {"cap-1", "cap-2"}
    assert str(m) in caplog.text
    assert capture_record_lines(override_projects_root.resolve(), {"cap-1", "cap-2"}) == [good1, good2]


@pytest.mark.unit
def test_a_startup_after_a_copy_torn_inside_a_character_serves_and_repairs(db_session, legacy):
    import app.core.volume_layout as vl

    _capture_records(db_session, legacy)
    vl.migrate_volume_layout_at_startup(db_session)
    own = legacy["root"] / STEMS["a1"] / "metadata" / "manifest.jsonl"
    _write(own, TORN)  # the only line, cut inside "á", no newline

    vl.migrate_volume_layout_at_startup(db_session)  # raised UnicodeDecodeError before

    raw = own.read_bytes().split(b"\n")
    assert raw[0] == TORN
    assert [json.loads(l)["capture_id"] for l in raw[1:] if l] == ["cap-i1"]


@pytest.mark.unit
def test_a_pending_journal_whose_volume_manifest_is_torn_inside_a_character_completes(db_session, legacy, monkeypatch):
    import app.core.volume_layout as vl

    _capture_records(db_session, legacy)

    class Crash(BaseException):
        pass

    real = vl._move

    def crash(src, dst):
        raise Crash()

    monkeypatch.setattr(vl, "_move", crash)
    with pytest.raises(Crash):
        vl.migrate_volume_layout(db_session)
    db_session.rollback()
    monkeypatch.setattr(vl, "_move", real)
    own = legacy["root"] / STEMS["a1"] / "metadata" / "manifest.jsonl"
    _write(own, own.read_bytes(), TORN)
    assert vl.read_layout_journal() is not None

    report = vl.migrate_volume_layout(db_session)

    assert report["completed_journal"] is True
