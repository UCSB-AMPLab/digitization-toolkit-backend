"""No refusal of a whole list leaves its hash recorded.

A list id's hash (`<reports>/<list-id>/sha256`) is the reference against which
later files under that id are checked (ListIdReused). A refusal of the whole
list (the project would take a volume's stem directory, an operator would
have to create the project) is decided before the hash is written; if one
still happens after this run wrote the first hash (a project deleted between
the check and its use), the hash this run wrote is removed, but only while
this run holds the list lock: without it, another run of the same file may
already have passed the check against that hash, so the hash stays (and is
logged), and a different file under the id is refused as before. A corrected
file under the same id then imports. A hash recorded by an earlier import is
never removed; one that cannot be removed is logged with its path.

The lock-unavailable test is the verifier's.
"""

import pytest

import errno
import threading

from tests.unit.test_list_import import VALID_ID, _as, _item, _list_with, _post, _volumes, reports_root  # noqa: F401


def _hash_file(reports_root, doc):
    return reports_root / doc["id"] / "sha256"


@pytest.mark.unit
def test_a_list_refused_for_a_stem_directory_records_no_hash_and_the_corrected_file_imports(
        client, db_session, reports_root):
    from app.models.collection import Collection
    from app.models.project import Project

    p = Project(name="Otro")
    db_session.add(p)
    db_session.commit()
    db_session.add(Collection(name="V", project_id=p.id, filename_stem="xx-test-cesta"))
    db_session.commit()
    _as("adm", "admin")
    doc = _list_with([_item(code="xx-test-950", stem="xx-test-950")])
    doc["list"]["name"] = "XX-Test-Cesta"

    refused = _post(client, doc)

    assert refused.status_code == 422, refused.text
    assert not _hash_file(reports_root, doc).exists()

    doc["list"]["name"] = "Cesta corregida"
    resp = _post(client, doc)

    assert resp.status_code == 200, resp.text
    assert resp.json()["created"] == 1
    assert _hash_file(reports_root, doc).exists()


@pytest.mark.unit
def test_an_operator_refused_under_the_lock_records_no_hash(client, db_session, reports_root, monkeypatch):
    """The project the operator's list names is found before the lock and gone under it."""
    import app.core.list_import as li
    from app.models.project import Project

    db_session.add(Project(name="Prueba válida"))
    db_session.commit()
    real_find = li._find_project
    calls = {"n": 0}

    def gone_after_the_first_look(db, name):
        calls["n"] += 1
        return real_find(db, name) if calls["n"] == 1 else None

    monkeypatch.setattr(li, "_find_project", gone_after_the_first_look)
    _as("op", "operator")
    doc = _list_with([_item(code="xx-test-950", stem="xx-test-950")])

    resp = _post(client, doc)

    assert resp.status_code == 403, resp.text
    assert not _hash_file(reports_root, doc).exists()

    monkeypatch.setattr(li, "_find_project", real_find)
    _as("adm", "admin")
    assert _post(client, doc).status_code == 200


@pytest.mark.unit
def test_a_refusal_after_this_run_wrote_the_first_hash_removes_it(client, db_session, reports_root, monkeypatch):
    import app.core.list_import as li

    def refused(*a, **k):
        raise li.ProjectNeedsAdmin(li.PROJECT_NEEDS_ADMIN_MESSAGE)

    monkeypatch.setattr(li, "_project_for", refused)
    _as("adm", "admin")
    doc = _list_with([_item(code="xx-test-950", stem="xx-test-950")])

    assert _post(client, doc).status_code == 403
    assert not _hash_file(reports_root, doc).exists()
    assert set(_volumes(db_session)) == set()


@pytest.mark.unit
def test_a_refusal_never_removes_a_hash_an_earlier_import_recorded(client, db_session, reports_root, monkeypatch):
    import app.core.list_import as li

    _as("adm", "admin")
    doc = _list_with([_item(code="xx-test-950", stem="xx-test-950")])
    assert _post(client, doc).status_code == 200
    recorded = _hash_file(reports_root, doc).read_bytes()

    def refused(*a, **k):
        raise li.ProjectNeedsAdmin(li.PROJECT_NEEDS_ADMIN_MESSAGE)

    monkeypatch.setattr(li, "_project_for", refused)
    assert _post(client, doc).status_code == 403
    assert _hash_file(reports_root, doc).read_bytes() == recorded


def _sessions(db_session, n):
    from sqlalchemy.orm import sessionmaker
    mk = sessionmaker(bind=db_session.get_bind())
    return [mk() for _ in range(n)]


