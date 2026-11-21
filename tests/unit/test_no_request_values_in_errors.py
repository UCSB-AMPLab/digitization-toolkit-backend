"""No value a request sent, and no bound SQL parameter, reaches an error the backend gives or logs (NEH-395).

Two rules:

- Every engine is created with hide_parameters=True, so a database error's
  text names the statement and never the values bound to it (a password's
  hash, for one). app.core.db.engine is the application's only engine;
  conftest's test engine follows it.
- A request that does not validate is answered 422 with, for each error,
  its `type`, `loc` and `msg` and nothing taken from the request: no
  `input`, no `ctx`, no unknown field's name, and a `msg` that would repeat
  a submitted value replaced by the error's type
  (app.core.validation_errors.plain_errors, answered by app.main's handler
  for every router).

    python -m pytest tests/unit/test_no_request_values_in_errors.py
"""

import json

import pytest

from tests.unit.test_testing_space_admin import (
    PASSWORD,
    _bearer,
    _ordinary_user,
    _real_auth,
    _token_for,
    fresh_throttle,  # noqa: F401  (autouse fixture)
    sends,  # noqa: F401  (autouse fixture)
)

MARKER = "SecretPw-395"


# ---------------------------------------------------------------------------
# Bound parameters
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_applications_engine_hides_parameters():
    from app.core.db import engine
    assert engine.hide_parameters is True


@pytest.mark.unit
def test_a_failing_statement_does_not_carry_its_bound_value(db_session):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
    with pytest.raises(DBAPIError) as caught:
        with db_session.get_bind().connect() as conn:
            conn.execute(text("INSERT INTO no_such_table VALUES (:v)"), {"v": MARKER})
    assert MARKER not in str(caught.value)
    assert "no_such_table" in str(caught.value)


@pytest.mark.unit
def test_a_failing_password_update_does_not_carry_the_hash(client, db_session):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
    _ordinary_user(db_session, "jefa", role="admin")
    target = _ordinary_user(db_session, "ana")
    token = _token_for(client, "jefa", PASSWORD)
    db_session.execute(text("CREATE TRIGGER no_writes BEFORE UPDATE ON users "
                            "BEGIN SELECT RAISE(ABORT, 'forced failure'); END"))
    db_session.commit()
    _real_auth()
    client.cookies.clear()
    with pytest.raises(DBAPIError) as caught:
        client.put(f"/auth/users/{target.id}/password", headers=_bearer(token),
                   json={"new_password": MARKER})
    assert "forced failure" in str(caught.value)
    assert "pbkdf2_sha256$" not in str(caught.value)
    assert MARKER not in str(caught.value)


# ---------------------------------------------------------------------------
# 422 bodies
# ---------------------------------------------------------------------------

def _plain(resp):
    assert resp.status_code == 422, resp.text
    assert MARKER not in resp.text
    for error in resp.json()["detail"]:
        assert set(error) == {"type", "loc", "msg"}, error
    return resp.json()["detail"]


@pytest.mark.unit
def test_a_password_of_the_wrong_type_is_not_echoed(client, db_session):
    _ordinary_user(db_session, "jefa", role="admin")
    target = _ordinary_user(db_session, "ana")
    token = _token_for(client, "jefa", PASSWORD)
    _real_auth()
    client.cookies.clear()
    resp = client.put(f"/auth/users/{target.id}/password", headers=_bearer(token),
                      json={"new_password": [MARKER]})
    [error] = _plain(resp)
    assert error["loc"] == ["body", "new_password"]


@pytest.mark.unit
def test_a_reset_without_the_current_password_does_not_echo_the_body(client, db_session):
    _ordinary_user(db_session, "ana")
    token = _token_for(client, "ana", PASSWORD)
    _real_auth()
    client.cookies.clear()
    resp = client.post("/auth/password-reset", headers=_bearer(token), json={"new_password": MARKER})
    [error] = _plain(resp)
    assert error["loc"] == ["body", "old_password"] and error["type"] == "missing"


@pytest.mark.unit
def test_a_body_that_is_not_an_object_is_not_echoed(client, db_session):
    _real_auth()
    resp = client.post("/auth/login", json=[MARKER, {"password": MARKER}])
    _plain(resp)


@pytest.mark.unit
def test_an_unknown_fields_name_is_not_echoed(client, db_session):
    from tests.unit.test_capture_manifest_export import _as
    resp = _as(client, "admin").patch("/system/testing/account", json={MARKER: "x", "username": "otra"})
    errors = _plain(resp)
    assert any(e["type"] == "extra_forbidden" for e in errors)


@pytest.mark.unit
def test_every_router_answers_the_same_way(client, db_session):
    _real_auth()
    resp = client.post("/auth/login", json={"username": 5, "password": MARKER})
    _plain(resp)
    assert "input" not in json.dumps(resp.json())


@pytest.mark.unit
def test_a_message_that_would_repeat_the_value_is_replaced_by_the_type():
    from app.core.validation_errors import plain_errors
    errors = [{"type": "value_error", "loc": ("body", "rotate_deg"),
               "msg": f"Value error, rotate_deg must be one of (0, 90); got {MARKER}",
               "input": MARKER, "ctx": {"error": MARKER}}]
    assert plain_errors(errors) == [{"type": "value_error", "loc": ["body", "rotate_deg"], "msg": "value_error"}]


@pytest.mark.unit
def test_the_rotation_validator_names_the_allowed_values_and_not_the_one_sent():
    from app.api.cameras import _validate_rotate_deg
    with pytest.raises(ValueError) as caught:
        _validate_rotate_deg(45)
    assert "45" not in str(caught.value)
    assert "0, 90, 180, 270" in str(caught.value)


@pytest.mark.unit
def test_no_validator_of_ours_puts_the_value_it_checks_in_its_message():
    """A raise ValueError(f"...") in app/api or app/schemas never formats the function's own argument."""
    import ast
    from pathlib import Path
    root = Path(__file__).resolve().parents[2] / "app"
    found = []
    for path in list((root / "api").glob("*.py")) + list((root / "schemas").glob("*.py")):
        for fn in ast.walk(ast.parse(path.read_text())):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            args = [a.arg for a in fn.args.args if a.arg not in ("self", "cls")]
            for node in ast.walk(fn):
                if (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)
                        and getattr(node.exc.func, "id", None) == "ValueError"
                        and node.exc.args and isinstance(node.exc.args[0], ast.JoinedStr)):
                    names = {n.id for n in ast.walk(node.exc.args[0]) if isinstance(n, ast.Name)}
                    if names & set(args):
                        found.append(f"{path.name}:{node.lineno} {fn.name}")
    assert found == []
