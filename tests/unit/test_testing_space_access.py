"""The testing space's access rule (NEH-391): who reaches which route, and which material on it.

Three kinds of caller and two spaces. The testing account reaches only the
routes app.core.space_routes admits it to, and on them only testing
material. Operators and reviewers never reach testing material: lists and
counts leave it out, and by id it answers 404 `not_found`. An
administrator reaches real work, reads testing material by id on the
routes marked for it, and lists it only with `space=testing`.

A. Coverage: every route of the application has exactly one entry in the
   table, every entry has a route, every field named like material is
   declared as a target, every list route takes `space`, and every route
   that needs a session runs app.api.space.enforce_space.
B. The role lists of every route are the ones they were before the testing
   account was admitted anywhere, all checked by the one RoleChecker,
   which admits the testing account exactly where the table does.
C. Every route with a target, for each caller, refuses the other space's
   material with 404 `not_found` (403 where the caller's role is not
   admitted) and leaves the row as it was.
D. Every list and count route lists only the caller's space.
E. The testing account is refused with 403 on every route not admitted to it.
F. enforce_space leaves the public routes and the 401s alone.
G. The image file routes serve a testing image to the testing account and
   an administrator, and answer 404 to an operator and a reviewer.

    python -m pytest tests/unit/test_testing_space_access.py

Version: 0.4.0
"""

import ast
import re
from pathlib import Path

import pytest

from tests.unit.test_capture_manifest_export import tenant  # noqa: F401  (fixture)
from tests.unit.test_fisqua_outbox import fisqua  # noqa: F401  (fixture)
from tests.unit.test_unit_mode import _unit, link  # noqa: F401  (fixtures)

BACKEND = Path(__file__).resolve().parents[2]
# app.core.project_names.TESTING_PREFIX.
T = "captua-testing-"
# app.core.project_names.TESTING_PROJECT_PREFIX: a testing project's stored name begins with it.
TP = T + "-"

ROLE_LISTS_BEFORE = {
    ('POST', '/records/'): (('admin', 'operator'),),
    ('GET', '/records/'): (('admin', 'operator', 'reviewer'),),
    ('GET', '/records/count'): (('admin', 'operator', 'reviewer'),),
    ('GET', '/records/to-retake'): (('admin', 'operator', 'reviewer'),),
    ('GET', '/records/to-retake/count'): (('admin', 'operator', 'reviewer'),),
    ('GET', '/records/to-retake/summary'): (('admin', 'operator', 'reviewer'),),
    ('GET', '/records/{rec_id}'): (('admin', 'operator', 'reviewer'),),
    ('PATCH', '/records/{rec_id}'): (('admin', 'operator'),),
    ('DELETE', '/records/{rec_id}'): (('admin', 'operator'),),
    ('POST', '/records/{rec_id}/images'): (('admin', 'operator'),),
    ('GET', '/records/{rec_id}/images'): (('admin', 'operator', 'reviewer'),),
    ('GET', '/records/images/{img_id}'): (('admin', 'operator', 'reviewer'),),
    ('PATCH', '/records/images/{img_id}'): (('admin', 'operator'),),
    ('DELETE', '/records/images/{img_id}'): (('admin', 'operator'),),
    ('GET', '/records/images/{img_id}/file'): (('admin', 'operator', 'reviewer'),),
    ('GET', '/records/images/{img_id}/preview'): (('admin', 'operator', 'reviewer'),),
    ('GET', '/records/images/{img_id}/thumbnail'): (('admin', 'operator', 'reviewer'),),
    ('GET', '/records/{rec_id}/annotations'): (('admin', 'operator', 'reviewer'),),
    ('POST', '/records/{rec_id}/annotations'): (('admin', 'operator', 'reviewer'),),
    ('DELETE', '/records/annotations/{annotation_id}'): (('admin', 'operator', 'reviewer'),),
    ('PATCH', '/records/{rec_id}/status'): (('admin', 'operator', 'reviewer'),),
    ('POST', '/records/bulk-status'): (('admin', 'operator', 'reviewer'),),
    ('POST', '/records/{rec_id}/reject'): (('admin', 'reviewer'),),
    ('POST', '/records/{rec_id}/missing-page'): (('admin', 'operator'),),
    ('POST', '/records/{rec_id}/close-missing'): (('admin', 'operator'),),
    ('GET', '/records/{rec_id}/rejections'): (('admin', 'operator', 'reviewer'),),
    ('GET', '/cameras/devices'): (('admin', 'operator', 'reviewer'),),
    ('GET', '/cameras/setup'): (('admin', 'operator', 'reviewer'),),
    ('PUT', '/cameras/roles'): (('admin', 'operator'),),
    ('POST', '/cameras/rescan'): (('admin', 'operator'),),
    ('POST', '/cameras/side/{camera_index}'): (('admin',),),
    ('GET', '/cameras/capabilities'): (('admin', 'operator', 'reviewer'),),
    ('GET', '/cameras/preview/{camera_index}'): (('admin', 'operator', 'reviewer'),),
    ('DELETE', '/cameras/preview/tmp'): (('admin', 'operator'),),
    ('GET', '/cameras/focus/{camera_index}'): (('admin', 'operator', 'reviewer'),),
    ('POST', '/cameras/focus/{camera_index}'): (('admin', 'operator'),),
    ('POST', '/cameras/focus-lock/{camera_index}'): (('admin', 'operator'),),
    ('DELETE', '/cameras/focus-lock/{camera_index}'): (('admin', 'operator'),),
    ('POST', '/cameras/settings/{camera_index}'): (('admin', 'operator'),),
    ('POST', '/cameras/test-capture/{camera_index}'): (('admin', 'operator'),),
    ('POST', '/cameras/capture'): (('admin', 'operator'),),
    ('POST', '/cameras/capture/dual'): (('admin', 'operator'),),
    ('GET', '/cameras/capture-requests/{client_capture_id}'): (('admin', 'operator'),),
    ('POST', '/cameras/calibrate'): (('admin', 'operator'),),
    ('POST', '/cameras/calibrate/white-balance'): (('admin', 'operator'),),
    ('POST', '/cameras/calibrate/white-balance/manual'): (('admin', 'operator'),),
    ('GET', '/cameras/dslr/{camera_index}/settings'): (('admin', 'operator', 'reviewer'),),
    ('PUT', '/cameras/dslr/{camera_index}/settings'): (('admin', 'operator'),),
    ('PUT', '/cameras/{camera_index}/orientation'): (('admin', 'operator'),),
    ('POST', '/cameras/'): (('admin', 'operator'),),
    ('GET', '/cameras/'): (('admin', 'operator', 'reviewer'),),
    ('GET', '/cameras/{id}'): (('admin', 'operator', 'reviewer'),),
    ('PUT', '/cameras/settings/{id}'): (('admin', 'operator'),),
    ('DELETE', '/cameras/settings/{id}'): (('admin', 'operator'),),
    ('POST', '/projects/'): (('admin',),),
    ('GET', '/projects/'): (('admin', 'operator', 'reviewer'),),
    ('GET', '/projects/{project_id}'): (('admin', 'operator', 'reviewer'),),
    ('POST', '/projects/{project_id}/add_record/{rec_id}'): (('admin', 'operator'),),
    ('POST', '/projects/{project_id}/remove_record/{rec_id}'): (('admin', 'operator'),),
    ('PUT', '/projects/{project_id}'): (('admin', 'operator'),),
    ('POST', '/projects/{project_id}/move-collections'): (('admin', 'operator'),),
    ('DELETE', '/projects/{project_id}'): (('admin',),),
    ('POST', '/projects/{project_id}/initialize'): (('admin', 'operator'),),
    ('GET', '/projects/{project_id}/records'): (('admin', 'operator', 'reviewer'),),
    ('GET', '/projects/{project_id}/members'): (('admin', 'operator', 'reviewer'),),
    ('POST', '/projects/{project_id}/members'): (('admin',),),
    ('DELETE', '/projects/{project_id}/members/{user_id}'): (('admin',),),
    ('POST', '/collections/'): (('admin', 'operator'),),
    ('GET', '/collections/'): (('admin', 'operator', 'reviewer'),),
    ('GET', '/collections/count'): (('admin', 'operator', 'reviewer'),),
    ('GET', '/collections/{collection_id}'): (('admin', 'operator', 'reviewer'),),
    ('GET', '/collections/{collection_id}/offsite'): (('admin', 'operator', 'reviewer'),),
    ('POST', '/collections/{collection_id}/release-from-unit'): (('admin',),),
    ('POST', '/collections/{collection_id}/keep-on-unit'): (('admin',),),
    ('GET', '/collections/{collection_id}/offsite/free'): (('admin', 'operator'),),
    ('POST', '/collections/{collection_id}/offsite/free'): (('admin', 'operator'),),
    ('POST', '/collections/{collection_id}/offsite/restore'): (('admin',),),
    ('GET', '/collections/{collection_id}/offsite/jobs/{job_id}'): (('admin', 'operator'),),
    ('GET', '/collections/{collection_id}/capture-manifest'): (('admin', 'operator'),),
    ('GET', '/collections/{collection_id}/colour-card'): (('admin', 'operator'),),
    ('POST', '/collections/{collection_id}/colour-card/sets'): (('admin', 'operator'),),
    ('POST', '/collections/{collection_id}/colour-card/sets/{set_id}/shots'): (('admin', 'operator'),),
    ('POST', '/collections/{collection_id}/colour-card/sets/{set_id}/complete'): (('admin', 'operator'),),
    ('POST', '/collections/{collection_id}/colour-card/skip'): (('admin', 'operator'),),
    ('GET', '/collections/{collection_id}/colour-card/shots/{shot_id}/preview'): (('admin', 'operator', 'reviewer'),),
    ('GET', '/collections/{collection_id}/hierarchy'): (('admin', 'operator', 'reviewer'),),
    ('PATCH', '/collections/{collection_id}'): (('admin', 'operator'),),
    ('POST', '/collections/{collection_id}/move-records'): (('admin', 'operator'),),
    ('DELETE', '/collections/{collection_id}'): (('admin', 'operator'),),
    ('PATCH', '/collections/{collection_id}/records/reorder'): (('admin', 'operator'),),
    ('POST', '/collections/{collection_id}/records/missing-opening'): (('admin', 'operator'),),
    ('POST', '/collections/{collection_id}/images/renumber'): (('admin', 'operator'),),
    ('POST', '/collections/{collection_id}/promote'): (('admin', 'operator'),),
    ('POST', '/collections/{collection_id}/take-back'): (('admin', 'operator'),),
    ('GET', '/collections/{collection_id}/status-changes'): (('admin', 'operator', 'reviewer'),),
    ('POST', '/collections/{collection_id}/export'): (('admin', 'operator', 'reviewer'),),
    ('GET', '/collections/{collection_id}/export/status/{job_id}'): (('admin', 'operator', 'reviewer'),),
    ('GET', '/collections/{collection_id}/export/download'): (('admin', 'operator', 'reviewer'),),
    ('GET', '/collections/{source_id}/adopt-into/{target_id}'): (('admin',),),
    ('POST', '/collections/{source_id}/adopt-into/{target_id}'): (('admin',),),
    ('POST', '/collections/{collection_id}/usb-copy'): (('admin',),),
    ('GET', '/collections/{collection_id}/usb-copy/status/{job_id}'): (('admin',),),
    ('GET', '/auth/setup/status'): (),
    ('POST', '/auth/register'): (),
    ('POST', '/auth/login'): (),
    ('POST', '/auth/logout'): (),
    ('POST', '/auth/refresh'): (),
    ('POST', '/auth/password-reset'): (),
    ('GET', '/auth/users'): (('admin',),),
    ('GET', '/auth/users/{user_id}'): (('admin',),),
    ('PATCH', '/auth/users/{user_id}/role'): (('admin',),),
    ('PATCH', '/auth/users/{user_id}/active'): (('admin',),),
    ('PUT', '/auth/users/{user_id}/password'): (('admin',),),
    ('DELETE', '/auth/users/{user_id}'): (('admin',),),
    ('GET', '/users/me'): (),
    ('PUT', '/users/me/language'): (),
    ('GET', '/system/logs'): (('admin',),),
    ('GET', '/system/integrity'): (('admin',),),
    ('GET', '/system/temperature'): (('admin', 'operator', 'reviewer'),),
    ('GET', '/system/catalogue-backup'): (('admin', 'operator', 'reviewer'),),
    ('GET', '/system/unit'): (('admin', 'operator', 'reviewer'),),
    ('PUT', '/system/unit'): (('admin',),),
    ('PUT', '/system/unit/capture-key'): (('admin', 'operator'),),
    ('GET', '/system/testing'): (('admin',),),
    ('PUT', '/system/testing'): (('admin',),),
    ('PATCH', '/system/testing/account'): (('admin',),),
    ('POST', '/system/testing/clear'): (('admin',),),
    ('GET', '/system/testing/usb'): (('admin',),),
    ('GET', '/system/offsite/retired-keys'): (('admin',),),
    ('GET', '/system/offsite'): (('admin',),),
    ('POST', '/system/offsite'): (('admin',),),
    ('GET', '/system/offsite/template'): (('admin',),),
    ('POST', '/system/offsite/import'): (('admin',),),
    ('GET', '/system/adoption'): (('admin',),),
    ('POST', '/system/adoption/repair'): (('admin',),),
    ('GET', '/system/adoption/requests/{request_id}'): (('admin',),),
    ('GET', '/system/pairing'): (('admin',),),
    ('POST', '/system/pairing'): (('admin',),),
    ('POST', '/system/pairing/renew'): (('admin',),),
    ('DELETE', '/system/pairing'): (('admin',),),
    ('GET', '/system/storage'): (('admin', 'operator', 'reviewer'),),
    ('GET', '/system/storage/freeable'): (('admin', 'operator'),),
    ('GET', '/system/storage/devices'): (('admin',),),
    ('POST', '/system/storage/mount'): (('admin',),),
    ('DELETE', '/system/storage/mount'): (('admin',),),
    ('POST', '/system/storage/activate'): (('admin',),),
    ('DELETE', '/system/storage/activate'): (('admin',),),
    ('POST', '/system/power'): (),
    ('GET', '/system/camera-kind'): (('admin', 'operator', 'reviewer'),),
    ('PUT', '/system/camera-kind'): (('admin',),),
    ('DELETE', '/system/camera-kind'): (('admin',),),
    ('GET', '/activity/collections'): (('admin', 'operator', 'reviewer'),),
    ('GET', '/activity/daily'): (('admin', 'operator', 'reviewer'),),
    ('POST', '/imports/digitisation-list'): (('admin', 'operator'),),
    ('GET', '/imports/digitisation-lists'): (('admin', 'operator', 'reviewer'),),
    ('PUT', '/imports/digitisation-lists/{list_id}/items/{position}/done'): (('admin', 'reviewer'),),
    ('GET', '/imports/digitisation-list/{list_id}/report'): (('admin', 'operator', 'reviewer'),),
    ('GET', '/fisqua/descriptions'): (('admin',),),
    ('GET', '/network/status'): (('admin', 'operator', 'reviewer'),),
    ('GET', '/network/wifi'): (('admin', 'operator', 'reviewer'),),
    ('GET', '/network/wifi/join'): (('admin', 'operator', 'reviewer'),),
    ('POST', '/network/wifi/join'): (('admin', 'operator'),),
    ('DELETE', '/network/wifi/saved/{uuid}'): (('admin', 'operator'),),
    ('GET', '/network/tailscale'): (('admin', 'operator', 'reviewer'),),
    ('GET', '/health'): (),
}


