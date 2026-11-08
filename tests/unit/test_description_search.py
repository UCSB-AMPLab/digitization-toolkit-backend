"""Searching Fisqua's descriptions from the new-volume form (NEH-321, contract 1.10).

GET /fisqua/descriptions?q= asks Fisqua's GET /api/capture/v1/descriptions?q=
with the unit's token, through the gated transport, the call budget, the
configuration stop and the hold the 1.8 lookup uses
(app.core.unit_mode.search_descriptions), and answers the list validated.

    python -m pytest tests/unit/test_description_search.py

Version: 0.3.3
"""

import httpx
import pytest

from tests.unit.test_capture_manifest_export import TENANT, _as, tenant  # noqa: F401  (fixtures)
from tests.unit.test_fisqua_outbox import TOKEN, fisqua  # noqa: F401  (fixture)
from tests.unit.test_unit_mode import BASE, Server, _http, _result, _unit, recipient  # noqa: F401  (fixture)


def _entry(code, title="Título", level="file", display="1790-1795", start="1790", end="1795"):
    return {"referenceCode": code, "title": title, "level": level,
            "dates": {"display": display, "start": start, "end": end}}


class SearchTransport:
    """Answers the 1.10 search from a script; keeps each query it was sent."""

    def __init__(self, answers=()):
        self.answers = list(answers)
        self.asked = []
        # Called with each query just before it is answered (NEH-321 round 2).
        self.on_call = None

    def search_descriptions(self, q):
        self.asked.append(q)
        if self.on_call is not None:
            self.on_call(q)
        answer = self.answers.pop(0) if self.answers else _result("error", error="no answer")
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def search(fisqua, monkeypatch):
    """Fisqua configured and the lookup's gated transport a SearchTransport."""
    from app.core import unit_mode
    transport = SearchTransport()
    monkeypatch.setattr(unit_mode, "get_lookup_transport", lambda: transport)
    return transport


def _get(client, q="co-ahr", role="admin"):
    params = {} if q is None else {"q": q}
    return _as(client, role).get("/fisqua/descriptions", params=params)


def _code(resp, status, code):
    assert resp.status_code == status, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == code
    assert isinstance(detail["message"], str) and detail["message"]
    return detail


# ---------------------------------------------------------------------------
# Mode, configuration, role and query
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("mode", ["offline", "backup"])
def test_outside_fisqua_mode_the_search_is_a_409_and_nothing_is_asked(client, db_session, search, mode):
    _unit(db_session, mode)
    _code(_get(client), 409, "off_in_this_mode")
    assert search.asked == []


@pytest.mark.unit
def test_in_fisqua_mode_without_the_link_configured_the_search_is_a_409(client, db_session, monkeypatch):
    from app.core import unit_mode
    transport = SearchTransport()
    monkeypatch.setattr(unit_mode, "get_lookup_transport", lambda: transport)
    _unit(db_session, "fisqua")
    _code(_get(client), 409, "fisqua_not_configured")
    assert transport.asked == []


@pytest.mark.unit
@pytest.mark.parametrize("role, status", [("admin", 200), ("operator", 403), ("reviewer", 403)])
def test_only_an_admin_may_search(client, db_session, search, role, status):
    """In fisqua mode only an admin can create a volume (volumes_from_fisqua), so only an admin searches."""
    _unit(db_session, "fisqua")
    search.answers = [_result("search", response={"results": []})]
    assert _get(client, role=role).status_code == status
    assert search.asked == (["co-ahr"] if role == "admin" else [])


@pytest.mark.unit
def test_the_search_needs_a_signed_in_user(client, db_session, search):
    _unit(db_session, "fisqua")
    assert client.get("/fisqua/descriptions", params={"q": "co-ahr"}).status_code == 401
    assert search.asked == []


@pytest.mark.unit
@pytest.mark.parametrize("q", [None, "", "ab", "  ab  ", "   "])
def test_a_query_under_three_characters_is_a_400_without_calling_fisqua(client, db_session, search, q):
    _unit(db_session, "fisqua")
    _code(_get(client, q=q), 400, "query_too_short")
    assert search.asked == []


@pytest.mark.unit
def test_the_query_is_trimmed_before_it_is_sent(client, db_session, search):
    _unit(db_session, "fisqua")
    search.answers = [_result("search", response={"results": []})]
    resp = _get(client, q="  caj002  ")
    assert resp.status_code == 200, resp.text
    assert search.asked == ["caj002"]


