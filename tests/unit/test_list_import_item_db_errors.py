"""A database failure at any step of one item costs that item only.

The lookups for a volume already holding the item's reference code or stem
run inside the same rollback-and-reject handling as the insert, so a lost
connection there is reported as `refused_by_database` for that item, the
other items are still created, and the report is stored.
"""

import pytest
from sqlalchemy.exc import OperationalError

from tests.unit.test_list_import import _as, _item, _list_with, _post, _volumes, reports_root  # noqa: F401


@pytest.mark.unit
@pytest.mark.parametrize("failing", ["external_id", "filename_stem"])
def test_a_failed_duplicate_lookup_rejects_that_item_and_the_rest_are_created(
        client, db_session, reports_root, monkeypatch, failing):
    from app.models.collection import Collection

    real_query = db_session.query
    # The stem lookup is armed by the item's reference-code lookup, so that
    # the whole-list check on the project name (which also looks stems up)
    # runs normally.
    state = {"failed": False, "armed": failing == "external_id"}

    class FailingQuery:
        def __init__(self, q):
            self.q = q

        def filter(self, *criteria):
            text = " ".join(str(c) for c in criteria)
            if "external_id" in text:
                state["armed"] = True
            if state["armed"] and not state["failed"] and failing in text:
                state["failed"] = True
                raise OperationalError("SELECT", {}, Exception("connection lost"))
            return self.q.filter(*criteria)

        def __getattr__(self, name):
            return getattr(self.q, name)

    def query(*entities, **kw):
        q = real_query(*entities, **kw)
        return FailingQuery(q) if len(entities) == 1 and entities[0] is Collection.id else q

    monkeypatch.setattr(db_session, "query", query)
    _as("adm", "admin")

    resp = _post(client, _list_with([_item(), _item(code="xx-test-950", stem="xx-test-950")]))

    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert [(r["position"], r["reason"]) for r in report["rejects"]] == [(1, "refused_by_database")]
    assert report["created"] == 1 and report["reportStored"] is True
    monkeypatch.setattr(db_session, "query", real_query)
    assert set(_volumes(db_session)) == {"xx-test-950"}