# ---------------------------------------------------------------------------
# The application's routes
# ---------------------------------------------------------------------------

def _route_contexts():
    """(method, full path, route context) for every route and method of the application, HEAD left out."""
    from fastapi.routing import iter_route_contexts

    from app.main import app
    out = []
    for context in iter_route_contexts(app.routes):
        for method in sorted(set(context.methods or ()) - {"HEAD"}):
            out.append((method, context.path, context))
    return out


def _dependant(context):
    from fastapi.routing import APIRoute

    route = context.original_route
    if not isinstance(route, APIRoute):
        return None
    effective = getattr(context, "_route_context", None)
    return effective.dependant if effective is not None else route.dependant


def _walk(dependant):
    yield dependant
    for sub in dependant.dependencies:
        yield from _walk(sub)


def _role_lists(dependant):
    from app.api.auth import RoleChecker
    return tuple(tuple(d.call.allowed_roles) for d in _walk(dependant)
                 if isinstance(d.call, RoleChecker))


def _role_checkers(dependant):
    return [d.call for d in _walk(dependant) if type(d.call).__name__ == "RoleChecker"]


def _models_in(annotation):
    """The pydantic models an annotation holds, through Optional, unions, lists and dicts."""
    import typing
    found = []
    if getattr(annotation, "model_fields", None) is not None:
        found.append(annotation)
    for arg in typing.get_args(annotation):
        found.extend(_models_in(arg))
    return found


def _model_field_names(model, seen=None):
    """A model's field names and aliases, and those of every model nested in it, at any depth."""
    seen = set() if seen is None else seen
    if model in seen:
        return set()
    seen.add(model)
    names = set()
    for name, field in model.model_fields.items():
        names.add(name)
        if field.alias:
            names.add(field.alias)
        for nested in _models_in(field.annotation):
            names |= _model_field_names(nested, seen)
    return names


def _query_and_body_names(dependant):
    """Every query parameter and body field a route takes, by the name a client sends (aliases too).

    A body model's nested models, and models in its lists, are walked too.
    """
    names = set()
    for dep in _walk(dependant):
        for p in dep.query_params:
            names.add(("query", p.alias or p.name))
        for p in dep.body_params:
            models = _models_in(p.field_info.annotation)
            if models:
                for model in models:
                    names |= {("body", n) for n in _model_field_names(model)}
            else:
                names.add(("body", p.alias or p.name))
    return names


# ---------------------------------------------------------------------------
# A. Coverage
# ---------------------------------------------------------------------------

# Path parameters that name material, by name, and the kind each names.
PATH_MATERIAL = {
    "project_id": "project",
    "collection_id": "collection",
    "rec_id": "record",
    "img_id": "image",
    "annotation_id": "annotation",
    "source_id": "collection",
    "target_id": "collection",
}
# `id` names a camera settings row on these routes; each row belongs to one image.
CAMERA_SETTINGS_PATHS = {"/cameras/{id}", "/cameras/settings/{id}"}
# Path parameters that name no material, and why.
PATH_NOT_MATERIAL = {
    "camera_index": "a camera body's index on the unit",
    "job_id": "an export or offsite job; the handler answers 404 unless the job is the path's volume's",
    "set_id": "a volume's set of colour card shots; the handler answers 404 unless it is the path's volume's",
    "shot_id": "a colour card shot; the handler answers 404 unless it is of the path's volume",
    "user_id": "a user account",
    "list_id": "a digitisation list file",
    "position": "an item's position in a digitisation list file",
    "uuid": "a saved network connection",
    "client_capture_id": "a client's id for one capture request; the lookup answers only a stored capture whose "
                         "record is in the caller's space, and 404 for any other",
    "request_id": "a client's id for one adoption request; adoptions are an administrator's and real work only",
}
# Query and body fields that name material, and the kind each names.
FIELD_MATERIAL = {
    "project_id": "project",
    "target_project_id": "project",
    "collection_id": "collection",
    "parent_collection_id": "collection",
    "target_collection_id": "collection",
    "record_id": "record",
    "after_record_id": "record",
    "afterRecordId": "record",
    "record_image_id": "image",
    "record_ids": "record_list",
    "ordered_ids": "record_list",
    "project_name": "project_name",
    "capture_id": "capture",
}
# Query and body fields named like an id or a name that name no material, and why.
FIELD_NOT_MATERIAL = {
    "external_id": "a volume's reference code, set on the volume itself, not a reference to other material",
    "hardware_id": "a camera body's hardware id",
    "itemId": "a digitisation queue entry's id",
    "requestId": "a Fisqua request's id, stored on the placeholders",
    "serverName": "a Wi-Fi network's RADIUS server name",
    "unitName": "the unit's name sent to Fisqua at pairing",
    "bucketName": "the offsite bucket's name, written to the unit's offsite settings",
    "applicationKeyId": "the id of the offsite bucket's application key, written to the unit's offsite settings",
    "user_id": "a user account",
    "pair_id": "groups the images of one dual capture; nothing is looked up by it, on any route",
    "client_capture_id": "a client's id for one capture request; the capture routes answer a stored capture "
                         "only to a request of its kind, volume and space, and refuse any other naming nothing of it",
    "client_request_id": "a client's id for one adoption request; a repeat is answered from the request recorded "
                         "for it, which names the same route's volumes",
    "run_id": "the run id of an adoption's journal, compared with the journal on the unit; nothing is looked up by it",
    "expected_image_ids": "a retake's own record's current images, compared with the record named by record_id "
                          "(itself a target); nothing is looked up by them",
    "filename_stem": "a volume's own stem, set on the volume itself; a stem another volume holds is refused "
                     "by the model's uniqueness check, not looked up",
}
_NAMED_LIKE_MATERIAL = re.compile(r"(_id|_ids|Id|Ids|_name|Name|stem|Stem)$")


@pytest.mark.unit
def test_every_route_has_exactly_one_entry_and_every_entry_a_route():
    from app.core.space_routes import ROUTES
    keys = [(method, path) for method, path, _ in _route_contexts()]
    assert len(keys) == len(set(keys)), "a route is served twice under one method and path"
    assert set(keys) - set(ROUTES) == set(), "routes with no entry in app.core.space_routes"
    assert set(ROUTES) - set(keys) == set(), "entries for routes the application does not have"


@pytest.mark.unit
def test_every_path_parameter_naming_material_is_a_target():
    from app.core.space_routes import ROUTES
    for method, path, _ in _route_contexts():
        declared = {(t.where, t.name): t.kind for t in ROUTES[(method, path)].targets}
        for name in re.findall(r"\{(\w+)\}", path):
            if name == "id" and path in CAMERA_SETTINGS_PATHS:
                assert declared.get(("path", name)) == "camera_settings", (method, path, name)
            elif name in PATH_MATERIAL:
                assert declared.get(("path", name)) == PATH_MATERIAL[name], (method, path, name)
            else:
                assert name in PATH_NOT_MATERIAL, f"{method} {path}: path parameter {name} is not classified"


def _body_fields(dependant):
    """(field name, alias) of every body field a route takes: its body models' fields at any depth, or the parameter."""
    out = set()

    def walk(model, seen):
        if model in seen:
            return
        seen.add(model)
        for name, field in model.model_fields.items():
            out.add((name, field.alias))
            for nested in _models_in(field.annotation):
                walk(nested, seen)

    for dep in _walk(dependant):
        for p in dep.body_params:
            models = _models_in(p.field_info.annotation)
            if models:
                for model in models:
                    walk(model, set())
            else:
                out.add((p.name, p.alias))
    return out


@pytest.mark.unit
def test_every_query_and_body_field_naming_material_is_a_target():
    """A body field is declared by its field name, whatever alias the client sends it under."""
    from app.core.space_routes import ROUTES
    for method, path, context in _route_contexts():
        dependant = _dependant(context)
        if dependant is None:
            continue
        declared = {(t.where, t.name): t.kind for t in ROUTES[(method, path)].targets}
        queries = {name for where, name in _query_and_body_names(dependant) if where == "query"}
        for name in queries:
            if name in FIELD_MATERIAL:
                assert declared.get(("query", name)) == FIELD_MATERIAL[name], (method, path, "query", name)
            elif _NAMED_LIKE_MATERIAL.search(name):
                assert name in FIELD_NOT_MATERIAL, f"{method} {path}: query field {name} is not classified"
        fields = _body_fields(dependant)
        for name, alias in fields:
            kind = FIELD_MATERIAL.get(name) or FIELD_MATERIAL.get(alias)
            if kind:
                assert declared.get(("body", name)) == kind, (method, path, "body", name, alias)
            elif _NAMED_LIKE_MATERIAL.search(name) or (alias and _NAMED_LIKE_MATERIAL.search(alias)):
                assert name in FIELD_NOT_MATERIAL or alias in FIELD_NOT_MATERIAL, \
                    f"{method} {path}: body field {name} is not classified"
        for (where, name), kind in declared.items():
            if where == "query":
                assert name in queries, (method, path, where, name)
            elif where == "body":
                assert name in {n for n, _ in fields}, (method, path, where, name)


@pytest.mark.unit
def test_every_list_route_takes_space_and_every_session_route_runs_enforce_space():
    from app.api.space import enforce_space
    from app.core.space_routes import ROUTES
    for method, path, context in _route_contexts():
        entry = ROUTES[(method, path)]
        dependant = _dependant(context)
        if entry.public:
            continue
        assert dependant is not None, (method, path)
        calls = [d.call for d in _walk(dependant)]
        assert enforce_space in calls, f"{method} {path} does not run enforce_space"
        if entry.lists:
            assert ("query", "space") in _query_and_body_names(dependant), f"{method} {path} takes no space"


@pytest.mark.unit
def test_every_router_is_included_with_enforce_space():
    """A route added to any router later runs enforce_space too: it is on every include_router call."""
    source = (BACKEND / "app" / "main.py").read_text()
    calls = [n for n in ast.walk(ast.parse(source))
             if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "include_router"]
    assert calls
    import app.main as main
    from app.api.space import enforce_space
    for call in calls:
        deps = [k for k in call.keywords if k.arg == "dependencies"]
        assert deps, ast.unparse(call)
        value = eval(compile(ast.Expression(deps[0].value), "main.py", "eval"), vars(main))
        assert [d.dependency for d in value] == [enforce_space], ast.unparse(call)


# ---------------------------------------------------------------------------
# B. Role lists
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_role_lists_are_as_they_were():
    found = {}
    for method, path, context in _route_contexts():
        dependant = _dependant(context)
        if dependant is not None:
            found[(method, path)] = _role_lists(dependant)
    assert found == ROLE_LISTS_BEFORE


@pytest.mark.unit
def test_there_is_one_role_checker():
    from app.api.auth import RoleChecker
    defined = []
    for path in (BACKEND / "app").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ClassDef) and node.name == "RoleChecker":
                defined.append(str(path.relative_to(BACKEND)))
    assert defined == ["app/api/auth.py"]
    for method, path, context in _route_contexts():
        dependant = _dependant(context)
        if dependant is not None:
            for checker in _role_checkers(dependant):
                assert type(checker) is RoleChecker, (method, path)


@pytest.mark.unit
def test_the_role_checker_admits_the_testing_account_where_the_table_does():
    from app.core.space_routes import ROUTES
    from app.models.user import User
    tester = User(username="pruebas", role="testing", is_active=True)
    for method, path, context in _route_contexts():
        dependant = _dependant(context)
        if dependant is None:
            continue
        for checker in _role_checkers(dependant):
            assert checker.admits(tester, (method, path)) is ROUTES[(method, path)].testing, (method, path)
            for role in ("admin", "operator", "reviewer"):
                user = User(username=role, role=role, is_active=True)
                assert checker.admits(user, (method, path)) is (role in checker.allowed_roles), (method, path)


# ---------------------------------------------------------------------------
# The material and the callers
# ---------------------------------------------------------------------------

REAL, TESTING = False, True
CALLERS = ("testing", "operator", "reviewer", "admin")
JPEG = b"\xff\xd8jpeg-bytes-for-the-access-tests"


class World:
    """One project, two volumes, a record, an image, an annotation and a camera settings row in each space."""

    def __init__(self, db, root, names=None):
        from app.core.security import create_access_token
        from app.models.camera import CameraSettings
        from app.models.collection import Collection
        from app.models.project import Project
        from app.models.record import Record, RecordAnnotation, RecordImage
        from app.models.user import User

        self.db = db
        self.users, self.headers = {}, {}
        for role in CALLERS:
            user = User(username=f"u-{role}", hashed_password="x", role=role, is_active=True)
            db.add(user)
            db.commit()
            db.refresh(user)
            self.users[role] = user
            self.headers[role] = {"Authorization": f"Bearer {create_access_token(str(user.id))}"}
        self.P, self.V, self.V2, self.R, self.I, self.A, self.CS = {}, {}, {}, {}, {}, {}, {}
        # A testing project's name and a testing volume's stem begin with the prefix.
        self.stem = {}
        for space, tag in ((REAL, "real"), (TESTING, "test")):
            given = (names or {}).get(space, {})
            base = T if space else "xx-"
            stem = given.get("stem", f"{base}{tag}-1")
            self.stem[space] = stem
            project = Project(name=(TP if space else "") + given.get("project", f"Proyecto {tag}"), testing=space)
            db.add(project)
            db.commit()
            vols = []
            for n in (1, 2):
                vol = Collection(name=given.get("volume", f"Tomo {tag}") + f" {n}",
                                 filename_stem=stem if n == 1 else f"{base}{tag}-{n}",
                                 external_id=stem if n == 1 and names else None,
                                 project_id=project.id, **({"kept_on_unit": True} if names and n == 1 else {}))
                db.add(vol)
                db.commit()
                vols.append(vol)
            rec = Record(title=given.get("record", f"r {tag}"), collection_id=vols[0].id, status="captured",
                         capture_mode="single", sequence=1)
            db.add(rec)
            db.commit()
            captures = root / stem / "captures"
            captures.mkdir(parents=True, exist_ok=True)
            master = captures / f"{stem}-img_0001.jpg"
            master.write_bytes(JPEG + tag.encode())
            thumb = captures / f"{stem}-img_0001_thumb.jpg"
            thumb.write_bytes(JPEG + b"thumb" + tag.encode())
            img = RecordImage(record_id=rec.id, filename=master.name, file_path=str(master),
                              thumbnail_path=str(thumb), format="jpg", capture_id=f"cid-{tag}")
            db.add(img)
            db.commit()
            ann = RecordAnnotation(record_id=rec.id, error_types=["blur"], note="n", created_by="u-admin")
            cs = CameraSettings(record_image_id=img.id)
            db.add_all([ann, cs])
            db.commit()
            for store, row in ((self.P, project), (self.V, vols[0]), (self.V2, vols[1]), (self.R, rec),
                               (self.I, img), (self.A, ann), (self.CS, cs)):
                db.refresh(row)
                store[space] = row

    def value(self, kind, space, caller=None):
        """The value naming `space`'s material of `kind`; a project name as `caller` sends it.

        The testing account sends a project's display name; anyone else the
        name as stored, which for a testing project carries the prefix.
        """
        return {
            "project": lambda: self.P[space].id,
            "project_name": lambda: self.P[space].display_name if caller == "testing" else self.P[space].name,
            "collection": lambda: self.V[space].id,
            "record": lambda: self.R[space].id,
            "record_list": lambda: [self.R[space].id],
            "image": lambda: self.I[space].id,
            "annotation": lambda: self.A[space].id,
            "camera_settings": lambda: self.CS[space].id,
            "capture": lambda: self.I[space].capture_id,
        }[kind]()

    def snapshot(self):
        """Every row of the material's tables, read afresh."""
        from sqlalchemy import select
        from app.models.camera import CameraSettings
        from app.models.collection import Collection
        from app.models.project import Project
        from app.models.record import Record, RecordAnnotation, RecordImage
        self.db.expire_all()
        return {m.__tablename__: sorted(tuple(r) for r in self.db.execute(select(m.__table__)).all())
                for m in (Project, Collection, Record, RecordImage, RecordAnnotation, CameraSettings)}


