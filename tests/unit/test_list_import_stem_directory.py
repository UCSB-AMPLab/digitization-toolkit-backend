"""The list import meets the stem directory checks of app.models.collection item by item.

A stem that is a project's directory, or whose `<projects root>/<stem>/`
already holds files, is refused by the model's flush listener. The import
turns that refusal into a reject of that item with a fixed reason; the other
items are still created. A list whose project name folds to an existing stem
is refused whole, before any volume is written.
"""

import pytest

from tests.unit.test_list_import import _as, _item, _list_with, _post, _volumes, reports_root  # noqa: F401


@pytest.mark.unit
def test_an_item_whose_stem_directory_holds_files_is_rejected_and_the_rest_created(
        client, db_session, reports_root, override_projects_root):
    stray = override_projects_root / "xx-test-901" / "captures" / "left.jpg"
    stray.parent.mkdir(parents=True)
    stray.write_bytes(b"left by someone")
    _as("adm", "admin")

    resp = _post(client, _list_with([_item(), _item(code="xx-test-950", stem="xx-test-950")]))

    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["created"] == 1
    assert [(r["position"], r["reason"]) for r in report["rejects"]] == [(1, "filename_stem_directory_occupied")]
    assert set(_volumes(db_session)) == {"xx-test-950"}
    assert stray.read_bytes() == b"left by someone"


@pytest.mark.unit
def test_an_item_whose_stem_is_a_project_directory_is_rejected_and_the_rest_created(
        client, db_session, reports_root):
    from app.models.project import Project

    db_session.add(Project(name="XX-TEST-901"))
    db_session.commit()
    _as("adm", "admin")

    resp = _post(client, _list_with([_item(), _item(code="xx-test-950", stem="xx-test-950")]))

    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert [(r["position"], r["reason"]) for r in report["rejects"]] == [(1, "filename_stem_is_project_directory")]
    assert set(_volumes(db_session)) == {"xx-test-950"}


@pytest.mark.unit
def test_a_list_whose_project_would_take_a_stems_directory_is_refused_whole(client, db_session, reports_root):
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

    resp = _post(client, doc)

    assert resp.status_code == 422, resp.text
    assert "xx-test-cesta" in resp.json()["detail"]
    assert set(_volumes(db_session)) == {None}
    assert db_session.query(Project).count() == 1