# ---------------------------------------------------------------------------
# Fisqua's answers
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_200_is_passed_on_validated(client, db_session, search):
    _unit(db_session, "fisqua")
    search.answers = [_result("search", response={"results": [
        _entry("co-ahr-con-caj002-car001", title="Autos de la Real Audiencia", level="file"),
        {"title": "no code"},
        {"referenceCode": 12, "title": "a number"},
        "not an object",
        {"referenceCode": "co-ahr-con", "title": None, "level": None, "dates": "1790",
         "extra": "dropped"},
        {"referenceCode": "co-ahr-con-caj003", "dates": {"display": 1790, "start": None, "end": "1800"}},
    ]})]
    resp = _get(client)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"results": [
        _entry("co-ahr-con-caj002-car001", title="Autos de la Real Audiencia", level="file"),
        {"referenceCode": "co-ahr-con", "title": "", "level": None,
         "dates": {"display": None, "start": None, "end": None}},
        {"referenceCode": "co-ahr-con-caj003", "title": "", "level": None,
         "dates": {"display": None, "start": None, "end": "1800"}},
    ]}


@pytest.mark.unit
def test_at_most_twenty_results_are_passed_on(client, db_session, search):
    _unit(db_session, "fisqua")
    search.answers = [_result("search", response={"results": [_entry(f"xx-{i:03d}") for i in range(30)]})]
    results = _get(client).json()["results"]
    assert [r["referenceCode"] for r in results] == [f"xx-{i:03d}" for i in range(20)]


@pytest.mark.unit
@pytest.mark.parametrize("body", [{"results": "x"}, {}, {"results": None}])
def test_a_200_without_a_results_list_is_unreachable(client, db_session, search, body):
    _unit(db_session, "fisqua")
    search.answers = [_result("search", response=body)]
    _code(_get(client), 503, "fisqua_unreachable")


@pytest.mark.unit
def test_fisqua_query_too_short_is_a_400(client, db_session, search):
    _unit(db_session, "fisqua")
    search.answers = [_result("query_too_short", status_code=400, response={"rule": "query-too-short"})]
    _code(_get(client), 400, "query_too_short")


@pytest.mark.unit
@pytest.mark.parametrize("status", [404, 405])
def test_a_fisqua_without_1_10_answers_an_empty_list_marked_no_search(client, db_session, search, status):
    _unit(db_session, "fisqua")
    search.answers = [_result("unsupported", status_code=status)]
    resp = _get(client)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"results": [], "search": False}


@pytest.mark.unit
def test_a_429_is_passed_on_with_its_retry_after_and_holds_the_link_back(client, db_session, search):
    from app.core import fisqua_outbox
    _unit(db_session, "fisqua")
    search.answers = [_result("error", status_code=429, error="Fisqua answered HTTP 429", retry_after=30.0)]
    resp = _get(client)
    _code(resp, 429, "rate_limited")
    assert resp.headers["Retry-After"] == "30"
    assert fisqua_outbox.call_budget.cooling()
    # While the link cools down, Fisqua is not asked again.
    resp = _get(client)
    _code(resp, 429, "rate_limited")
    assert 0 < int(resp.headers["Retry-After"]) <= 30
    assert search.asked == ["co-ahr"]


@pytest.mark.unit
def test_the_search_leaves_the_decisions_reserve_and_five_calls_for_the_save_time_lookup(
        client, db_session, search, monkeypatch):
    from app.core import fisqua_contract as fc
    from app.core import fisqua_outbox
    _unit(db_session, "fisqua")
    reserve = fc.DECISIONS_RESERVED_CALLS + 5
    assert fc.SEARCH_RESERVED_CALLS == reserve
    budget = fisqua_outbox.CallBudget(limit=reserve + 2)
    monkeypatch.setattr(fisqua_outbox, "call_budget", budget)
    search.answers = [_result("search", response={"results": []}) for _ in range(3)]
    assert _get(client).status_code == 200
    assert _get(client).status_code == 200
    resp = _get(client)
    _code(resp, 429, "rate_limited")
    assert "Retry-After" not in resp.headers
    assert len(search.asked) == 2
    # What the search left is still there for the 1.8 lookup and the decisions reads.
    assert all(budget.take() for _ in range(reserve))
    assert not budget.take()


