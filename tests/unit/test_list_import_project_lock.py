"""Two lists of different ids whose names match once normalised create one project, not two.

The list lock serialises imports of one list id only. Finding the project
by its normalised name and creating it is serialised across all imports by
an exclusive lock on `<imports>/.project-create.lock`: the import that takes
it second looks again under it and finds the first one's project. An import
that cannot take that lock and would have to create a project is refused,
with nothing written, rather than risk a second project.

Version: 0.1.0-beta
"""

import threading
import time

import pytest

from tests.unit.test_list_import import _item, _list_with, _volumes, reports_root  # noqa: F401


def _sessions(db_session, n):
    from sqlalchemy.orm import sessionmaker
    mk = sessionmaker(bind=db_session.get_bind())
    return [mk() for _ in range(n)]


@pytest.mark.unit
def test_basket_and_basket_imported_at_once_share_one_project(db_session, reports_root, monkeypatch):
    import app.core.list_import as li
    from app.models.project import Project

    real_find = li._find_project
    calls = {}

    def slow_when_missing(db, name):
        found = real_find(db, name)
        t = threading.current_thread().name
        calls[t] = calls.get(t, 0) + 1
        if found is None and calls[t] > 1:  # the lookup that decides whether to create
            time.sleep(0.5)
        return found

    monkeypatch.setattr(li, "_find_project", slow_when_missing)
    sa, sb = _sessions(db_session, 2)
    a = _list_with([_item(code="xx-test-001", stem="xx-test-001")], id="00000000-0000-4000-8000-00000000000a")
    a["list"]["name"] = "Basket"
    b = _list_with([_item(code="xx-test-002", stem="xx-test-002")], id="00000000-0000-4000-8000-00000000000b")
    b["list"]["name"] = "basket"
    out = {}
    ta = threading.Thread(name="A", target=lambda: out.__setitem__("A", li.import_list(sa, a, "ad", True)))
    tb = threading.Thread(name="B", target=lambda: out.__setitem__("B", li.import_list(sb, b, "ad", True)))
    ta.start()
    tb.start()
    ta.join(15)
    tb.join(15)
    sa.close()
    sb.close()

    db_session.expire_all()
    assert db_session.query(Project).count() == 1
    assert out["A"]["projectId"] == out["B"]["projectId"]
    assert sorted(c for c in (out["A"]["projectCreated"], out["B"]["projectCreated"])) == [False, True]
    assert set(_volumes(db_session)) == {"xx-test-001", "xx-test-002"}


@pytest.mark.unit
def test_without_the_project_lock_a_list_that_would_create_a_project_is_refused(db_session, reports_root, monkeypatch):
    import app.core.list_import as li
    from app.models.project import Project

    monkeypatch.setattr(li, "_take_project_lock", lambda: None)
    doc = _list_with([_item(code="xx-test-001", stem="xx-test-001")])

    with pytest.raises(li.ProjectLockUnavailable):
        li.import_list(db_session, doc, "ad", True)

    assert db_session.query(Project).count() == 0
    assert _volumes(db_session) == {}


@pytest.mark.unit
def test_without_the_project_lock_a_list_for_an_existing_project_imports(db_session, reports_root, monkeypatch):
    import app.core.list_import as li
    from app.models.project import Project

    db_session.add(Project(name="Prueba válida"))
    db_session.commit()
    monkeypatch.setattr(li, "_take_project_lock", lambda: None)

    report = li.import_list(db_session, _list_with([_item(code="xx-test-001", stem="xx-test-001")]), "ad", True)

    assert report["created"] == 1 and report["projectCreated"] is False