@pytest.fixture
def world(client, db_session, override_projects_root):
    return World(db_session, override_projects_root)


def _own(caller):
    return TESTING if caller == "testing" else REAL


def _refused_space(caller, entry):
    """The space whose material the caller does not reach on this route, or None when it reaches both."""
    if caller == "testing":
        return REAL
    if caller == "admin" and entry.admin_reads_testing and not entry.lists:
        return None
    return TESTING


def _role_admitted(caller, key, entry):
    if caller == "testing":
        return entry.testing
    return all(caller in roles for roles in ROLE_LISTS_BEFORE[key])


_DUMMY_PATH = {"camera_index": "0", "job_id": "no-such-job", "list_id": "0", "position": "0", "uuid": "x",
               "set_id": "999999", "shot_id": "999999",
               "client_capture_id": "00000000-0000-4000-8000-000000000000",
               "request_id": "00000000-0000-4000-8000-000000000000"}


def _body_base(world, key, space, caller):
    """The fields a route's body model requires besides its targets, so the body validates as the handler's would."""
    return {
        ("POST", "/records/"): lambda: {"title": "t", "capture_mode": "single"},
        ("POST", "/records/bulk-status"): lambda: {"status": "in_review"},
        ("POST", "/cameras/capture"): lambda: {"project_name": world.value("project_name", space, caller)},
        ("POST", "/cameras/capture/dual"): lambda: {"project_name": world.value("project_name", space, caller)},
        ("POST", "/collections/"): lambda: {"name": "Nuevo", "filename_stem": (T if space else "xx-") + "zz-1"},
    }.get(key, dict)()


def _request(world, client, caller, method, path, entry, tested=None, tested_space=None, own=None):
    """Send `caller`'s request: every target its own space's material, `tested` the given space's."""
    own = _own(caller) if own is None else own
    url = path
    params, body = {}, {}
    for name in re.findall(r"\{(\w+)\}", path):
        target = next((t for t in entry.targets if t.where == "path" and t.name == name), None)
        if target is None:
            value = str(world.users["admin"].id) if name == "user_id" else _DUMMY_PATH[name]
        else:
            space = tested_space if target == tested else own
            value = str(world.value(target.kind, space, caller))
        url = url.replace("{" + name + "}", value)
    if tested is not None and tested.where == "query":
        params[tested.name] = world.value(tested.kind, tested_space, caller)
    if tested is not None and tested.where == "body":
        body = _body_base(world, (method, path), own, caller)
        body[tested.name] = world.value(tested.kind, tested_space, caller)
    kwargs = {"params": params, "headers": world.headers[caller]}
    if method != "GET":
        kwargs["json"] = body
    return client.request(method, url, **kwargs)


def _not_found(resp):
    if resp.status_code != 404:
        return False
    detail = resp.json().get("detail")
    return isinstance(detail, dict) and detail.get("code") == "not_found"


def _target_cases(refused_only=False):
    from app.core.space_routes import ROUTES
    cases = []
    for (method, path), entry in ROUTES.items():
        for target in entry.targets:
            for caller in CALLERS:
                if refused_only and _refused_space(caller, entry) is None:
                    continue
                cases.append(pytest.param(method, path, target, caller,
                                          id=f"{method} {path} {target.where}:{target.name} {caller}"))
    return cases


def _checkers_of(key):
    for method, path, context in _route_contexts():
        if (method, path) == key:
            return _role_checkers(_dependant(context))
    raise KeyError(key)


def _trap_role_checkers(key):
    """Every RoleChecker of the route replaced by a 418, so a request that passes enforce_space stops there."""
    from fastapi import HTTPException
    from app.main import app

    def trap():
        raise HTTPException(status_code=418, detail="passed enforce_space")

    for checker in _checkers_of(key):
        app.dependency_overrides[checker] = trap


# ---------------------------------------------------------------------------
# C. Material of the other space, by id, through every target
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("method,path,target,caller", _target_cases(refused_only=True))
def test_the_other_spaces_material_is_not_found(world, client, method, path, target, caller):
    """An administrator on a route that reads both spaces is in test_own_space_material_passes_the_rule."""
    from app.core.space_routes import ROUTES
    key = (method, path)
    entry = ROUTES[key]
    refused = _refused_space(caller, entry)
    before = world.snapshot()
    resp = _request(world, client, caller, method, path, entry, tested=target, tested_space=refused)
    if _role_admitted(caller, key, entry):
        assert _not_found(resp), (resp.status_code, resp.text)
    else:
        assert resp.status_code == 403, (resp.status_code, resp.text)
    assert world.snapshot() == before


@pytest.mark.unit
@pytest.mark.parametrize("method,path,target,caller", _target_cases())
def test_own_space_material_passes_the_rule(world, client, method, path, target, caller):
    """The caller's own space's material passes enforce_space: the route's RoleChecker, replaced by a 418, answers."""
    from app.core.space_routes import ROUTES
    key = (method, path)
    entry = ROUTES[key]
    spaces = [_own(caller)]
    if caller == "admin" and entry.admin_reads_testing and not entry.lists:
        spaces = [REAL, TESTING]
    _trap_role_checkers(key)
    for space in spaces:
        resp = _request(world, client, caller, method, path, entry, tested=target, tested_space=space, own=space)
        if caller == "testing" and not entry.testing:
            assert resp.status_code == 403, (space, resp.status_code, resp.text)
        else:
            assert resp.status_code == 418, (space, resp.status_code, resp.text)


@pytest.mark.unit
@pytest.mark.parametrize("caller", ["operator", "reviewer", "admin"])
def test_an_admins_space_parameter_decides_its_list_targets(world, client, caller):
    """On a list route an administrator's targets are read in the space `space` asks for."""
    real, test = world.V[REAL].id, world.V[TESTING].id
    h = world.headers[caller]
    assert _not_found(client.get("/records/", params={"collection_id": test}, headers=h))
    if caller == "admin":
        assert client.get("/records/", params={"collection_id": test, "space": "testing"}, headers=h).status_code == 200
        assert _not_found(client.get("/records/", params={"collection_id": real, "space": "testing"}, headers=h))
    else:
        assert client.get("/records/", params={"space": "testing"}, headers=h).status_code == 403


@pytest.mark.unit
def test_a_target_that_names_no_row_is_left_to_the_handler(world, client):
    resp = client.get("/records/999999", headers=world.headers["testing"])
    assert resp.status_code == 404 and resp.json()["detail"] == "Record not found"


@pytest.mark.unit
def test_a_body_list_with_one_record_of_the_other_space_is_refused_whole(world, client):
    before = world.snapshot()
    resp = client.post("/records/bulk-status", headers=world.headers["operator"],
                       json={"record_ids": [world.R[REAL].id, world.R[TESTING].id], "status": "in_review"})
    assert _not_found(resp), resp.text
    assert world.snapshot() == before


@pytest.mark.unit
def test_a_string_id_in_a_body_is_resolved_as_the_route_validates_it(world, client):
    """Pydantic takes "7" for an int; so does the rule."""
    resp = client.patch(f"/records/{world.R[TESTING].id}", headers=world.headers["testing"],
                        json={"collection_id": str(world.V[REAL].id)})
    assert _not_found(resp), resp.text


@pytest.mark.unit
def test_the_testing_account_moves_records_within_its_space_only(world, client):
    h = world.headers["testing"]
    resp = client.post(f"/collections/{world.V[TESTING].id}/move-records", headers=h,
                       params={"target_collection_id": world.V[REAL].id})
    assert _not_found(resp), resp.text
    resp = client.post(f"/collections/{world.V[TESTING].id}/move-records", headers=h,
                       params={"target_collection_id": world.V2[TESTING].id})
    assert resp.status_code == 200, resp.text
    world.db.expire_all()
    assert world.db.get(type(world.R[TESTING]), world.R[TESTING].id).collection_id == world.V2[TESTING].id


@pytest.mark.unit
def test_a_route_with_no_entry_refuses_the_testing_account_only(world, db_session):
    from fastapi import HTTPException
    from starlette.requests import Request
    import asyncio
    from app.api.space import enforce_space
    from app.main import app

    def request_as(caller):
        token = world.headers[caller]["Authorization"].encode()
        return Request({"type": "http", "method": "GET", "path": "/elsewhere", "query_string": b"",
                        "headers": [(b"authorization", token)], "route": object(), "app": app,
                        "path_params": {}})

    with pytest.raises(HTTPException) as refused:
        asyncio.run(enforce_space(request_as("testing"), db=db_session))
    assert refused.value.status_code == 403
    for caller in ("operator", "reviewer", "admin"):
        assert asyncio.run(enforce_space(request_as(caller), db=db_session)) is None


# ---------------------------------------------------------------------------
# D. Lists and counts
# ---------------------------------------------------------------------------

def _ids(resp):
    return {row["id"] for row in resp.json()}


def _count(resp):
    return {resp.json()["count"]}


LIST_CASES = {
    ("GET", "/records/"): (_ids, lambda w, s: {w.R[s].id}),
    ("GET", "/records/count"): (_count, lambda w, s: {1}),
    ("GET", "/records/to-retake"): (lambda r: {row["recordId"] for row in r.json()}, lambda w, s: {w.R[s].id}),
    ("GET", "/records/to-retake/count"): (_count, lambda w, s: {1}),
    ("GET", "/records/to-retake/summary"): (
        lambda r: {("volume", v["collectionId"]) for v in r.json()["volumes"]} | {("pending", r.json()["pendingRecords"])},
        lambda w, s: {("volume", w.V[s].id), ("pending", 1)}),
    ("GET", "/projects/"): (_ids, lambda w, s: {w.P[s].id}),
    ("GET", "/collections/"): (_ids, lambda w, s: {w.V[s].id, w.V2[s].id}),
    ("GET", "/collections/count"): (_count, lambda w, s: {2}),
    ("GET", "/activity/collections"): (lambda r: {row["collection_id"] for row in r.json()},
                                       lambda w, s: {w.V[s].id, w.V2[s].id}),
    ("GET", "/activity/daily"): (
        lambda r: {("images", sum(d["captured_images"] for d in r.json())),
                   ("reviewed", sum(d["reviewed_records"] for d in r.json()))},
        lambda w, s: {("images", 1), ("reviewed", 1)}),
    ("GET", "/imports/digitisation-lists"): (
        lambda r: {i["collectionId"] for entry in r.json() for i in entry["items"] if i["collectionId"]},
        lambda w, s: {w.V[s].id}),
    ("GET", "/cameras/"): (_ids, lambda w, s: {w.CS[s].id}),
}


@pytest.fixture
def listed(world, monkeypatch):
    """The world with a pending record and a review in each space, and a digitisation list naming both volumes."""
    import uuid as _uuid
    from app.api import imports
    from app.models.record import RecordStatusChange
    for space in (REAL, TESTING):
        world.R[space].status = "rejected"
        world.db.add(RecordStatusChange(record_id=world.R[space].id, collection_id=world.V[space].id,
                                        from_status="in_review", to_status="rejected", changed_by="u-admin"))
    world.db.commit()
    entry = {"listId": str(_uuid.uuid4()), "listName": "Lista", "projectId": None, "importedAt": None,
             "listFileMissing": False, "source": "file", "doneMarks": {},
             "items": [{"position": i, "sequence": i, "referenceCode": stem, "filenameStem": stem, "title": stem,
                        "notes": None, "action": None, "assignee": None, "withdrawn": False, "itemId": None,
                        "retakes": []}
                       for i, stem in enumerate((world.stem[REAL], world.stem[TESTING]), start=1)]}
    monkeypatch.setattr(imports, "stored_lists", lambda: [dict(entry, items=[dict(i) for i in entry["items"]])])
    return world


@pytest.mark.unit
def test_every_list_route_is_exercised():
    from app.core.space_routes import ROUTES
    assert set(LIST_CASES) == {key for key, entry in ROUTES.items() if entry.lists}


@pytest.mark.unit
@pytest.mark.parametrize("key", sorted(LIST_CASES), ids=lambda k: f"{k[0]} {k[1]}")
@pytest.mark.parametrize("caller", CALLERS)
def test_a_list_holds_only_the_callers_space(listed, client, key, caller):
    from app.core.space_routes import ROUTES
    method, path = key
    extract, expected = LIST_CASES[key]
    h = listed.headers[caller]
    resp = client.get(path, headers=h)
    if caller == "testing" and not ROUTES[key].testing:
        assert resp.status_code == 403, resp.text
        return
    assert resp.status_code == 200, resp.text
    assert extract(resp) == expected(listed, _own(caller))
    with_space = client.get(path, headers=h, params={"space": "testing"})
    if caller == "admin":
        assert with_space.status_code == 200, with_space.text
        assert extract(with_space) == expected(listed, TESTING)
    else:
        assert with_space.status_code == 403, with_space.text


@pytest.mark.unit
def test_a_projects_records_are_its_spaces_only(world, client):
    from app.models.record import Record
    # A record put in a testing project directly, as no route would put it, outside every volume.
    stray = Record(title="stray", project_id=world.P[TESTING].id, status="captured", capture_mode="single")
    world.db.add(stray)
    world.db.commit()
    for caller in ("testing", "admin"):
        resp = client.get(f"/projects/{world.P[TESTING].id}/records", headers=world.headers[caller])
        assert resp.status_code == 200 and _ids(resp) == {stray.id}, resp.text
    assert _not_found(client.get(f"/projects/{world.P[TESTING].id}/records", headers=world.headers["operator"]))