@pytest.mark.unit
def test_a_spent_call_budget_is_a_429_without_calling_fisqua(client, db_session, search, monkeypatch):
    from app.core import fisqua_outbox
    _unit(db_session, "fisqua")
    monkeypatch.setattr(fisqua_outbox, "call_budget", fisqua_outbox.CallBudget(limit=0))
    _code(_get(client), 429, "rate_limited")
    assert search.asked == []


@pytest.mark.unit
def test_each_search_takes_from_the_call_budget(client, db_session, search, monkeypatch):
    from app.core import fisqua_outbox
    _unit(db_session, "fisqua")
    monkeypatch.setattr(fisqua_outbox, "call_budget",
                        fisqua_outbox.CallBudget(limit=2 + fisqua_outbox.fc.SEARCH_RESERVED_CALLS))
    search.answers = [_result("search", response={"results": []}) for _ in range(3)]
    assert _get(client).status_code == 200
    assert _get(client).status_code == 200
    _code(_get(client), 429, "rate_limited")
    assert len(search.asked) == 2


@pytest.mark.unit
@pytest.mark.parametrize("answer", [
    _result("error", status_code=503, error="Fisqua answered HTTP 503"),
    _result("error", status_code=500, error="Fisqua answered HTTP 500"),
    _result("error", status_code=400, error="Fisqua answered HTTP 400"),
    _result("error", error="Fisqua could not be reached: timeout: x"),
    _result("error", error="Fisqua could not be reached: ConnectError: x"),
    httpx.ReadTimeout("slow"),
])
def test_fisqua_down_or_unreachable_is_a_503(client, db_session, search, answer):
    _unit(db_session, "fisqua")
    search.answers = [answer]
    detail = _code(_get(client), 503, "fisqua_unreachable")
    assert TOKEN not in str(detail)


@pytest.mark.unit
@pytest.mark.parametrize("answer", [
    lambda: (_ for _ in ()).throw(RuntimeError(f"connection refused, Authorization: Bearer {TOKEN}")),
    lambda: _result("error", error=f"Fisqua could not be reached: ConnectError: Bearer {TOKEN}"),
])
def test_the_token_never_reaches_the_answer(client, db_session, search, answer):
    _unit(db_session, "fisqua")

    def call(q):
        result = answer()
        search.answers.insert(0, result)

    search.on_call = call
    resp = _get(client)
    detail = _code(resp, 503, "fisqua_unreachable")
    assert TOKEN not in resp.text
    assert TOKEN not in str(detail)


@pytest.mark.unit
def test_the_database_connection_is_released_before_fisqua_is_called(client, db_session, search):
    """A stalled Fisqua must not hold a pooled connection for the length of the call."""
    _unit(db_session, "fisqua")
    seen = []

    def call(q):
        seen.append((db_session.in_transaction(), db_session.get_bind().pool.checkedout()))

    search.on_call = call
    search.answers = [_result("search", response={"results": []})]
    assert _get(client).status_code == 200
    assert seen == [(False, 0)]


@pytest.mark.unit
@pytest.mark.parametrize("status, reason", [(401, "token"), (403, "token")])
def test_a_token_refusal_takes_the_link_config_error_handling(client, db_session, search, status, reason):
    from app.core import fisqua_outbox
    _unit(db_session, "fisqua")
    search.answers = [_result("config_error", status_code=status, reason=reason,
                              error=f"Fisqua refused this unit's token (HTTP {status})")]
    _code(_get(client), 503, "fisqua_unreachable")
    assert fisqua_outbox.config_error() is not None
    # The link is stopped: the next search does not call Fisqua.
    _code(_get(client), 503, "fisqua_unreachable")
    assert len(search.asked) == 1


@pytest.mark.unit
def test_a_switch_out_of_fisqua_meanwhile_is_a_409(client, db_session, search):
    _unit(db_session, "fisqua")
    search.answers = [_result("off_in_this_mode", error="the unit is in backup mode")]
    _code(_get(client), 409, "off_in_this_mode")


@pytest.mark.unit
def test_the_hold_stops_the_search(client, db_session, search, monkeypatch):
    from app.core import offsite_hold
    _unit(db_session, "fisqua")
    monkeypatch.setattr(offsite_hold, "held", lambda *a, **k: True)
    _code(_get(client), 503, "fisqua_unreachable")
    assert search.asked == []