def _project(db_session, name="Prueba válida"):
    from app.models.project import Project
    db_session.add(Project(name=name))
    db_session.commit()


def _try(li, *a, **k):
    try:
        r = li.import_list(*a, **k)
        return f"created {r['created']}"
    except (li.ListIdReused, li.ListUnverifiable, li.ProjectNeedsAdmin, li.ListRefused, li.ListHashNotRecorded) as exc:
        return type(exc).__name__


@pytest.mark.unit
def test_without_the_lock_a_late_refusal_keeps_the_hash_another_run_is_importing_under(
        db_session, reports_root, monkeypatch, caplog):
    """A (H1) writes the first hash and is refused late; B (H1) passed the check on A's hash and is still
    importing; with the hash gone and B's report not yet stored, C (H2) imports under the same id."""
    import app.core.list_import as li

    def nolock(fd, op):
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(li.fcntl, "flock", nolock)
    _project(db_session)
    real_pf = li._project_for
    b_past_hash, a_done, c_done = threading.Event(), threading.Event(), threading.Event()

    def pf(db, name, actor, may, notes=None):
        t = threading.current_thread().name
        if t == "A":
            b_past_hash.wait(10)
            raise li.ProjectNeedsAdmin("project deleted between the check and its use (simulated)")
        if t == "B":
            b_past_hash.set()
            a_done.wait(10)
            c_done.wait(10)
        return real_pf(db, name, actor, may, notes)

    monkeypatch.setattr(li, "_project_for", pf)
    # A must write the first hash. Started together, B could reach the exclusive create first; A would
    # then compare against B's hash, write nothing, and be refused with no hash of its own to keep or log.
    real_check = li._check_hash
    a_wrote_hash = threading.Event()

    def check(directory, list_id, file_sha256):
        t = threading.current_thread().name
        if t == "B":
            a_wrote_hash.wait(10)
        try:
            return real_check(directory, list_id, file_sha256)
        finally:
            if t == "A":
                a_wrote_hash.set()

    monkeypatch.setattr(li, "_check_hash", check)
    sa, sb, sc = _sessions(db_session, 3)
    doc_h1 = _list_with([_item(code="xx-test-001", stem="xx-test-001")])
    doc_h2 = _list_with([_item(code="xx-test-002", stem="xx-test-002")])
    out = {}
    caplog.set_level("WARNING", logger="app.core.list_import")
    ta = threading.Thread(name="A", target=lambda: out.__setitem__("A", _try(li, sa, doc_h1, "op", False, file_sha256="1" * 64)))
    tb = threading.Thread(name="B", target=lambda: out.__setitem__("B", _try(li, sb, doc_h1, "ad", True, file_sha256="1" * 64)))
    ta.start()
    tb.start()
    ta.join(15)
    a_done.set()
    h = reports_root / VALID_ID / "sha256"
    out["hash after A"] = h.read_text().strip()[:6] if h.exists() else None
    out["C"] = _try(li, sc, doc_h2, "ad", True, file_sha256="2" * 64)
    c_done.set()
    tb.join(15)
    out["hash at end"] = h.read_text().strip()[:6] if h.exists() else None
    for s in (sa, sb, sc):
        s.close()
    vols = sorted(_volumes(db_session))
    assert out["A"] == "ProjectNeedsAdmin"
    assert out["hash after A"] == "111111"
    assert out["C"] == "ListIdReused", "two different files were imported under one list id"
    assert out["B"] == "created 1" and out["hash at end"] == "111111"
    assert vols == ["xx-test-001"]
    assert str(h) in caplog.text


@pytest.mark.unit
def test_a_hash_that_cannot_be_removed_is_logged_with_its_path(client, db_session, reports_root, monkeypatch, caplog):
    import app.core.list_import as li

    _as("adm", "admin")
    doc = _list_with([_item(code="xx-test-950", stem="xx-test-950")])
    directory = reports_root / doc["id"]

    def refused(*a, **k):
        directory.chmod(0o555)
        raise li.ProjectNeedsAdmin(li.PROJECT_NEEDS_ADMIN_MESSAGE)

    monkeypatch.setattr(li, "_project_for", refused)
    with caplog.at_level("ERROR", logger="app.core.list_import"):
        try:
            assert _post(client, doc).status_code == 403
        finally:
            directory.chmod(0o755)

    assert (directory / "sha256").exists()
    assert str(directory / "sha256") in caplog.text