# ---------------------------------------------------------------------------
# E. The testing account on the routes not admitted to it
# ---------------------------------------------------------------------------

def _not_admitted():
    from app.core.space_routes import ROUTES
    return [pytest.param(m, p, id=f"{m} {p}") for (m, p), e in ROUTES.items() if not e.public and not e.testing]


@pytest.mark.unit
@pytest.mark.parametrize("method,path", _not_admitted())
def test_the_testing_account_is_refused_where_not_admitted(world, client, method, path):
    from app.core.space_routes import ROUTES
    before = world.snapshot()
    resp = _request(world, client, "testing", method, path, ROUTES[(method, path)])
    assert resp.status_code == 403, resp.text
    assert world.snapshot() == before


@pytest.mark.unit
def test_the_testing_account_reads_its_own_profile_and_the_unit(world, client):
    h = world.headers["testing"]
    assert client.get("/users/me", headers=h).json()["role"] == "testing"
    assert client.get("/system/unit", headers=h).status_code == 200


# ---------------------------------------------------------------------------
# F. Public routes and 401s
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_public_routes_and_missing_sessions_are_left_alone(world, client):
    assert client.get("/health").status_code == 200
    assert client.get("/auth/setup/status").json() == {"needs_setup": False, "local": False,
                                                       "default_language": "es"}
    assert client.post("/auth/login", json={"username": "u-operator", "password": "wrong"}).status_code == 401
    assert client.post("/auth/refresh").status_code == 401
    assert client.post("/auth/logout", headers={"X-Requested-With": "x"}).status_code == 204
    for path in ("/records/", f"/records/{world.R[TESTING].id}", f"/records/{world.R[REAL].id}"):
        assert client.get(path).status_code == 401, path
        assert client.get(path, headers={"Authorization": "Bearer not-a-token"}).status_code == 401, path
    # A cookie session's write without the CSRF header keeps the route's own 403, whatever it names.
    client.cookies.set("dtk_session", world.headers["operator"]["Authorization"].split()[1])
    try:
        resp = client.delete(f"/records/{world.R[TESTING].id}")
        assert resp.status_code == 403 and "X-Requested-With" in resp.text, resp.text
    finally:
        client.cookies.clear()


@pytest.mark.unit
def test_the_testing_account_signs_in_through_the_public_route(world, client):
    from app.core.security import hash_password
    world.users["testing"].hashed_password = hash_password("pw-91")
    world.db.commit()
    assert client.post("/auth/login", json={"username": "u-testing", "password": "pw-91"}).status_code == 200


# ---------------------------------------------------------------------------
# G. Image files
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("which", ["file", "preview", "thumbnail"])
def test_the_image_files_follow_the_rule(world, client, which):
    test_img, real_img = world.I[TESTING], world.I[REAL]
    for caller in ("testing", "admin"):
        resp = client.get(f"/records/images/{test_img.id}/{which}", headers=world.headers[caller])
        assert resp.status_code == 200 and resp.content.startswith(JPEG), (caller, resp.status_code)
        assert b"test" in resp.content
    for caller in ("operator", "reviewer"):
        assert _not_found(client.get(f"/records/images/{test_img.id}/{which}", headers=world.headers[caller]))
        resp = client.get(f"/records/images/{real_img.id}/{which}", headers=world.headers[caller])
        assert resp.status_code == 200 and b"real" in resp.content
    assert _not_found(client.get(f"/records/images/{real_img.id}/{which}", headers=world.headers["testing"]))


# ---------------------------------------------------------------------------
# H. Canaries: no response names the other space's material
# ---------------------------------------------------------------------------

CANARY = {
    REAL: {"project": "CanRealProyecto", "volume": "CanRealTomo", "stem": "xx-canreal-1",
           "record": "CanRealRegistro", "file": "canreal-img_0001"},
    TESTING: {"project": "CanTestProyecto", "volume": "CanTestTomo", "stem": T + "cantest-1",
              "record": "CanTestRegistro", "file": "cantest-img_0001",
              # Made by the testing account through POST /projects/, POST /collections/ and a dual capture.
              "routed_project": "CanTestRutaProyecto", "routed_volume": "CanTestRutaTomo",
              "routed_record": "CanTestRutaRegistro"},
}
# Routes an administrator calls that report on the whole unit, both spaces, by design.
WHOLE_UNIT_DIAGNOSTICS = {
    ("GET", "/system/integrity"): "the integrity report covers every file on the card",
    ("GET", "/system/logs"): "the unit's log names whatever any user acted on",
    ("GET", "/system/testing"): "the testing space's own settings and counts",
}


class CanaryWorld(World):
    """World, with distinctive names on each space's material and everything a listing shows: a rejection,
    a review, a kept real volume, the default project, a digitisation list naming both volumes."""

    def __init__(self, db, root, monkeypatch, client=None):
        super().__init__(db, root, names=CANARY)
        if client is not None:
            self._create_through_the_routes(client, monkeypatch)
        import uuid as _uuid
        from app.api import imports
        from app.models.record import RecordRejection, RecordStatusChange
        from app.models.unit_settings import UnitSettings
        for space in (REAL, TESTING):
            names = CANARY[space]
            self.R[space].status = "rejected"
            db.add(RecordRejection(record_id=self.R[space].id, predefined_reason="blur",
                                   comment=names["record"], rejected_by="u-admin"))
            db.add(RecordStatusChange(record_id=self.R[space].id, collection_id=self.V[space].id,
                                      from_status="in_review", to_status="rejected", changed_by="u-admin"))
            db.commit()
        row = db.get(UnitSettings, 1)
        if row is None:
            row = UnitSettings(id=1, mode="offline", projects_enabled=False)
            db.add(row)
        row.default_project_id = self.P[REAL].id
        db.commit()
        entry = {"listId": str(_uuid.uuid4()), "listName": "Lista", "projectId": None, "importedAt": None,
                 "listFileMissing": False, "source": "file", "doneMarks": {},
                 "items": [{"position": i, "sequence": i, "referenceCode": stem, "filenameStem": stem,
                            "title": stem, "notes": None, "action": None, "assignee": None, "withdrawn": False,
                            "itemId": None, "retakes": []}
                           for i, stem in enumerate((CANARY[REAL]["stem"],), start=1)]}
        # A list names stems in its own file, so a list naming a testing stem would show that text to
        # whoever reads the list; the join to the testing volume is checked in group D.
        monkeypatch.setattr(imports, "stored_lists",
                            lambda: [dict(entry, items=[dict(i) for i in entry["items"]])])


    def _create_through_the_routes(self, client, monkeypatch):
        """A testing project, volume and dual capture made by the testing account, the camera stood in for."""
        import capture.project_manager as project_manager_module
        import capture.service as capture_service
        from tests.unit.test_capture_contract_naming import _FakeBackend
        backend = _FakeBackend()
        monkeypatch.setattr(capture_service, "get_backend", lambda: backend)
        monkeypatch.setattr(capture_service, "is_camera_connected", lambda idx: True)
        monkeypatch.setattr(project_manager_module, "default_camera_config_from_registry",
                            lambda camera_index, resolution="high", registry=None: (
                                {"camera_index": camera_index, "rotate_deg": 0}, "hw"))
        h, names = self.headers["testing"], CANARY[TESTING]
        resp = client.post("/projects/", headers=h, json={"name": names["routed_project"]})
        assert resp.status_code == 200, resp.text
        self.routed_project_id = resp.json()["id"]
        resp = client.post("/collections/", headers=h,
                           json={"name": names["routed_volume"], "project_id": self.routed_project_id})
        assert resp.status_code == 201, resp.text
        self.routed_volume_id = resp.json()["id"]
        resp = client.post("/cameras/capture/dual", headers=h,
                           json={"project_name": names["routed_project"], "collection_id": self.routed_volume_id,
                                 "record_title": names["routed_record"]})
        assert resp.status_code == 200 and resp.json()["success"], resp.text
        self.routed_record_id = resp.json()["record_id"]


@pytest.fixture
def canaries(client, db_session, override_projects_root, monkeypatch):
    return CanaryWorld(db_session, override_projects_root, monkeypatch, client=client)


def _get_routes_for(caller):
    """The GET routes `caller` is admitted to, from the table and the role lists; for an administrator,
    those that name no material in their path, less the whole-unit diagnostics."""
    from app.core.space_routes import ROUTES
    out = []
    for (method, path), entry in sorted(ROUTES.items()):
        if method != "GET" or path in ("/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"):
            continue
        if caller == "testing":
            admitted = entry.public or entry.testing
        else:
            admitted = entry.public or all(caller in roles for roles in ROLE_LISTS_BEFORE[(method, path)])
        if caller == "admin":
            admitted = admitted and not any(t.where == "path" for t in entry.targets)
            admitted = admitted and (method, path) not in WHOLE_UNIT_DIAGNOSTICS
        if admitted:
            out.append((method, path, entry))
    return out


def _leaks(resp, space):
    found = []
    for what, canary in CANARY[space].items():
        if canary.encode() in resp.content:
            found.append(f"{what} {canary!r} in the body")
        for header, value in resp.headers.items():
            if canary in value:
                found.append(f"{what} {canary!r} in header {header}")
    return found


def _prefixed_names(resp):
    """Every `name` in a JSON body that carries the testing prefix, and the stored testing project name anywhere."""
    found = []
    if (TP + CANARY[TESTING]["project"]).encode() in resp.content:
        found.append("the stored testing project name in the body")
    try:
        body = resp.json()
    except ValueError:
        return found
    stack = [body]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if isinstance(node.get("name"), str) and node["name"].startswith(T):
                found.append(f"name {node['name']!r}")
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return found


@pytest.mark.unit
@pytest.mark.parametrize("caller", CALLERS)
def test_no_response_names_the_other_spaces_material(canaries, client, caller):
    """A route that fails on this platform (the camera routes need a Raspberry Pi) answers 500; its body is read too.

    No body, for any caller, carries the testing prefix in a project's name.
    """
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app, raise_server_exceptions=False)
    hidden = REAL if caller == "testing" else TESTING
    routes = _get_routes_for(caller)
    assert routes
    leaks = {}
    for method, path, entry in routes:
        resp = _request(canaries, client, caller, method, path, entry)
        found = _leaks(resp, hidden) + _prefixed_names(resp)
        if found:
            leaks[f"{method} {path} ({resp.status_code})"] = found
    if caller == "admin":
        for path, params in (("/projects/", {"space": "testing"}), (f"/projects/{canaries.P[TESTING].id}", {})):
            resp = client.get(path, headers=canaries.headers["admin"], params=params)
            assert resp.status_code == 200, resp.text
            if _prefixed_names(resp):
                leaks[f"GET {path} (admin)"] = _prefixed_names(resp)
    assert leaks == {}


@pytest.mark.unit
def test_the_testing_account_reads_and_sends_its_projects_display_name(canaries, client):
    h = canaries.headers["testing"]
    tp = canaries.P[TESTING]
    assert tp.name == TP + CANARY[TESTING]["project"]
    assert sorted(p["name"] for p in client.get("/projects/", headers=h).json()) == \
        sorted([CANARY[TESTING]["project"], CANARY[TESTING]["routed_project"]])
    assert client.get(f"/projects/{tp.id}", headers=h).json()["name"] == CANARY[TESTING]["project"]
    resp = client.put(f"/projects/{tp.id}", headers=h, json={"name": "CanTestOtro", "description": "d"})
    assert resp.status_code == 200 and resp.json()["name"] == "CanTestOtro", resp.text
    canaries.db.expire_all()
    assert canaries.db.get(type(tp), tp.id).name == TP + "CanTestOtro"
    admin = client.get(f"/projects/{tp.id}", headers=canaries.headers["admin"])
    assert admin.json()["name"] == "CanTestOtro"


@pytest.mark.unit
def test_the_canaries_are_seen_in_their_own_space(canaries, client):
    """The canary test can fail: each space's names do appear to the callers that reach it."""
    seen = client.get("/records/", headers=canaries.headers["testing"])
    assert CANARY[TESTING]["record"].encode() in seen.content
    assert CANARY[TESTING]["routed_record"].encode() in seen.content
    seen = client.get("/collections/", headers=canaries.headers["admin"], params={"space": "testing"})
    assert CANARY[TESTING]["routed_volume"].encode() in seen.content
    seen = client.get("/system/unit", headers=canaries.headers["operator"])
    assert CANARY[REAL]["volume"].encode() in seen.content


@pytest.mark.unit
def test_the_testing_account_reads_the_units_mode_and_projects_setting_only(canaries, client):
    body = client.get("/system/unit", headers=canaries.headers["testing"]).json()
    full = client.get("/system/unit", headers=canaries.headers["operator"]).json()
    assert set(body) == set(full)
    assert {k: body[k] for k in ("mode", "projectsEnabled")} == {k: full[k] for k in ("mode", "projectsEnabled")}
    # The testing account always works in its own projects.
    assert body["projectsShown"] is True and full["projectsShown"] is False
    assert body["defaultProjectId"] is None and body["keptVolumes"] == [] and body["prerequisites"] == []
    assert full["defaultProjectId"] == canaries.P[REAL].id and full["keptVolumes"]


# ---------------------------------------------------------------------------
# I. A project's directory is the project's alone, in both spaces
# ---------------------------------------------------------------------------

def _tree(root, name, files=("images/a.jpg",)):
    from app.core.paths import project_dir_name
    base = root / project_dir_name(name)
    for f in files:
        (base / f).parent.mkdir(parents=True, exist_ok=True)
        (base / f).write_bytes(b"real bytes " + name.encode("utf-8"))
    return base


def _project_row(db, name, testing=False):
    """A project whose display name is `name`; a testing one is stored with the prefix."""
    from app.models.project import Project
    p = Project(name=(TP + name) if testing else name, testing=testing)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _taken(resp):
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "project_directory_taken", detail
    return detail


@pytest.mark.unit
def test_a_testing_project_renamed_as_a_real_project_takes_a_directory_of_its_own(
        world, client, override_projects_root):
    """Renamed to "Réal" beside real "Real", then again: the real tree and its paths never move."""
    from app.core.paths import project_dir_name
    from app.models.record import RecordImage
    real = _project_row(world.db, "Real")
    base = _tree(override_projects_root, "Real")
    real_image = RecordImage(record_id=world.R[REAL].id, filename="a.jpg", file_path=str(base / "images/a.jpg"),
                             format="jpg")
    world.db.add(real_image)
    world.db.commit()
    tp = world.P[TESTING]
    _tree(override_projects_root, tp.name)
    h = world.headers["testing"]
    resp = client.put(f"/projects/{tp.id}", headers=h, json={"name": "Réal"})
    assert resp.status_code == 200 and resp.json()["name"] == "Réal", resp.text
    world.db.expire_all()
    assert world.db.get(type(tp), tp.id).name == TP + "Réal"
    assert (override_projects_root / project_dir_name(TP + "Réal") / "images/a.jpg").exists()
    assert client.put(f"/projects/{tp.id}", headers=h, json={"name": "Otro nombre"}).status_code == 200
    world.db.expire_all()
    assert world.db.get(type(real), real.id).name == "Real"
    assert (base / "images/a.jpg").read_bytes() == b"real bytes Real"
    assert world.db.get(RecordImage, real_image.id).file_path == str(base / "images/a.jpg")