# ---------------------------------------------------------------------------
# The transport
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_search_path_and_query():
    from app.core import fisqua_contract as fc
    assert fc.DESCRIPTION_SEARCH_PATH == "/api/capture/v1/descriptions"
    server = Server(httpx.Response(200, json={"results": []}))
    result = _http(server, bypass=True).search_descriptions("co-ahr con&x=1")
    assert result.outcome == "search" and result.response == {"results": []}
    request = server.requests[0]
    assert request.method == "GET"
    assert request.url.path == "/api/capture/v1/descriptions"
    assert request.url.params["q"] == "co-ahr con&x=1"
    assert list(request.url.params.keys()) == ["q"]
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"


@pytest.mark.unit
@pytest.mark.parametrize("response, outcome", [
    (httpx.Response(200, json={"results": []}), "search"),
    (httpx.Response(200, json=[]), "error"),
    (httpx.Response(200, text="not json"), "error"),
    (httpx.Response(400, json={"rule": "query-too-short"}), "query_too_short"),
    (httpx.Response(400, json={"rule": "other"}), "error"),
    (httpx.Response(404), "unsupported"),
    (httpx.Response(405, headers={"Allow": "POST"}), "unsupported"),
    (httpx.Response(401), "config_error"),
    (httpx.Response(403), "config_error"),
    (httpx.Response(302, headers={"Location": "https://elsewhere"}), "config_error"),
    (httpx.Response(429, headers={"Retry-After": "12"}), "error"),
    (httpx.Response(503), "error"),
    (httpx.Response(500), "error"),
])
def test_the_search_answers(response, outcome):
    result = _http(Server(response), bypass=True).search_descriptions("co-ahr")
    assert result.outcome == outcome
    assert result.status_code == response.status_code
    if response.status_code == 429:
        assert result.retry_after == 12.0


@pytest.mark.unit
def test_a_network_failure_on_the_search_is_an_error_naming_its_kind():
    result = _http(Server(httpx.ConnectError(f"refused {TOKEN}")), bypass=True).search_descriptions("co-ahr")
    assert result.outcome == "error" and "ConnectError" in result.error


@pytest.mark.unit
def test_the_search_has_its_own_short_timeout():
    from app.core import fisqua_contract as fc
    from app.core.fisqua_http import HttpFisquaTransport
    assert fc.SEARCH_TIMEOUT_SECONDS == 5.0

    def transport(server):
        # A client with the link's own 60 s timeout, as get_transport builds it.
        return HttpFisquaTransport(BASE, TOKEN, bypass_mode=True,
                                   client=httpx.Client(transport=httpx.MockTransport(server), timeout=60.0))

    server = Server(httpx.Response(200, json={"results": []}))
    transport(server).search_descriptions("co-ahr")
    timeout = server.requests[0].extensions["timeout"]
    assert timeout["connect"] == 5.0 and timeout["read"] == 5.0
    # The other calls keep the link's own timeout.
    server = Server(httpx.Response(200, json={"referenceCode": "xx-a"}))
    transport(server).get_description("xx-a")
    assert server.requests[0].extensions["timeout"]["read"] == 60.0


@pytest.mark.unit
def test_a_stalled_fisqua_is_a_timeout_and_unreachable(client, db_session, fisqua, monkeypatch):
    """Through the real transport: a read timeout is a 503, not a hung request."""
    from app.core import unit_mode
    server = Server(httpx.ReadTimeout("stalled"))
    monkeypatch.setattr(unit_mode, "get_lookup_transport", lambda: _http(server))
    _unit(db_session, "fisqua")
    detail = _code(_get(client), 503, "fisqua_unreachable")
    assert "timeout" in detail["error"]


@pytest.mark.unit
@pytest.mark.parametrize("mode", ["offline", "backup"])
def test_the_gated_transport_sends_no_search_outside_fisqua_mode(db_session, fisqua, mode):
    _unit(db_session, mode)
    server = Server(httpx.Response(200, json={"results": []}))
    assert _http(server).search_descriptions("co-ahr").outcome == "off_in_this_mode"
    assert server.requests == []


@pytest.mark.unit
def test_the_null_transport_sends_nothing():
    from app.core.fisqua_outbox import NullTransport
    assert NullTransport().search_descriptions("co-ahr").outcome == "error"
