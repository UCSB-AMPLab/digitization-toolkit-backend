"""No refusal of a whole list leaves its hash recorded.

A list id's hash (`<reports>/<list-id>/sha256`) is the reference against which
later files under that id are checked (ListIdReused). A refusal of the whole
list (the project would take a volume's stem directory, an operator would
have to create the project) is decided before the hash is written; if one
still happens after this run wrote the first hash (a project deleted between
the check and its use), the hash this run wrote is removed. A corrected file
under the same id then imports. A hash recorded by an earlier import is never
removed.
"""

import pytest

from tests.unit.test_list_import import _as, _item, _list_with, _post, _volumes, reports_root  # noqa: F401


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