@pytest.mark.unit
def test_a_rename_is_refused_when_the_destination_directory_exists_on_disk(world, client, override_projects_root):
    orphan = _tree(override_projects_root, TP + "huerfano")
    tp = _project_row(world.db, "Sin carpeta", testing=True)
    _taken(client.put(f"/projects/{tp.id}", headers=world.headers["testing"], json={"name": "Huerfano"}))
    assert (orphan / "images/a.jpg").exists()


@pytest.mark.unit
def test_creating_a_project_whose_directory_is_taken_is_refused(world, client):
    _project_row(world.db, "Default")
    _taken(client.post("/projects/", headers=world.headers["admin"], json={"name": "default"}))
    _taken(client.post("/projects/", headers=world.headers["admin"], json={"name": "Défault"}))


@pytest.mark.unit
def test_deleting_one_of_two_projects_sharing_a_directory_leaves_the_tree(world, client, override_projects_root):
    """Two real projects that already share a directory, from before the rule: the delete keeps the tree."""
    from app.models.system_log import SystemLog
    first = _project_row(world.db, "Default")
    second = _project_row(world.db, "default")
    base = _tree(override_projects_root, "Default", files=("images/a.jpg", "metadata/manifest.jsonl"))
    assert client.delete(f"/projects/{second.id}", headers=world.headers["admin"]).status_code == 200
    assert (base / "images/a.jpg").exists() and (base / "metadata/manifest.jsonl").exists()
    world.db.expire_all()
    assert world.db.get(type(first), first.id) is not None
    logged = world.db.query(SystemLog).filter(SystemLog.action == "project_deleted").one()
    assert "left" in (logged.detail or "")


@pytest.mark.unit
def test_the_list_import_does_not_create_a_project_whose_directory_is_taken(world, client):
    from app.core import list_import
    from app.core.project_names import ProjectDirectoryTaken
    _project_row(world.db, "Real")
    with pytest.raises(ProjectDirectoryTaken):
        list_import._project_for(world.db, "Réal", "u-admin", True)


# ---------------------------------------------------------------------------
# J. Capture ids
# ---------------------------------------------------------------------------

def _manifest(root, stem, entries):
    import json as _json
    path = root / stem / "metadata" / "manifest.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(_json.dumps(e) + "\n" for e in entries))
    return path


def _export_bag_manifest(client, world, monkeypatch, tmp_path, volume):
    """Export `volume` as an administrator through the route; the bag's data/metadata/manifest.jsonl."""
    import time
    import zipfile
    from app.core import config
    exports = tmp_path / "exp"
    exports.mkdir(exist_ok=True)
    monkeypatch.setattr(config.settings, "EXPORTS_ROOT", str(exports))
    h = world.headers["admin"]
    resp = client.post(f"/collections/{volume.id}/export", headers=h)
    assert resp.status_code == 202, resp.text
    job = resp.json()["job_id"]
    deadline = time.time() + 15
    while True:
        st = client.get(f"/collections/{volume.id}/export/status/{job}", headers=h).json()
        if st["state"] in ("done", "failed") or time.time() > deadline:
            break
        time.sleep(0.05)
    assert st["state"] == "done", st
    with zipfile.ZipFile(exports / st["zip_filename"]) as zf:
        return next(zf.read(n) for n in zf.namelist() if n.endswith("data/metadata/manifest.jsonl"))


def _real_image_for_export(world, override_projects_root, capture_id):
    """The real image given `capture_id` and its record approved; the line a manifest would hold for it."""
    import hashlib
    img = world.I[REAL]
    data = (override_projects_root / "xx-real-1" / "captures" / img.filename).read_bytes()
    img.capture_id = capture_id
    img.role = "single"
    world.R[REAL].status = "approved"
    world.db.commit()
    return {"capture_id": capture_id, "files": [{"role": "single", "relative_path": f"captures/{img.filename}",
                                                 "sha256": hashlib.sha256(data).hexdigest()}]}


@pytest.mark.unit
def test_an_upload_naming_a_testing_images_capture_is_not_found(world, client):
    world.I[TESTING].capture_id = "cid-testing-image"
    world.db.commit()
    before = world.snapshot()
    resp = client.post(f"/records/{world.R[REAL].id}/images", headers=world.headers["operator"],
                       params={"capture_id": "cid-testing-image"},
                       files={"file": ("x.jpg", b"\xff\xd8x", "image/jpeg")})
    assert _not_found(resp), resp.text
    assert world.snapshot() == before


@pytest.mark.unit
def test_a_capture_id_only_a_testing_manifest_line_carries_passes_but_never_reaches_a_real_bag(
        world, client, override_projects_root, monkeypatch, tmp_path):
    """The rule reads the database only; the export's manifest filter keeps the testing line out."""
    _manifest(override_projects_root, world.stem[TESTING], [{"capture_id": "cid-testing-line", "origin": "TESTING-LINE",
                                                     "files": []}])
    _trap_role_checkers(("POST", "/records/{rec_id}/images"))
    for cid in ("cid-nobody-has", "cid-testing-line"):
        resp = client.post(f"/records/{world.R[REAL].id}/images", headers=world.headers["operator"],
                           params={"capture_id": cid}, files={"file": ("x.jpg", b"\xff\xd8x", "image/jpeg")})
        assert resp.status_code == 418, (cid, resp.text)
    from app.main import app
    app.dependency_overrides = {k: v for k, v in app.dependency_overrides.items()
                                if type(k).__name__ != "RoleChecker"}
    # As the upload would have stored it on the real image.
    line = _real_image_for_export(world, override_projects_root, "cid-testing-line")
    _manifest(override_projects_root, "xx-real-1", [dict(line, origin="REAL-LINE")])
    _manifest(override_projects_root, world.stem[TESTING], [dict(line, origin="TESTING-LINE")])
    manifest = _export_bag_manifest(client, world, monkeypatch, tmp_path, world.V[REAL])
    assert b"REAL-LINE" in manifest and b"TESTING-LINE" not in manifest


@pytest.mark.unit
def test_the_upload_route_opens_no_manifest(world, client, monkeypatch):
    from app.core import integrity

    def refuse(*a, **k):
        raise AssertionError("a manifest was read on the request")

    monkeypatch.setattr(integrity, "_manifests", refuse)
    monkeypatch.setattr(integrity, "read_manifest_lines", refuse)
    monkeypatch.setattr(integrity, "build_fixity_index", refuse)
    _trap_role_checkers(("POST", "/records/{rec_id}/images"))
    resp = client.post(f"/records/{world.R[REAL].id}/images", headers=world.headers["operator"],
                       params={"capture_id": "cid-anything"}, files={"file": ("x.jpg", b"\xff\xd8x", "image/jpeg")})
    assert resp.status_code == 418, resp.text


@pytest.mark.unit
def test_a_real_export_carries_no_testing_capture_line(client, db_session, override_projects_root,
                                                        monkeypatch, tmp_path):
    world = World(db_session, override_projects_root)
    line = _real_image_for_export(world, override_projects_root, "cid-shared")
    _manifest(override_projects_root, "xx-real-1", [dict(line, origin="REAL-LINE")])
    _manifest(override_projects_root, world.stem[TESTING], [dict(line, origin="TESTING-LINE")])
    manifest = _export_bag_manifest(client, world, monkeypatch, tmp_path, world.V[REAL])
    assert b"REAL-LINE" in manifest and b"TESTING-LINE" not in manifest


@pytest.mark.unit
def test_a_real_image_is_never_verified_against_a_testing_manifest(world, override_projects_root):
    """Its capture id's only line is in a testing manifest: the fixity checks made for it do not read that line."""
    from app.core import config
    from app.core.integrity import build_fixity_index, verify_images_against_manifest
    from app.core.testing_space import manifest_filter
    from app.models.record import RecordImage
    line = _real_image_for_export(world, override_projects_root, "cid-only-testing")
    _manifest(override_projects_root, world.stem[TESTING], [line])
    img = world.db.get(RecordImage, world.I[REAL].id)
    projects_dir = config.settings.projects_dir.resolve()
    path = (override_projects_root / "xx-real-1" / "captures" / img.filename).resolve()
    real_index = build_fixity_index(projects_dir, include=manifest_filter(world.db, False, projects_dir))
    assert real_index.expected("cid-only-testing", img.id, path) is None
    assert "cid-only-testing" not in real_index.captured
    # A wrong checksum on the testing line is not reported either: the line is not read for this image.
    wrong = dict(line, files=[dict(line["files"][0], sha256="0" * 64)])
    _manifest(override_projects_root, world.stem[TESTING], [wrong])
    assert verify_images_against_manifest([img]) == []
    # The same line in the real volume's own manifest is read: the check can fail.
    _manifest(override_projects_root, "xx-real-1", [wrong])
    assert [m["reason"] for m in verify_images_against_manifest([img])] == ["mismatch"]


# ---------------------------------------------------------------------------
# K. A scalar query parameter sent twice; the body read only where it names material
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_scalar_query_parameter_is_checked_in_the_value_the_handler_uses(world, client):
    h = world.headers["operator"]
    last_testing = [("collection_id", world.V[REAL].id), ("collection_id", world.V[TESTING].id)]
    assert _not_found(client.get("/records/", headers=h, params=last_testing))
    earlier_testing = [("collection_id", world.V[TESTING].id), ("collection_id", world.V[REAL].id)]
    resp = client.get("/records/", headers=h, params=earlier_testing)
    assert resp.status_code == 200 and _ids(resp) == {world.R[REAL].id}, resp.text


@pytest.fixture
def body_reads(monkeypatch):
    """Counts of Request.json calls and of the JSON parses Starlette makes."""
    import starlette.requests as sr
    counts = {"json": 0, "parse": 0}
    real_json = sr.Request.json

    async def counting_json(self):
        counts["json"] += 1
        if not hasattr(self, "_json"):
            # Starlette parses the body on the first call and keeps the result as _json.
            counts["parse"] += 1
        return await real_json(self)

    monkeypatch.setattr(sr.Request, "json", counting_json)
    return counts


@pytest.mark.unit
def test_the_body_is_not_read_where_no_body_names_material(world, client, monkeypatch):
    import starlette.requests as sr

    async def refuse(self):
        raise AssertionError("the body was read")

    monkeypatch.setattr(sr.Request, "body", refuse)
    monkeypatch.setattr(sr.Request, "json", refuse)
    deep = "[" * 200000 + "]" * 200000
    resp = client.request("GET", f"/records/{world.R[TESTING].id}", headers={
        **world.headers["testing"], "Content-Type": "application/json"}, content=deep)
    assert resp.status_code == 200, resp.text
    resp = client.request("GET", "/health", headers={"Content-Type": "application/json"}, content=deep)
    assert resp.status_code == 200


@pytest.mark.unit
def test_the_body_is_parsed_once_and_only_with_a_session(world, client, body_reads):
    url = f"/records/{world.R[TESTING].id}"
    client.patch(url, json={"title": "t"})
    assert body_reads == {"json": 1, "parse": 1}, "FastAPI's own read only, without a session"
    body_reads.update(json=0, parse=0)
    resp = client.patch(url, headers=world.headers["testing"], json={"title": "nuevo"})
    assert resp.status_code == 200, resp.text
    assert body_reads["parse"] == 1


@pytest.mark.unit
def test_a_deeply_nested_body_answers_what_the_route_answers(world, client):
    deep = "[" * 200000 + "]" * 200000
    for caller in ("testing", "operator"):
        resp = client.request("PATCH", f"/records/{world.R[_own(caller)].id}", content=deep,
                              headers={**world.headers[caller], "Content-Type": "application/json"})
        assert resp.status_code in (400, 422) and resp.status_code != 500, resp.text


# ---------------------------------------------------------------------------
# L. End to end through the real handlers
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_testing_account_works_its_own_material_end_to_end(world, client, override_projects_root):
    from app.models.collection import Collection
    from app.models.project import Project
    from app.models.record import Record, RecordImage
    db, h = world.db, world.headers["testing"]
    rec, img, vol = world.R[TESTING], world.I[TESTING], world.V[TESTING]

    assert client.patch(f"/records/{rec.id}", headers=h, json={"title": "Retitulado"}).status_code == 200
    assert client.patch(f"/records/images/{img.id}", headers=h, json={"sequence": 4}).status_code == 200
    assert client.patch(f"/collections/{vol.id}", headers=h, json={"name": "Tomo renombrado"}).status_code == 200
    second = Record(title="r2", collection_id=vol.id, status="captured", capture_mode="single", sequence=2)
    db.add(second)
    db.commit()
    assert client.patch(f"/collections/{vol.id}/records/reorder", headers=h,
                        json={"ordered_ids": [second.id, rec.id]}).status_code == 200
    assert client.post(f"/collections/{vol.id}/images/renumber", headers=h).status_code == 200
    db.expire_all()
    assert db.get(Record, rec.id).title == "Retitulado"
    assert db.get(Collection, vol.id).name == "Tomo renombrado"
    assert db.get(Record, second.id).sequence < db.get(Record, rec.id).sequence

    # A record moved, and a volume re-parented, inside the testing space.
    resp = client.post(f"/collections/{vol.id}/move-records", headers=h,
                       params={"target_collection_id": world.V2[TESTING].id})
    assert resp.status_code == 200, resp.text
    db.expire_all()
    assert db.get(Record, rec.id).collection_id == world.V2[TESTING].id
    resp = client.post(f"/collections/{world.V2[TESTING].id}/move-records", headers=h,
                       params={"target_collection_id": vol.id})
    assert resp.status_code == 200, resp.text
    resp = client.patch(f"/collections/{world.V2[TESTING].id}", headers=h, json={"parent_collection_id": vol.id})
    assert resp.status_code == 200 and resp.json()["parent_collection_id"] == vol.id, resp.text
    db.expire_all()
    assert db.get(Record, rec.id).collection_id == vol.id

    db.delete(db.get(Record, second.id))
    # A record left approved by review, as no route of the testing account leaves one.
    db.get(Record, rec.id).status = "approved"
    db.commit()
    # An approved record's images are deleted by an admin only (records.py, LOCKED_STATUSES): the testing
    # account is refused as an operator is, then deletes once the record is back to captured.
    assert client.delete(f"/records/images/{img.id}", headers=h).status_code == 409
    db.get(Record, rec.id).status = "captured"
    db.commit()
    assert client.delete(f"/records/images/{img.id}", headers=h).status_code == 200
    assert client.delete(f"/records/{rec.id}", headers=h).status_code == 200
    db.expire_all()
    assert db.get(RecordImage, img.id) is None and db.get(Record, rec.id) is None
    vol_id, vol2_id = vol.id, world.V2[TESTING].id
    assert client.delete(f"/collections/{vol2_id}", headers=h).status_code == 204
    assert client.delete(f"/collections/{vol_id}", headers=h).status_code == 204
    db.expire_all()
    assert db.get(Collection, vol_id) is None and db.get(Collection, vol2_id) is None
    tp = world.P[TESTING]
    assert client.put(f"/projects/{tp.id}", headers=h, json={"name": "Pruebas renombradas"}).status_code == 200
    db.expire_all()
    assert db.get(Project, tp.id).name == TP + "Pruebas renombradas"
    # The project's tree on disk, under its stored name's directory: its manifest is removed, never retired;
    # a file no testing row names stays, with the directory that holds it, and is logged.
    from app.core.integrity import RETIRED_MANIFESTS_DIR
    from app.core.paths import project_dir_name
    directory = project_dir_name(TP + "Pruebas renombradas")
    tree = _tree(override_projects_root, TP + "Pruebas renombradas",
                 files=("images/suelta.jpg", "metadata/manifest.jsonl"))
    assert client.delete(f"/projects/{tp.id}", headers=h).status_code == 200
    db.expire_all()
    assert db.get(Project, tp.id) is None
    assert db.get(Project, world.P[REAL].id) is not None
    assert not (tree / "metadata").exists()
    assert (tree / "images" / "suelta.jpg").is_file()
    from app.models.system_log import SystemLog
    assert db.query(SystemLog).filter(SystemLog.action == "testing_purge_file_left",
                                      SystemLog.detail.contains(str(tree))).count() == 1
    assert list((override_projects_root / RETIRED_MANIFESTS_DIR).glob(f"{directory}-*-manifest.jsonl")) == []


