"""A list holding an escaped lone surrogate (`\\ud800`) is refused whole, before anything is written.

json.loads accepts the escape and returns a string no UTF-8 encoder will
write, so the item it sat in was rejected, echoed into the report, and the
report's storage (ensure_ascii=False) raised: a 500 with no report after
other items were committed. The file is now refused with 400 when any key or
string in it holds a lone surrogate. A surrogate pair is one character and
is accepted. Report storage escapes what it cannot encode, so it cannot
fail this way.
"""

import json

import pytest

from tests.unit.test_list_import import VALID, _as, _item, _list_with, _load, _volumes, reports_root  # noqa: F401


def _post_text(client, text: str):
    return client.post("/imports/digitisation-list",
                       files={"file": ("list.json", text.encode("utf-8"), "application/json")})


def _with_escape(doc, placeholder, escape):
    return json.dumps(doc).replace(placeholder, escape)


@pytest.mark.unit
@pytest.mark.parametrize("where", ["referenceCode", "title", "key", "listName", "array"])
def test_a_lone_surrogate_anywhere_refuses_the_list_and_writes_nothing(client, db_session, reports_root, where):
    from app.models.project import Project

    _as("adm", "admin")
    good = _item(code="xx-test-950", stem="xx-test-950")
    bad = _item(code="xx-test-951", stem="xx-test-951")
    if where == "referenceCode":
        bad["referenceCode"] = "SURROGATE"
    elif where == "title":
        bad["title"] = "SURROGATE"
    elif where == "key":
        bad["SURROGATE"] = 1
    elif where == "array":
        bad["notes"] = ["fine", "SURROGATE"]
    doc = _list_with([good, bad])
    if where == "listName":
        doc["list"]["name"] = "SURROGATE"

    resp = _post_text(client, _with_escape(doc, "SURROGATE", "x\\ud800"))

    assert resp.status_code == 400, resp.text
    assert "surrogate" in resp.json()["detail"]
    assert _volumes(db_session) == {}
    assert db_session.query(Project).count() == 0
    assert not reports_root.exists() or not any(p.is_file() and p.name != ".lock" for p in reports_root.rglob("*"))


@pytest.mark.unit
def test_an_escaped_surrogate_pair_is_one_character_and_accepted(client, db_session, reports_root):
    _as("adm", "admin")
    doc = _list_with([_item(code="xx-test-950", stem="xx-test-950", title="TITLE")])

    resp = _post_text(client, _with_escape(doc, "TITLE", "Tomo \\ud83d\\ude00"))

    assert resp.status_code == 200, resp.text
    assert resp.json()["created"] == 1


@pytest.mark.unit
def test_report_storage_escapes_what_it_cannot_encode(reports_root):
    from app.core.list_import import store_report

    path = store_report({"listId": "00000000-0000-4000-8000-0000000000aa", "rejects": [{"referenceCode": "x\ud800"}]})

    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["rejects"][0]["referenceCode"] == "x\ud800"


@pytest.mark.unit
def test_a_repeated_key_holding_a_lone_surrogate_is_a_400_that_can_be_sent(client, db_session, reports_root):
    _as("adm", "admin")
    text = json.dumps(_list_with([_item()]))
    text = text[:-1] + ', "x\\ud800": 1, "x\\ud800": 2}'

    resp = _post_text(client, text)

    assert resp.status_code == 400, resp.text
    assert "appears twice" in resp.json()["detail"]
