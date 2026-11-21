"""The testing space's access rule on every route (NEH-391): enforce_space.

app.main includes every router with `dependencies=[Depends(enforce_space)]`,
so every route of a router runs it, before the route's own dependencies.
It reads the matched route's entry in app.core.space_routes and:

* leaves a public route, and a request with no valid session, to the route
  itself: it authenticates nobody, and a 401 or the CSRF 403 stays the
  route's own answer;
* refuses the testing account with 403 on a route the table does not admit
  it to, and on a route the table has no entry for;
* resolves each target the request names (path, query or JSON body) to its
  space with app.core.testing_space.spaces_of, and answers 404 `not_found`
  for material in a space the caller does not reach: the testing account
  reaches testing material only, an operator and a reviewer real work
  only, an administrator real work, and testing material too on a route
  marked `admin_reads_testing`, and on a list route the space its `space`
  query parameter asks for. A caller whose role the route's RoleChecker
  refuses is answered that 403 first, as before. A target naming no row is
  left to the handler, as is a value that does not validate as the
  route's own parameter would, a body that is not JSON, a body that does
  not validate as the route's body model (the handler's 422), and a body
  whose field is unset or null.

A query target is checked in the value the handler receives: the last one
sent for a scalar parameter, every one for a list parameter. The JSON body
is read only on a route that declares a body target and only for a caller
with a valid session, and only when FastAPI has read it as JSON for the
handler, so Starlette's parsed copy is reused rather than parsed again; a
body that cannot be read is left to the route. It is validated with the
matched route's own body fields, as FastAPI validates it for the handler
(request_body_to_args), and a body target is read as the attribute of
that name of the validated model (or the body parameter of that name):
aliases, defaults and coercions are the handler's. A body target is
declared by its field name, never by an alias.

Version: 0.3.3
"""

from __future__ import annotations

import email.message
import typing
from typing import Any, List

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, TypeAdapter, ValidationError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.api.auth import RoleChecker, session_user_or_none
from app.api.deps import get_db_dependency
from app.core import space_routes, testing_space

_ONE_ID = TypeAdapter(int)
_IDS = TypeAdapter(List[int])
_NAME = TypeAdapter(str)

# No JSON body to read targets from.
_NO_BODY = object()
# A body that does not validate as the route's body model: no body target is read.
_INVALID_BODY = object()


def _is_json(request: Request) -> bool:
    """Whether FastAPI reads this request's body as JSON: a content type of application/json or +json."""
    content_type = request.headers.get("content-type")
    if not content_type:
        return False
    message = email.message.Message()
    message["content-type"] = content_type
    subtype = message.get_content_subtype()
    return message.get_content_maintype() == "application" and (subtype == "json" or subtype.endswith("+json"))


async def _json_body(request: Request) -> Any:
    """The body FastAPI parsed as JSON for the handler, or _NO_BODY.

    FastAPI reads the body before the dependencies run, and Starlette keeps
    what request.json() parsed, so this returns that copy. Anything that
    cannot be read or parsed, nesting too deep included, gives _NO_BODY and
    is left to the route.
    """
    if not _is_json(request):
        return _NO_BODY
    try:
        return await request.json()
    except Exception:
        return _NO_BODY


def _is_list_param(request: Request, name: str) -> bool:
    """Whether the matched route takes query parameter `name` as a list (every value) rather than a scalar (the last)."""
    route = request.scope.get("route")
    stack = [getattr(route, "dependant", None)]
    while stack:
        dep = stack.pop()
        if dep is None:
            continue
        for param in dep.query_params:
            if (param.alias or param.name) == name:
                annotation = param.field_info.annotation
                kinds = {typing.get_origin(a) or a for a in (annotation, *typing.get_args(annotation))}
                return bool(kinds & {list, set, tuple, frozenset})
        stack.extend(dep.dependencies)
    return False


async def _validated_body(request: Request, body: Any) -> Any:
    """The route's body as its handler receives it: {body parameter name: validated value}; _INVALID_BODY if it fails.

    Validated with the matched route's flattened body parameters and its
    embedding, through FastAPI's own request_body_to_args, so a single
    body model is returned under its parameter's name as an instance of
    that model.
    """
    from fastapi.dependencies.utils import get_flat_dependant, request_body_to_args
    from fastapi.routing import _should_embed_body_fields

    if body is _NO_BODY:
        return _INVALID_BODY
    route = request.scope.get("route")
    flat = getattr(route, "_flat_dependant", None)
    if flat is None:
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            return _INVALID_BODY
        flat = get_flat_dependant(dependant)
    if not flat.body_params:
        return _INVALID_BODY
    embed = getattr(route, "_embed_body_fields", None)
    if embed is None:
        embed = _should_embed_body_fields(flat.body_params)
    try:
        values, errors = await request_body_to_args(flat.body_params, body, embed)
    except Exception:
        return _INVALID_BODY
    return _INVALID_BODY if errors else values