ADMIN_READS = {
    ("GET", "/records/{rec_id}"): lambda w, r: r.json()["id"] == w.R[TESTING].id,
    ("GET", "/records/{rec_id}/images"): lambda w, r: [i["id"] for i in r.json()] == [w.I[TESTING].id],
    ("GET", "/records/images/{img_id}"): lambda w, r: r.json()["id"] == w.I[TESTING].id,
    ("GET", "/records/images/{img_id}/file"): lambda w, r: b"test" in r.content,
    ("GET", "/records/images/{img_id}/preview"): lambda w, r: b"test" in r.content,
    ("GET", "/records/images/{img_id}/thumbnail"): lambda w, r: b"test" in r.content,
    ("GET", "/records/{rec_id}/annotations"): lambda w, r: [a["id"] for a in r.json()] == [w.A[TESTING].id],
    ("GET", "/records/{rec_id}/rejections"): lambda w, r: [x["comment"] for x in r.json()] == ["rechazo de prueba"],
    ("GET", "/projects/{project_id}"): lambda w, r: (r.json()["id"], r.json()["name"]) == (
        w.P[TESTING].id, "Proyecto test"),
    ("GET", "/projects/{project_id}/records"): lambda w, r: [x["id"] for x in r.json()] == [w.stray.id],
    ("GET", "/collections/{collection_id}"): lambda w, r: r.json()["id"] == w.V[TESTING].id,
    ("GET", "/collections/{collection_id}/hierarchy"): lambda w, r: r.json()["id"] == w.V[TESTING].id,
    ("GET", "/collections/{collection_id}/status-changes"): lambda w, r: [
        (x["record_id"], x["to_status"]) for x in r.json()] == [(w.R[TESTING].id, "rejected")],
    # The USB copy routes: past the rule, the handler answers the empty body and the unknown job itself.
    # The copy is exercised in tests/unit/test_testing_space_usb.py.
    ("POST", "/collections/{collection_id}/usb-copy"): lambda w, r: r.status_code == 422,
    ("GET", "/collections/{collection_id}/usb-copy/status/{job_id}"): lambda w, r: (
        r.status_code, r.json()["detail"]) == (404, "USB copy not found"),
    # Colour card shots (app.core.colour_card): a testing volume answers as not enabled. Exercised in
    # tests/unit/test_colour_card.py.
    ("GET", "/collections/{collection_id}/colour-card"): lambda w, r: r.json()["enabled"] is False,
    ("POST", "/collections/{collection_id}/colour-card/sets"): lambda w, r: (
        r.status_code, r.json()["detail"]["code"]) == (409, "colour_card_not_enabled"),
    ("POST", "/collections/{collection_id}/colour-card/sets/{set_id}/shots"): lambda w, r: r.status_code == 422,
    ("POST", "/collections/{collection_id}/colour-card/sets/{set_id}/complete"): lambda w, r: (
        r.status_code, r.json()["detail"]["code"]) == (409, "colour_card_not_enabled"),
    ("POST", "/collections/{collection_id}/colour-card/skip"): lambda w, r: (
        r.status_code, r.json()["detail"]["code"]) == (409, "colour_card_not_enabled"),
    ("GET", "/collections/{collection_id}/colour-card/shots/{shot_id}/preview"): lambda w, r: (
        r.status_code, r.json()["detail"]["code"]) == (404, "colour_card_shot_not_found"),
}


@pytest.mark.unit
def test_every_admin_read_is_exercised():
    from app.core.space_routes import ROUTES
    assert set(ADMIN_READS) == {k for k, e in ROUTES.items() if e.admin_reads_testing}


@pytest.mark.unit
@pytest.mark.parametrize("key", sorted(ADMIN_READS), ids=lambda k: f"{k[0]} {k[1]}")
def test_an_administrator_reads_testing_material_by_id(world, client, key, monkeypatch, tmp_path):
    from app.core import config
    from app.core.space_routes import ROUTES
    monkeypatch.setattr(config.settings, "EXPORTS_ROOT", str(tmp_path))
    from app.models.record import Record, RecordRejection, RecordStatusChange
    db = world.db
    # A rejection, a status change and a record directly in the project, so each read has something to show.
    db.add(RecordRejection(record_id=world.R[TESTING].id, predefined_reason="blur", comment="rechazo de prueba",
                           rejected_by="u-admin"))
    db.add(RecordStatusChange(record_id=world.R[TESTING].id, collection_id=world.V[TESTING].id,
                              from_status="in_review", to_status="rejected", changed_by="u-admin"))
    world.stray = Record(title="suelto", project_id=world.P[TESTING].id, status="captured", capture_mode="single")
    db.add(world.stray)
    db.commit()
    method, path = key
    resp = _request(world, client, "admin", method, path, ROUTES[key], own=TESTING)
    assert not _not_found(resp), resp.text
    assert ADMIN_READS[key](world, resp), (resp.status_code, resp.text[:300])


EXPORT_ROUTES = [("POST", "/collections/{collection_id}/export"),
                 ("GET", "/collections/{collection_id}/export/status/{job_id}"),
                 ("GET", "/collections/{collection_id}/export/download")]


@pytest.mark.unit
@pytest.mark.parametrize("method,path", EXPORT_ROUTES, ids=[f"{m} {p}" for m, p in EXPORT_ROUTES])
def test_no_one_exports_a_testing_volume(world, client, monkeypatch, tmp_path, method, path):
    """No route takes a testing record to approved, so the export is no route of the testing space.

    The testing account is refused the three routes (403); an administrator, an operator and a reviewer are
    answered 404 `not_found` for a testing volume, as for any testing material they do not reach.
    """
    from app.core import config
    from app.core.space_routes import ROUTES
    exports = tmp_path / "exp"
    exports.mkdir()
    monkeypatch.setattr(config.settings, "EXPORTS_ROOT", str(exports))
    entry = ROUTES[(method, path)]
    assert not entry.testing and not entry.admin_reads_testing
    world.R[TESTING].status = "approved"
    world.db.commit()
    before = world.snapshot()
    url = path.replace("{collection_id}", str(world.V[TESTING].id)).replace("{job_id}", "no-such-job")
    assert client.request(method, url, headers=world.headers["testing"]).status_code == 403
    for caller in ("admin", "operator", "reviewer"):
        assert _not_found(client.request(method, url, headers=world.headers[caller])), caller
    assert world.snapshot() == before
    assert list(exports.iterdir()) == []


# ---------------------------------------------------------------------------
# M. A manifest's space is its directory's name
# ---------------------------------------------------------------------------

def _line(capture_id):
    return {"capture_id": capture_id, "files": []}


@pytest.mark.unit
def test_a_manifests_space_is_read_from_its_directory_name_alone(tmp_path):
    """No row is read: the filter is made without a database session."""
    from app.core import testing_space
    from app.core.integrity import RETIRED_MANIFESTS_DIR
    root = tmp_path / "projects"
    manifests = {
        "real project": (_manifest(root, "proyecto_real", [_line("a")]), False),
        "real stem": (_manifest(root, "xx-real-9", [_line("b")]), False),
        "testing project": (_manifest(root, project_dir_name_of(TP + "Pruebas"), [_line("c")]), True),
        "testing stem": (_manifest(root, T + "t9", [_line("d")]), True),
        "name-based volume in a testing project": (
            _manifest(root, project_dir_name_of(TP + "Pruebas") + "/tomo", [_line("e")]), True),
        "no row names it": (_manifest(root, "huerfano", [_line("f")]), False),
    }
    retired = root / RETIRED_MANIFESTS_DIR
    retired.mkdir(parents=True)
    for label, dir_name, space in (("retired real", "proyecto_viejo", False),
                                   ("retired testing", project_dir_name_of(TP + "Viejo"), True)):
        path = retired / f"{dir_name}-20260101T000000Z-manifest.jsonl"
        path.write_text("{}\n")
        manifests[label] = (path, space)
    for want in (False, True):
        include = testing_space.manifest_filter(None, want, root)
        for label, (path, space) in manifests.items():
            assert include(path) is (space == want), (label, want)


@pytest.mark.unit
def test_nothing_asks_the_database_who_owns_a_manifest():
    from app.core import testing_space
    for gone in ("_manifest_owners", "_owners_of", "manifest_spaces"):
        assert not hasattr(testing_space, gone), gone


def project_dir_name_of(name):
    from app.core.paths import project_dir_name
    return project_dir_name(name)


# ---------------------------------------------------------------------------
# N. One lock from the directory check to the commit; the rename recovery
# ---------------------------------------------------------------------------

@pytest.fixture
def contended(monkeypatch):
    """The project lock held by the test; each flock asked for while it is held is recorded, then let through.

    The test holds `<imports>/.project-create.lock` on a descriptor of its
    own. When a writer asks for it, its own non-blocking attempt fails,
    which proves the two contend on one lock; the test then lets its hold
    go and the writer's blocking call goes ahead. Each flush that writes a
    project records whether the lock is held at that moment.
    """
    import fcntl
    import os
    from sqlalchemy import event
    from sqlalchemy.orm import Session
    from app.core import list_import
    from app.models.project import Project

    path = list_import.reports_dir() / list_import.PROJECT_LOCK_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    state = {"fd": None, "waited": 0, "flushes": []}
    real_flock = fcntl.flock

    def hold():
        """Take the lock on the test's descriptor; a no-op while the test still holds it (nobody asked)."""
        if state["fd"] is None:
            state["fd"] = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
            real_flock(state["fd"], fcntl.LOCK_EX | fcntl.LOCK_NB)

    def flock(fd, op):
        if op == fcntl.LOCK_EX and state["fd"] is not None:
            try:
                real_flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                state["waited"] += 1
                real_flock(state["fd"], fcntl.LOCK_UN)
                os.close(state["fd"])
                state["fd"] = None
            else:
                raise AssertionError("the writer's lock is not the one the test holds")
        return real_flock(fd, op)

    def is_held():
        probe = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            real_flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        finally:
            os.close(probe)
        return False

    def after_flush(session, flush_context):
        if any(isinstance(o, Project) for o in list(session.new) + list(session.dirty)):
            state["flushes"].append(is_held())

    monkeypatch.setattr(list_import.fcntl, "flock", flock)
    event.listen(Session, "after_flush", after_flush)
    state["hold"] = hold
    yield state
    event.remove(Session, "after_flush", after_flush)
    if state["fd"] is not None:
        os.close(state["fd"])


@pytest.mark.unit
def test_create_rename_and_the_list_import_contend_on_one_lock(world, client, contended):
    from app.core import list_import
    contended["hold"]()
    resp = client.post("/projects/", headers=world.headers["admin"], json={"name": "Nuevo"})
    assert resp.status_code == 200, resp.text
    assert contended["waited"] == 1, "the create route did not ask for the lock"
    contended["hold"]()
    resp = client.put(f"/projects/{world.P[TESTING].id}", headers=world.headers["testing"], json={"name": "Otra"})
    assert resp.status_code == 200, resp.text
    assert contended["waited"] == 2, "the rename route did not ask for the lock"
    contended["hold"]()
    list_import._project_for(world.db, "Desde lista", "u-admin", True)
    assert contended["waited"] == 3, "the list import did not ask for the lock"
    assert contended["flushes"] and all(contended["flushes"]), contended["flushes"]


@pytest.mark.unit
def test_without_the_lock_create_and_rename_are_refused(world, client, monkeypatch):
    from app.core import list_import
    from app.models.project import Project
    monkeypatch.setattr(list_import, "_take_project_lock", lambda: None)
    assert client.post("/projects/", headers=world.headers["admin"], json={"name": "Nuevo"}).status_code == 503
    resp = client.put(f"/projects/{world.P[TESTING].id}", headers=world.headers["testing"], json={"name": "Otra"})
    assert resp.status_code == 503, resp.text
    world.db.expire_all()
    assert world.db.query(Project).filter(Project.name == "Nuevo").count() == 0
    assert world.db.get(Project, world.P[TESTING].id).name == TP + "Proyecto test"


@pytest.mark.unit
@pytest.mark.parametrize("name", ["日本", "...", "."])
def test_a_name_that_gives_no_directory_is_refused(world, client, name):
    resp = client.post("/projects/", headers=world.headers["admin"], json={"name": name})
    assert resp.status_code == 422 and resp.json()["detail"]["code"] == "project_name_invalid", resp.text
    real = world.P[REAL]
    resp = client.put(f"/projects/{real.id}", headers=world.headers["admin"], json={"name": name})
    assert resp.status_code == 422 and resp.json()["detail"]["code"] == "project_name_invalid", resp.text


def _journal_world(db, root, data_dir, monkeypatch):
    from app.core import config
    monkeypatch.setattr(config.settings, "DATA_DIR", str(data_dir))
    from app.models.project import Project
    other = Project(name="Otro")
    moving = Project(name="Viejo")
    db.add_all([other, moving])
    db.commit()
    return other, moving


@pytest.mark.unit
def test_the_recovery_does_not_give_a_project_a_directory_another_projects_name_maps_to(
        db_session, override_projects_root, tmp_path, monkeypatch):
    """DB at the old name, the new directory there and the old one gone: it is another project's, not a moved tree."""
    import app.core.storage_ops as so
    from app.models.system_log import SystemLog
    other, moving = _journal_world(db_session, override_projects_root, tmp_path / "data", monkeypatch)
    old_root, new_root = override_projects_root / "viejo", override_projects_root / "otro"
    (new_root / "images").mkdir(parents=True)
    (new_root / "images" / "a.jpg").write_bytes(b"other's")
    # The project had a tree when the rename began, and it is gone now: only the other
    # project's name mapping to the new directory tells that the directory there is not this tree.
    old_root.mkdir(parents=True)
    so.write_rename_journal(moving.id, "Viejo", "OTRO", old_root, new_root)
    assert so.read_rename_journal()["old_root_existed"] is True
    old_root.rmdir()
    so.reconcile_pending_rename(db_session)
    db_session.expire_all()
    assert db_session.get(type(moving), moving.id).name == "Viejo"
    assert (new_root / "images" / "a.jpg").read_bytes() == b"other's"
    assert so.read_rename_journal() is not None
    [log] = db_session.query(SystemLog).filter(SystemLog.action == "project_rename_not_completed").all()
    assert log.level == "ERR"


@pytest.mark.unit
def test_the_recovery_does_not_complete_onto_a_directory_that_existed_before_the_move(
        db_session, override_projects_root, tmp_path, monkeypatch):
    """The project had no tree when the rename began: a directory at the new name now is not its tree."""
    import app.core.storage_ops as so
    from app.models.system_log import SystemLog
    _, moving = _journal_world(db_session, override_projects_root, tmp_path / "data", monkeypatch)
    old_root, new_root = override_projects_root / "viejo", override_projects_root / "nuevo"
    so.write_rename_journal(moving.id, "Viejo", "Nuevo", old_root, new_root)
    (new_root / "images").mkdir(parents=True)
    so.reconcile_pending_rename(db_session)
    db_session.expire_all()
    assert db_session.get(type(moving), moving.id).name == "Viejo"
    assert db_session.query(SystemLog).filter(SystemLog.action == "project_rename_not_completed").count() == 1


@pytest.mark.unit
def test_the_recovery_does_not_move_a_tree_into_a_directory_another_projects_name_maps_to(
        db_session, override_projects_root, tmp_path, monkeypatch):
    """DB at the new name and the tree at the old one, while another project's name maps to the new directory."""
    import app.core.storage_ops as so
    from app.models.project import Project
    from app.models.system_log import SystemLog
    _, moving = _journal_world(db_session, override_projects_root, tmp_path / "data", monkeypatch)
    old_root, new_root = override_projects_root / "viejo", override_projects_root / "gemelo"
    (old_root / "images").mkdir(parents=True)
    so.write_rename_journal(moving.id, "Viejo", "Gemelo", old_root, new_root)
    moving.name = "Gemelo"
    db_session.add(Project(name="GEMELO"))
    db_session.commit()
    so.reconcile_pending_rename(db_session)
    assert (old_root / "images").exists() and not new_root.exists()
    assert so.read_rename_journal() is not None
    assert db_session.query(SystemLog).filter(SystemLog.action == "project_rename_not_completed").count() == 1


# ---------------------------------------------------------------------------
# O. Body targets are read from the body as the handler's own model validates it
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_body_target_is_read_as_the_handlers_model_reads_it(world, client):
    """DualCaptureRequest takes afterRecordId and ignores after_record_id beside it: so does the rule."""
    _trap_role_checkers(("POST", "/cameras/capture/dual"))
    h = world.headers["operator"]
    real, test = world.R[REAL].id, world.R[TESTING].id
    resp = client.post("/cameras/capture/dual", headers=h, json={
        "project_name": world.P[REAL].name, "afterRecordId": real, "after_record_id": test})
    assert resp.status_code == 418, resp.text
    resp = client.post("/cameras/capture/dual", headers=h, json={
        "project_name": world.P[REAL].name, "afterRecordId": test, "after_record_id": real})
    assert _not_found(resp), resp.text
    # By its field name alone, as populate_by_name takes it.
    resp = client.post("/cameras/capture/dual", headers=h, json={
        "project_name": world.P[REAL].name, "after_record_id": test})
    assert _not_found(resp), resp.text


@pytest.mark.unit
def test_a_body_that_does_not_validate_is_left_to_the_handlers_422(world, client):
    resp = client.post("/cameras/capture/dual", headers=world.headers["operator"], json={
        "afterRecordId": world.R[TESTING].id})
    assert resp.status_code == 422, resp.text


@pytest.mark.unit
def test_each_body_target_is_declared_once_by_its_field_name():
    from app.core.space_routes import ROUTES
    for key, entry in ROUTES.items():
        names = [t.name for t in entry.targets if t.where == "body"]
        assert len(names) == len(set(names)), key
    assert ("body", "afterRecordId") not in {(t.where, t.name) for t in ROUTES[("POST", "/cameras/capture/dual")].targets}


@pytest.mark.unit
def test_the_rule_names_no_alias_of_its_own():
    """The alias is the model's alone: neither the rule nor the table spells it."""
    import app.api.space as space
    import app.core.space_routes as space_routes
    for module in (space, space_routes):
        assert "afterRecordId" not in Path(module.__file__).read_text().replace(
            '_b("afterRecordId", RECORD)', ""), module.__name__


# ---------------------------------------------------------------------------
# P. A refusal names nothing the caller did not send
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_stem_refused_as_a_projects_directory_does_not_name_the_project(world, client):
    from app.models.collection import Collection, StemIsProjectDirectoryError
    from app.models.project import Project
    _project_row(world.db, "Alfa-Beta Secreto")
    h = world.headers["operator"]
    resp = client.patch(f"/collections/{world.V2[REAL].id}", headers=h, json={"filename_stem": "alfa-beta_secreto"})
    assert resp.status_code in (409, 422), resp.text
    _project_row(world.db, "Gamma-Oculto")
    resp = client.patch(f"/collections/{world.V2[REAL].id}", headers=h, json={"filename_stem": "gamma-oculto"})
    assert resp.status_code == 409 and "Gamma" not in resp.text, resp.text
    # The same refusal at the flush, for a writer no route checked first.
    world.db.add(Collection(name="X", project_id=world.P[REAL].id, filename_stem="gamma-oculto"))
    with pytest.raises(StemIsProjectDirectoryError) as exc:
        world.db.commit()
    world.db.rollback()
    assert "Gamma" not in str(exc.value)
    world.db.add_all([Project(name="Delta-Escondido"),
                      Collection(name="Y", project_id=world.P[REAL].id, filename_stem="delta-escondido")])
    with pytest.raises(StemIsProjectDirectoryError) as exc:
        world.db.commit()
    world.db.rollback()
    assert "Delta" not in str(exc.value)


# ---------------------------------------------------------------------------
# Q. Real names in the testing namespace, through the routes; the testing account and a volume's codes
# ---------------------------------------------------------------------------

def _reserved(resp):
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "name_reserved", resp.text


RESERVED = [T + "x", "Captua-Testing-X", "cáptua-testing-x"]


@pytest.mark.unit
@pytest.mark.parametrize("caller", ["admin", "operator"])
def test_a_real_project_is_never_named_in_the_testing_namespace(world, client, caller):
    from app.models.project import Project
    h = world.headers[caller]
    for name in RESERVED:
        resp = client.post("/projects/", headers=h, json={"name": name})
        if caller == "admin":
            _reserved(resp)
        else:
            assert resp.status_code == 403, resp.text
        _reserved(client.put(f"/projects/{world.P[REAL].id}", headers=h, json={"name": name}))
    world.db.expire_all()
    assert world.db.get(Project, world.P[REAL].id).name == "Proyecto real"
    assert world.db.query(Project).filter(Project.testing.is_(False)).count() == 1


@pytest.mark.unit
@pytest.mark.parametrize("caller", ["admin", "operator"])
def test_a_list_named_in_the_testing_namespace_is_refused_through_the_route(world, client, caller):
    import json as _json
    import uuid as _uuid
    from app.core import list_import
    from app.models.project import Project
    for name in RESERVED:
        doc = {"contract": list_import.CONTRACT_NAME, "kind": list_import.LIST_KIND, "version": "1.0",
               "id": str(_uuid.uuid4()), "list": {"name": name}, "items": []}
        resp = client.post("/imports/digitisation-list", headers=world.headers[caller],
                           files={"file": ("l.json", _json.dumps(doc).encode(), "application/json")})
        assert resp.status_code == 422 and "testing space" in resp.text, resp.text
    assert world.db.query(Project).count() == 2


@pytest.mark.unit
@pytest.mark.parametrize("caller", ["admin", "operator"])
@pytest.mark.parametrize("field", ["filename_stem", "external_id"])
def test_a_real_volumes_codes_are_never_in_the_testing_namespace_through_the_routes(world, client, caller, field):
    from app.models.collection import Collection
    h = world.headers[caller]
    before = world.db.query(Collection).count()
    values = [T + "r9"] + (["CAPTUA-TESTING-R9", "Cáptua-testing-r9"] if field == "external_id" else [])
    for value in values:
        _reserved(client.post("/collections/", headers=h, json={"name": "Nuevo", "filename_stem": "xx-zz-9",
                                                                 field: value}))
        _reserved(client.patch(f"/collections/{world.V2[REAL].id}", headers=h, json={field: value}))
    world.db.expire_all()
    assert world.db.query(Collection).count() == before
    assert getattr(world.db.get(Collection, world.V2[REAL].id), field) in (None, "xx-real-2")


@pytest.mark.unit
@pytest.mark.parametrize("field", ["filename_stem", "external_id"])
def test_the_testing_account_does_not_set_or_change_a_volumes_codes(world, client, field):
    from app.models.collection import Collection
    h = world.headers["testing"]
    cases = [(world.V2[TESTING], T + "nuevo"), (world.V[TESTING], T + "otro")]
    if field == "filename_stem":
        cases.append((world.V2[TESTING], None))  # clearing it is a change too
    for vol, value in cases:
        resp = client.patch(f"/collections/{vol.id}", headers=h, json={field: value})
        assert resp.status_code == 409 and resp.json()["detail"]["code"] == "testing_volume_codes", resp.text
    world.db.expire_all()
    stored = world.db.get(Collection, world.V[TESTING].id)
    # The value it already has changes nothing, and is let through with the rest of the body.
    resp = client.patch(f"/collections/{stored.id}", headers=h,
                        json={field: getattr(stored, field), "name": "Renombrado"})
    assert resp.status_code == 200 and resp.json()["name"] == "Renombrado", resp.text


# ---------------------------------------------------------------------------
# R. The four review sequences, through the real routes
# ---------------------------------------------------------------------------

def _append_manifest(root, directory, entries):
    import json as _json
    path = root / directory / "metadata" / "manifest.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write("".join(_json.dumps(e) + "\n" for e in entries))
    return path


@pytest.mark.unit
def test_review_i_a_testing_project_named_like_the_default_project_stalls_no_real_work(world, client, link):
    """Renamed "Défault", "default", "Default ": operators still create volumes, and a queue sync imports."""
    from datetime import datetime, timezone
    from app.core import unit_mode
    from app.core.fisqua_queue import apply_queue
    from app.models.collection import Collection
    from app.models.project import Project
    from tests.unit.test_testing_space_schema import _queue_body
    # Two real projects, so the default project is the one named "default", created on first use.
    _project_row(world.db, "Segundo real")
    _unit(world.db, "offline")
    tp = world.P[TESTING]
    default_id = None
    for n, name in enumerate(["Défault", "default", "Default "], start=1):
        resp = client.put(f"/projects/{tp.id}", headers=world.headers["testing"], json={"name": name})
        assert resp.status_code == 200 and resp.json()["name"] == name, resp.text
        resp = client.post("/collections/", headers=world.headers["operator"],
                           json={"name": f"V{n}", "filename_stem": f"xx-def-{n}"})
        assert resp.status_code == 201, resp.text
        world.db.expire_all()
        project = world.db.get(Project, world.db.get(Collection, resp.json()["id"]).project_id)
        assert (project.name, project.testing) == ("default", False)
        assert default_id in (None, project.id)
        default_id = project.id
        chosen, _ = unit_mode.default_project(world.db, "u-admin")
        assert (chosen.id, chosen.testing) == (project.id, False)
    _unit(world.db, "fisqua", default_project_id=default_id)
    ack, report, _ = apply_queue(world.db, _queue_body("Cola", ["xx-def-901", "xx-def-902"]), "r1",
                                 datetime.now(timezone.utc))
    assert ack["skipped"] == [] and report["created"] == 2 and report["projectName"] == "default", (ack, report)
    world.db.expire_all()
    assert world.db.get(Project, tp.id).name == TP + "Default "


@pytest.mark.unit
def test_review_ii_a_retired_real_manifest_stays_real_when_a_testing_project_takes_the_name(
        world, client, override_projects_root, monkeypatch, tmp_path):
    from app.core import config, integrity
    from app.core.capture_manifest import ManifestRefused, build_capture_manifest
    from app.core.integrity import RETIRED_MANIFESTS_DIR, build_fixity_index
    from app.core.testing_space import manifest_filter
    from app.models.collection import Collection
    from app.models.record import RecordImage
    root = override_projects_root
    line = _real_image_for_export(world, root, "cid-retired")
    gone = _project_row(world.db, "Archivo")
    _manifest(root, "archivo", [dict(line, origin="RETIRED-REAL-LINE")])
    assert client.delete(f"/projects/{gone.id}", headers=world.headers["admin"]).status_code == 200
    assert len(list((root / RETIRED_MANIFESTS_DIR).glob("archivo-*-manifest.jsonl"))) == 1
    resp = client.put(f"/projects/{world.P[TESTING].id}", headers=world.headers["testing"], json={"name": "Archivo"})
    assert resp.status_code == 200 and resp.json()["name"] == "Archivo", resp.text

    img = world.db.get(RecordImage, world.I[REAL].id)
    path = (root / world.stem[REAL] / "captures" / img.filename).resolve()
    sha = line["files"][0]["sha256"]
    projects_dir = config.settings.projects_dir.resolve()
    real = build_fixity_index(projects_dir, include=manifest_filter(world.db, False, projects_dir))
    assert real.expected("cid-retired", img.id, path) == sha
    testing = build_fixity_index(projects_dir, include=manifest_filter(world.db, True, projects_dir))
    assert testing.expected("cid-retired", img.id, path) is None
    assert b"RETIRED-REAL-LINE" in _export_bag_manifest(client, world, monkeypatch, tmp_path, world.V[REAL])
    built = []
    real_build = integrity.build_fixity_index

    def recording(*args, **kwargs):
        built.append(real_build(*args, **kwargs))
        return built[-1]

    monkeypatch.setattr(integrity, "build_fixity_index", recording)
    try:
        build_capture_manifest(world.db, world.db.get(Collection, world.V[REAL].id))
    except ManifestRefused:
        pass  # a tenant or an offsite copy missing in the test; the index was built first
    assert built and built[0].expected("cid-retired", img.id, path) == sha