def _body_value(values: dict, name: str) -> Any:
    """The handler's value for body target `name`: the attribute of a body model, or the body parameter so named."""
    for value in values.values():
        if isinstance(value, BaseModel) and name in type(value).model_fields:
            return getattr(value, name)
    return values.get(name)


def _values(target: space_routes.Target, request: Request, body: Any) -> list:
    """The values the handler receives for one target, validated as it takes them; none when absent or invalid.

    A path or query value is validated here as the parameter's type; a
    body value comes already validated by the route's body model
    (_validated_body).
    """
    if target.where == space_routes.BODY:
        if body is _INVALID_BODY:
            return []
        value = _body_value(body, target.name)
        if value is None:
            return []
        return list(value) if target.kind == space_routes.RECORD_LIST else [value]
    if target.where == space_routes.PATH:
        raw = [request.path_params[target.name]] if target.name in request.path_params else []
    else:
        raw = request.query_params.getlist(target.name)
        if raw and not _is_list_param(request, target.name):
            raw = raw[-1:]
    adapter = {space_routes.RECORD_LIST: _IDS, space_routes.PROJECT_NAME: _NAME,
               space_routes.CAPTURE: _NAME}.get(target.kind, _ONE_ID)
    values = []
    for value in raw:
        try:
            parsed = adapter.validate_python(value)
        except ValidationError:
            continue
        values.extend(parsed if target.kind == space_routes.RECORD_LIST else [parsed])
    return values


def _reached(user, entry: space_routes.Entry, request: Request) -> frozenset:
    """The spaces (True for testing, False for real) the caller reaches on this route."""
    if user.role == testing_space.TESTING_ROLE:
        return frozenset({True})
    if user.role != "admin":
        return frozenset({False})
    if entry.lists:
        return frozenset({request.query_params.get("space") == testing_space.TESTING_SPACE})
    if entry.admin_reads_testing:
        return frozenset({True, False})
    return frozenset({False})


def _refused_by_role(request: Request, user, key) -> bool:
    """Whether a RoleChecker on the matched route refuses `user`."""
    route = request.scope.get("route")
    dependant = getattr(route, "dependant", None)
    stack = [dependant] if dependant is not None else []
    while stack:
        dep = stack.pop()
        if isinstance(dep.call, RoleChecker) and not dep.call.admits(user, key):
            return True
        stack.extend(dep.dependencies)
    return False


def _caller(request: Request, db: Session, entry):
    """The caller with a valid session, or None; a 403 for the testing account where the table does not admit it."""
    user = session_user_or_none(request, db)
    if user is None:
        return None
    if user.role == testing_space.TESTING_ROLE and (entry is None or not entry.testing):
        raise HTTPException(status_code=403, detail="Operation not permitted")
    return user


def _check_targets(request: Request, db: Session, user, entry, key, body: Any) -> None:
    reached = _reached(user, entry, request)
    for target in entry.targets:
        kind = space_routes.RECORD if target.kind == space_routes.RECORD_LIST else target.kind
        for value in _values(target, request, body):
            if testing_space.spaces_of(db, kind, value,
                                       testing_caller=user.role == testing_space.TESTING_ROLE) <= reached:
                continue
            if _refused_by_role(request, user, key):
                raise HTTPException(status_code=403, detail="Operation not permitted")
            raise HTTPException(status_code=404, detail=testing_space.not_found_detail())


async def enforce_space(request: Request, db: Session = Depends(get_db_dependency)) -> None:
    key = space_routes.route_key(request)
    entry = space_routes.entry_for(key)
    if entry is not None and entry.public:
        return
    user = await run_in_threadpool(_caller, request, db, entry)
    if user is None or entry is None:
        return
    body = _INVALID_BODY
    if any(t.where == space_routes.BODY for t in entry.targets):
        body = await _validated_body(request, await _json_body(request))
    await run_in_threadpool(_check_targets, request, db, user, entry, key, body)