@pytest.mark.unit
def test_review_iii_a_retired_testing_manifest_never_reaches_real_work(
        world, client, override_projects_root, monkeypatch, tmp_path):
    from app.core.integrity import RETIRED_MANIFESTS_DIR
    from app.core.paths import project_dir_name
    from app.models.record import RecordImage
    root = override_projects_root
    h = world.headers["testing"]
    tp = world.P[TESTING]
    directory = project_dir_name(tp.name)
    _manifest(root, directory, [{"capture_id": "cid-gone", "origin": "TESTING-LINE",
                                 "files": [{"role": "single", "relative_path": "images/gone.jpg",
                                            "sha256": "0" * 64}]}])
    assert client.delete(f"/records/images/{world.I[TESTING].id}", headers=h).status_code == 200
    assert client.delete(f"/records/{world.R[TESTING].id}", headers=h).status_code == 200
    for vol in (world.V2[TESTING], world.V[TESTING]):
        assert client.delete(f"/collections/{vol.id}", headers=h).status_code == 204
    assert client.delete(f"/projects/{tp.id}", headers=h).status_code == 200
    # Removed with the project's directory, never retired.
    assert list((root / RETIRED_MANIFESTS_DIR).glob(f"{directory}-*-manifest.jsonl")) == []
    assert not (root / directory).exists()

    resp = client.post(f"/records/{world.R[REAL].id}/images", headers=world.headers["operator"],
                       params={"capture_id": "cid-gone"}, files={"file": ("gone.jpg", JPEG, "image/jpeg")})
    assert resp.status_code in (200, 201), resp.text
    world.db.expire_all()
    assert world.db.query(RecordImage).filter(RecordImage.capture_id == "cid-gone").count() == 1
    line = _real_image_for_export(world, root, "cid-real-line")
    _append_manifest(root, world.stem[REAL], [dict(line, origin="REAL-LINE")])
    manifest = _export_bag_manifest(client, world, monkeypatch, tmp_path, world.V[REAL])
    assert b"REAL-LINE" in manifest and b"TESTING-LINE" not in manifest


@pytest.mark.unit
def test_review_iv_directories_are_unique_in_the_testing_space_and_apart_from_real_ones(world, client):
    from app.core.paths import project_dir_name
    from app.models.project import Project
    other = _project_row(world.db, "Otra", testing=True)
    h = world.headers["testing"]
    assert client.put(f"/projects/{world.P[TESTING].id}", headers=h, json={"name": "Plaza"}).status_code == 200
    for name in ("PLAZA", "Pláza", "plaza"):
        _taken(client.put(f"/projects/{other.id}", headers=h, json={"name": name}))
    resp = client.post("/projects/", headers=world.headers["admin"], json={"name": "Plaza"})
    assert resp.status_code == 200 and resp.json()["name"] == "Plaza", resp.text
    world.db.expire_all()
    real, tp = world.db.get(Project, resp.json()["id"]), world.db.get(Project, world.P[TESTING].id)
    assert real.display_name == tp.display_name == "Plaza"
    assert (project_dir_name(real.name), project_dir_name(tp.name)) == ("plaza", TP + "plaza")
    assert world.db.get(Project, other.id).name == TP + "Otra"


# ---------------------------------------------------------------------------
# S. The route's namespace check comes before the Fisqua lookup
# ---------------------------------------------------------------------------

@pytest.fixture
def lookups(monkeypatch):
    from app.core import unit_mode
    asked = []

    def check(db, codes):
        asked.append(list(codes))

    monkeypatch.setattr(unit_mode, "check_reference_codes", check)
    return asked


@pytest.mark.unit
@pytest.mark.parametrize("body", [
    {"name": "Nuevo", "filename_stem": T + "r9"},
    {"name": "Nuevo", "filename_stem": "xx-zz-9", "external_id": "CAPTUA-TESTING-R9"},
    {"name": "Captua-Testing-Tomo", "filename_stem": "xx-zz-9"},
], ids=["stem", "external_id", "name"])
def test_post_collections_refuses_the_namespace_before_asking_fisqua(world, client, link, lookups, body):
    from app.models.collection import Collection
    _unit(world.db, "fisqua")
    before = world.db.query(Collection).count()
    _reserved(client.post("/collections/", headers=world.headers["admin"], json=body))
    assert lookups == []
    assert world.db.query(Collection).count() == before


@pytest.mark.unit
@pytest.mark.parametrize("body", [{"filename_stem": T + "r9"}, {"external_id": "Captua-Testing-R9"},
                                  # With a new code beside it, which the route would otherwise ask Fisqua about.
                                  {"name": "captua-testing-tomo", "external_id": "xx-real-2"}],
                         ids=["stem", "external_id", "name"])
def test_patch_collections_refuses_the_namespace_before_asking_fisqua(world, client, link, lookups, body):
    from app.models.collection import Collection
    _unit(world.db, "fisqua")
    vol = world.V2[REAL]
    _reserved(client.patch(f"/collections/{vol.id}", headers=world.headers["admin"], json=body))
    assert lookups == []
    world.db.expire_all()
    stored = world.db.get(Collection, vol.id)
    assert (stored.name, stored.filename_stem, stored.external_id) == ("Tomo real 2", "xx-real-2", None)


@pytest.mark.unit
def test_a_testing_project_renamed_as_a_stems_tail_meets_no_stem(world, client):
    """A testing project's directory begins with the project prefix, which no stem does: the two never meet."""
    from app.core.paths import project_dir_name
    from app.models.collection import Collection
    from app.models.project import Project
    vol = Collection(name="Choque", project_id=world.P[TESTING].id, filename_stem=T + "choque")
    world.db.add(vol)
    world.db.commit()
    resp = client.put(f"/projects/{world.P[TESTING].id}", headers=world.headers["testing"], json={"name": "choque"})
    assert resp.status_code == 200 and resp.json()["name"] == "choque", resp.text
    assert T not in resp.text, resp.text
    world.db.expire_all()
    assert project_dir_name(world.db.get(Project, world.P[TESTING].id).name) == TP + "choque" != T + "choque"


@pytest.mark.unit
def test_the_testing_accounts_display_name_has_a_limit(world, client, override_projects_root):
    from app.core.paths import project_dir_name
    from app.core.testing_space import display_name_max_length
    from app.models.project import Project
    longest = display_name_max_length()
    assert longest == 255 - len(TP)
    tp = world.P[TESTING]
    tree = _tree(override_projects_root, tp.name)
    h = world.headers["testing"]
    resp = client.put(f"/projects/{tp.id}", headers=h, json={"name": "a" * (longest + 1)})
    assert resp.status_code == 422 and resp.json()["detail"]["code"] == "project_name_invalid", resp.text
    world.db.expire_all()
    assert world.db.get(Project, tp.id).name == TP + "Proyecto test" and (tree / "images/a.jpg").exists()
    resp = client.put(f"/projects/{tp.id}", headers=h, json={"name": "a" * longest})
    assert resp.status_code == 200 and resp.json()["name"] == "a" * longest, resp.text
    world.db.expire_all()
    stored = world.db.get(Project, tp.id).name
    assert len(stored) == 255 and (override_projects_root / project_dir_name(stored) / "images/a.jpg").exists()



# ---------------------------------------------------------------------------
# T. The real lock: a second writer waits for the first one's commit
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("second", ["create", "rename", "list import"])
def test_a_second_writer_waits_at_the_lock_until_the_first_commits(world, client, monkeypatch, second):
    """The first writer, POST /projects/ "Plaza", is held inside its commit; the second asks for the lock then.

    The second writer's flock is tried without blocking first: it is
    refused while the first writer is inside its commit, which shows that
    the lock is held there, and nothing of the second writer's check has
    run. Released, the first commits and the second goes on, and sees it.
    """
    import fcntl
    import threading
    from fastapi.testclient import TestClient
    from sqlalchemy import event
    from sqlalchemy.orm import Session
    from app.api.deps import get_db_dependency
    from app.core import list_import, storage_ops
    from app.core.db import SessionLocal
    from app.main import app
    from app.models.project import Project

    def own_session():
        session = SessionLocal()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db_dependency] = own_session
    in_commit, release, progress = threading.Event(), threading.Event(), threading.Event()
    state = {"blocked": None}
    order = []
    real_flock = fcntl.flock

    def flock(fd, op):
        if op == fcntl.LOCK_EX and in_commit.is_set() and not release.is_set() and state["blocked"] is None:
            try:
                real_flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                state["blocked"] = True
                progress.set()
                return real_flock(fd, op)
            state["blocked"] = False
            progress.set()
            return None
        return real_flock(fd, op)

    def first_writer(session):
        return any(isinstance(o, Project) and o.name == "Plaza" for o in session.new)

    def before_commit(session):
        if first_writer(session) and not in_commit.is_set():
            session.info["first"] = True
            in_commit.set()
            release.wait(30)

    def after_commit(session):
        if session.info.pop("first", False):
            order.append("first committed")

    real_refuse = storage_ops.refuse_taken_project_directory

    def refuse(db, name, exclude_id=None):
        if name != "Plaza":
            order.append("second checked")
        return real_refuse(db, name, exclude_id)

    monkeypatch.setattr(list_import.fcntl, "flock", flock)
    monkeypatch.setattr(storage_ops, "refuse_taken_project_directory", refuse)
    event.listen(Session, "before_commit", before_commit)
    event.listen(Session, "after_commit", after_commit)
    out = {}
    real_id = world.P[REAL].id

    def run_first():
        out["first"] = TestClient(app).post("/projects/", headers=world.headers["admin"], json={"name": "Plaza"})

    def run_second():
        if second == "create":
            out["second"] = TestClient(app).post("/projects/", headers=world.headers["admin"], json={"name": "PLAZA"})
        elif second == "rename":
            out["second"] = TestClient(app).put(f"/projects/{real_id}", headers=world.headers["admin"],
                                                json={"name": "Pláza"})
        else:
            session = SessionLocal()
            try:
                project, created = list_import._project_for(session, "plaza", "u-admin", True)
                out["second"] = (project.name, created)
            finally:
                session.close()

    first = threading.Thread(target=run_first)
    later = threading.Thread(target=run_second)
    try:
        first.start()
        assert in_commit.wait(30), "the first writer never reached its commit"
        later.start()
        assert progress.wait(30), "the second writer never asked for the lock"
        assert state["blocked"] is True, "the second writer took the lock while the first was inside its commit"
        assert "second checked" not in order
    finally:
        release.set()
        first.join(30)
        later.join(30)
        event.remove(Session, "before_commit", before_commit)
        event.remove(Session, "after_commit", after_commit)
    assert out["first"].status_code == 200, out["first"].text
    if second == "list import":
        assert out["second"] == ("Plaza", False)
    else:
        assert order == ["first committed", "second checked"], order
        assert out["second"].status_code == 409, out["second"].text
        assert out["second"].json()["detail"]["code"] == "project_directory_taken"


# ---------------------------------------------------------------------------
# U. A body sent without a content type
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_body_without_a_content_type_is_refused_by_the_handler_and_skipped_by_the_rule(world, client):
    """FastAPI reads no JSON without a JSON content type, so the handler answers 422 and the rule reads nothing.

    If the handler ever took such a body, it would act on the testing record
    this body names, and this test fails on the status.
    """
    import json as _json
    before = world.snapshot()
    resp = client.post("/records/bulk-status", headers=world.headers["operator"],
                       content=_json.dumps({"record_ids": [world.R[TESTING].id], "status": "in_review"}))
    assert resp.status_code == 422, resp.text
    assert not _not_found(resp)
    assert world.snapshot() == before


# ---------------------------------------------------------------------------
# Stored image paths are written by the server only
# ---------------------------------------------------------------------------

# Every place under app/ and capture/ that assigns an image row's file_path or thumbnail_path, or builds a
# RecordImage, as (module, function). Each has been checked against the space rule: it writes a path the
# server derives from the row's own volume or project directory.
IMAGE_PATH_WRITERS = {
    ("app/api/records.py", "add_image_to_record"),      # the upload, into the record's volume
    ("app/api/records.py", "get_image_thumbnail"),      # a thumbnail regenerated beside its source
    ("app/api/cameras.py", "trigger_capture"),          # the single capture
    ("app/api/cameras.py", "create_image_record"),      # the dual capture's rows
    ("app/core/rebuild_volume.py", "_write_record"),    # a volume rebuilt from its bundle
    ("app/core/storage_ops.py", "relocate_image"),      # a record's files moved with it
    ("app/core/storage_ops.py", "reconcile_pending_rename"),  # an interrupted project rename completed
    ("app/core/volume_layout.py", "_execute"),          # the move into the stem layout
    ("app/api/projects.py", "_update_project_held"),    # a project rename's rebase
    # An adoption's renames, built by its plan from the target's own directory; both volumes real work.
    ("app/core/adopt_apply.py", "_write_rows"),
    # The same adoption's colour card shots: a shot row's paths, built by the plan from the target's own
    # `calibration/`; not an image row, matched by the attribute's name.
    ("app/core/adopt_apply.py", "_write_card_rows"),
}


@pytest.mark.unit
def test_stored_image_paths_are_written_by_the_server_only():
    """A stored image path is written by the server from the row's own volume or project directory, never from a client.

    Read with ast over every module under app/ and capture/: the only places
    that assign `.file_path` or `.thumbnail_path`, or call setattr with
    either name, or build a RecordImage, are IMAGE_PATH_WRITERS. No route in
    app/api takes RecordImageCreate or RecordImageBase as its body. A new
    writer fails this test until it has been checked against the space rule
    and added to the list.
    """
    import ast
    from fastapi.routing import APIRoute
    from app.main import app
    from app.schemas.record import RecordImageBase
    backend = Path(__file__).resolve().parents[2]
    found = set()
    for base in ("app", "capture"):
        for path in sorted((backend / base).rglob("*.py")):
            tree = ast.parse(path.read_text())
            parents = {}
            for node in ast.walk(tree):
                for child in ast.iter_child_nodes(node):
                    parents[child] = node

            def owner(node):
                while node in parents and not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    node = parents[node]
                return getattr(node, "name", None)

            rel = str(path.relative_to(backend))
            for node in ast.walk(tree):
                hit = False
                if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    hit = any(isinstance(x, ast.Attribute) and x.attr in ("file_path", "thumbnail_path")
                              for t in targets for x in ast.walk(t))
                elif isinstance(node, ast.Call):
                    func = node.func
                    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                    hit = name == "RecordImage" or (
                        name == "setattr" and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant)
                        and node.args[1].value in ("file_path", "thumbnail_path"))
                if hit:
                    found.add((rel, owner(node)))
    assert found == IMAGE_PATH_WRITERS
    bodies = []
    for route in app.routes:
        if isinstance(route, APIRoute):
            for param in route.dependant.body_params:
                annotation = getattr(param, "type_", None) or getattr(getattr(param, "field_info", None),
                                                                       "annotation", None)
                if isinstance(annotation, type) and issubclass(annotation, RecordImageBase):
                    bodies.append((sorted(route.methods), route.path))
    assert bodies == []
