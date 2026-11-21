"""Which space each route of the application reaches (NEH-391): one entry per route.

Every route of the application has exactly one entry in ROUTES, keyed by
its method and its full path template (the include prefix and the path,
as FastAPI matches it). An entry states:

* `testing`: whether the testing account is admitted to the route. The one
  RoleChecker (app.api.auth) admits the account where this is True and
  nowhere else; app.api.space.enforce_space refuses it with a 403 on every
  other route, and on a route that has no entry.
* `admin_reads_testing`: whether an administrator may name testing
  material on the route. Where it is False, an administrator reaches real
  work only.
* `targets`: every path, query or body field through which a request names
  material (a project, a volume, a record, an image, an annotation, a
  camera settings row, a project by name, a list of records, or a capture
  id). A body field is named by its field name in the route's body model,
  never by an alias, and read as the handler's validated model has it
  (app.api.space). Each is resolved to its spaces by app.core.testing_space.spaces_of,
  and a target in
  a space the caller does not reach is answered 404 `not_found`, as if it
  did not exist.
* `lists`: the model a list or count route lists. Its handler scopes the
  rows with app.core.testing_space.scope_query and takes the `space` query
  parameter; an administrator's targets on it are read in the space that
  parameter asks for.
* `public`: a route that needs no session. app.api.space.enforce_space
  leaves it alone.

tests/unit/test_testing_space_access.py walks the application's routes and
fails when a route has no entry, an entry has no route, or a path, query
or body field named like material is not declared as a target.

Version: 0.3.3
"""

from __future__ import annotations

import weakref
from typing import Dict, NamedTuple, Optional, Tuple

PATH = "path"
QUERY = "query"
BODY = "body"

PROJECT = "project"
COLLECTION = "collection"
RECORD = "record"
IMAGE = "image"
ANNOTATION = "annotation"
CAMERA_SETTINGS = "camera_settings"
PROJECT_NAME = "project_name"
RECORD_LIST = "record_list"
CAPTURE = "capture"

KINDS = frozenset({PROJECT, COLLECTION, RECORD, IMAGE, ANNOTATION, CAMERA_SETTINGS, PROJECT_NAME,
                   RECORD_LIST, CAPTURE})


class Target(NamedTuple):
    """A field through which a request names material: where it is, its name as the client sends it, its kind."""

    where: str
    name: str
    kind: str


class Entry(NamedTuple):
    """One route's place in the testing space's access rule."""

    testing: bool = False
    admin_reads_testing: bool = False
    targets: Tuple[Target, ...] = ()
    lists: Optional[str] = None
    public: bool = False


def _p(name, kind):
    return Target(PATH, name, kind)


def _q(name, kind):
    return Target(QUERY, name, kind)


def _b(name, kind):
    return Target(BODY, name, kind)


PUBLIC = Entry(public=True)

_REC = _p("rec_id", RECORD)
_IMG = _p("img_id", IMAGE)
_PROJ = _p("project_id", PROJECT)
_COL = _p("collection_id", COLLECTION)
_CAM_SETTINGS = _p("id", CAMERA_SETTINGS)

ROUTES: Dict[Tuple[str, str], Entry] = {
    # FastAPI's own documentation routes.
    ("GET", "/openapi.json"): PUBLIC,
    ("GET", "/docs"): PUBLIC,
    ("GET", "/docs/oauth2-redirect"): PUBLIC,
    ("GET", "/redoc"): PUBLIC,
    ("GET", "/health"): PUBLIC,

    # Records and their images.
    ("POST", "/records/"): Entry(testing=True, targets=(_b("project_id", PROJECT), _b("collection_id", COLLECTION))),
    ("GET", "/records/"): Entry(testing=True, lists="Record",
                                targets=(_q("project_id", PROJECT), _q("collection_id", COLLECTION))),
    ("GET", "/records/count"): Entry(testing=True, lists="Record",
                                     targets=(_q("project_id", PROJECT), _q("collection_id", COLLECTION))),
    ("GET", "/records/to-retake"): Entry(lists="Record"),
    ("GET", "/records/to-retake/count"): Entry(lists="Record"),
    ("GET", "/records/to-retake/summary"): Entry(lists="Record"),
    ("GET", "/records/{rec_id}"): Entry(testing=True, admin_reads_testing=True, targets=(_REC,)),
    ("PATCH", "/records/{rec_id}"): Entry(testing=True, targets=(
        _REC, _b("project_id", PROJECT), _b("collection_id", COLLECTION))),
    ("DELETE", "/records/{rec_id}"): Entry(testing=True, targets=(_REC,)),
    # capture_id names the capture the upload joins: the images and manifest lines that carry it.
    ("POST", "/records/{rec_id}/images"): Entry(testing=True, targets=(_REC, _q("capture_id", CAPTURE))),
    ("GET", "/records/{rec_id}/images"): Entry(testing=True, admin_reads_testing=True, targets=(_REC,)),
    ("GET", "/records/images/{img_id}"): Entry(testing=True, admin_reads_testing=True, targets=(_IMG,)),
    ("PATCH", "/records/images/{img_id}"): Entry(testing=True, targets=(_IMG,)),
    ("DELETE", "/records/images/{img_id}"): Entry(testing=True, targets=(_IMG,)),
    ("GET", "/records/images/{img_id}/file"): Entry(testing=True, admin_reads_testing=True, targets=(_IMG,)),
    ("GET", "/records/images/{img_id}/preview"): Entry(testing=True, admin_reads_testing=True, targets=(_IMG,)),
    ("GET", "/records/images/{img_id}/thumbnail"): Entry(testing=True, admin_reads_testing=True,
                                                         targets=(_IMG,)),
    ("GET", "/records/{rec_id}/annotations"): Entry(testing=True, admin_reads_testing=True, targets=(_REC,)),
    ("POST", "/records/{rec_id}/annotations"): Entry(targets=(_REC,)),
    ("DELETE", "/records/annotations/{annotation_id}"): Entry(targets=(_p("annotation_id", ANNOTATION),)),
    ("PATCH", "/records/{rec_id}/status"): Entry(targets=(_REC,)),
    ("POST", "/records/bulk-status"): Entry(targets=(_b("record_ids", RECORD_LIST),)),
    ("POST", "/records/{rec_id}/reject"): Entry(targets=(_REC,)),
    ("POST", "/records/{rec_id}/missing-page"): Entry(testing=True, targets=(_REC,)),
    ("POST", "/records/{rec_id}/close-missing"): Entry(testing=True, targets=(_REC,)),
    ("GET", "/records/{rec_id}/rejections"): Entry(testing=True, admin_reads_testing=True, targets=(_REC,)),

    # Cameras. The testing account reads them and captures with them; it
    # changes nothing of their configuration.
    ("GET", "/cameras/devices"): Entry(testing=True),
    ("GET", "/cameras/setup"): Entry(testing=True),
    ("PUT", "/cameras/roles"): Entry(),
    ("POST", "/cameras/rescan"): Entry(),
    ("POST", "/cameras/side/{camera_index}"): Entry(),
    ("GET", "/cameras/capabilities"): Entry(testing=True),
    ("GET", "/cameras/preview/{camera_index}"): Entry(testing=True),
    ("DELETE", "/cameras/preview/tmp"): Entry(),
    ("GET", "/cameras/focus/{camera_index}"): Entry(testing=True),
    ("POST", "/cameras/focus/{camera_index}"): Entry(),
    ("POST", "/cameras/focus-lock/{camera_index}"): Entry(),
    ("DELETE", "/cameras/focus-lock/{camera_index}"): Entry(),
    ("POST", "/cameras/settings/{camera_index}"): Entry(),
    ("POST", "/cameras/test-capture/{camera_index}"): Entry(testing=True),
    ("POST", "/cameras/capture"): Entry(testing=True, targets=(
        _b("project_name", PROJECT_NAME), _b("record_id", RECORD), _b("collection_id", COLLECTION))),
    ("POST", "/cameras/capture/dual"): Entry(testing=True, targets=(
        _b("project_name", PROJECT_NAME), _b("record_id", RECORD), _b("collection_id", COLLECTION),
        _b("after_record_id", RECORD))),
    ("POST", "/cameras/calibrate"): Entry(),
    ("POST", "/cameras/calibrate/white-balance"): Entry(),
    ("POST", "/cameras/calibrate/white-balance/manual"): Entry(),
    ("GET", "/cameras/dslr/{camera_index}/settings"): Entry(testing=True),
    ("PUT", "/cameras/dslr/{camera_index}/settings"): Entry(),
    ("PUT", "/cameras/{camera_index}/orientation"): Entry(),
    # The camera settings rows: each belongs to one image (record_image_id).
    ("POST", "/cameras/"): Entry(targets=(_b("record_image_id", IMAGE),)),
    ("GET", "/cameras/"): Entry(lists="CameraSettings"),
    ("GET", "/cameras/{id}"): Entry(targets=(_CAM_SETTINGS,)),
    ("PUT", "/cameras/settings/{id}"): Entry(targets=(_CAM_SETTINGS,)),
    ("DELETE", "/cameras/settings/{id}"): Entry(targets=(_CAM_SETTINGS,)),

    # Projects.
    ("POST", "/projects/"): Entry(testing=True),
    ("GET", "/projects/"): Entry(testing=True, lists="Project"),
    ("GET", "/projects/{project_id}"): Entry(testing=True, admin_reads_testing=True, targets=(_PROJ,)),
    # Places a record in a project outside every volume, which a testing record never is.
    ("POST", "/projects/{project_id}/add_record/{rec_id}"): Entry(targets=(_PROJ, _REC)),
    ("POST", "/projects/{project_id}/remove_record/{rec_id}"): Entry(targets=(_PROJ, _REC)),
    ("PUT", "/projects/{project_id}"): Entry(testing=True, targets=(_PROJ,)),
    ("POST", "/projects/{project_id}/move-collections"): Entry(testing=True, targets=(
        _PROJ, _b("target_project_id", PROJECT))),
    ("DELETE", "/projects/{project_id}"): Entry(testing=True, targets=(_PROJ,)),
    ("POST", "/projects/{project_id}/initialize"): Entry(targets=(_PROJ,)),
    ("GET", "/projects/{project_id}/records"): Entry(testing=True, admin_reads_testing=True, targets=(_PROJ,)),
    ("GET", "/projects/{project_id}/members"): Entry(targets=(_PROJ,)),
    ("POST", "/projects/{project_id}/members"): Entry(targets=(_PROJ,)),
    ("DELETE", "/projects/{project_id}/members/{user_id}"): Entry(targets=(_PROJ,)),

    # Volumes.
    ("POST", "/collections/"): Entry(testing=True, targets=(
        _b("project_id", PROJECT), _b("parent_collection_id", COLLECTION))),
    ("GET", "/collections/"): Entry(testing=True, lists="Collection", targets=(
        _q("project_id", PROJECT), _q("parent_collection_id", COLLECTION))),
    ("GET", "/collections/count"): Entry(testing=True, lists="Collection", targets=(
        _q("project_id", PROJECT), _q("parent_collection_id", COLLECTION))),
    ("GET", "/collections/{collection_id}"): Entry(testing=True, admin_reads_testing=True, targets=(_COL,)),
    ("GET", "/collections/{collection_id}/offsite"): Entry(targets=(_COL,)),
    ("POST", "/collections/{collection_id}/release-from-unit"): Entry(targets=(_COL,)),
    ("POST", "/collections/{collection_id}/keep-on-unit"): Entry(targets=(_COL,)),
    ("GET", "/collections/{collection_id}/offsite/free"): Entry(targets=(_COL,)),
    ("POST", "/collections/{collection_id}/offsite/free"): Entry(targets=(_COL,)),
    ("POST", "/collections/{collection_id}/offsite/restore"): Entry(targets=(_COL,)),
    ("GET", "/collections/{collection_id}/offsite/jobs/{job_id}"): Entry(targets=(_COL,)),
    ("GET", "/collections/{collection_id}/capture-manifest"): Entry(targets=(_COL,)),
    ("GET", "/collections/{collection_id}/hierarchy"): Entry(testing=True, admin_reads_testing=True,
                                                            targets=(_COL,)),
    ("PATCH", "/collections/{collection_id}"): Entry(testing=True, targets=(
        _COL, _b("parent_collection_id", COLLECTION))),
    ("POST", "/collections/{collection_id}/move-records"): Entry(testing=True, targets=(
        _COL, _q("target_collection_id", COLLECTION))),
    ("DELETE", "/collections/{collection_id}"): Entry(testing=True, targets=(_COL,)),
    ("PATCH", "/collections/{collection_id}/records/reorder"): Entry(testing=True, targets=(
        _COL, _b("ordered_ids", RECORD_LIST))),
    ("POST", "/collections/{collection_id}/records/missing-opening"): Entry(testing=True, targets=(
        _COL, _b("afterRecordId", RECORD))),
    ("POST", "/collections/{collection_id}/images/renumber"): Entry(testing=True, targets=(_COL,)),
    ("POST", "/collections/{collection_id}/promote"): Entry(targets=(_COL,)),
    ("POST", "/collections/{collection_id}/take-back"): Entry(targets=(_COL,)),
    ("GET", "/collections/{collection_id}/status-changes"): Entry(testing=True, admin_reads_testing=True,
                                                                 targets=(_COL,)),
    # The export bags approved records only, and no route takes a testing
    # record to approved: no one exports a testing volume.
    ("POST", "/collections/{collection_id}/export"): Entry(targets=(_COL,)),
    ("GET", "/collections/{collection_id}/export/status/{job_id}"): Entry(targets=(_COL,)),
    ("GET", "/collections/{collection_id}/export/download"): Entry(targets=(_COL,)),
    # The copy of a testing volume to a USB drive: the testing account and administrators,
    # who may name a testing volume here; the handler refuses a real volume to both.
    ("POST", "/collections/{collection_id}/usb-copy"): Entry(testing=True, admin_reads_testing=True,
                                                            targets=(_COL,)),
    ("GET", "/collections/{collection_id}/usb-copy/status/{job_id}"): Entry(
        testing=True, admin_reads_testing=True, targets=(_COL,)),

    # Sessions and users.
    ("GET", "/auth/setup/status"): PUBLIC,
    ("POST", "/auth/register"): PUBLIC,
    ("POST", "/auth/login"): PUBLIC,
    ("POST", "/auth/logout"): PUBLIC,
    ("POST", "/auth/refresh"): PUBLIC,
    ("POST", "/auth/password-reset"): PUBLIC,
    ("GET", "/auth/users"): Entry(),
    ("GET", "/auth/users/{user_id}"): Entry(),
    ("PATCH", "/auth/users/{user_id}/role"): Entry(),
    ("PATCH", "/auth/users/{user_id}/active"): Entry(),
    # An administrator gives another user a new password. The testing
    # account's is set through /system/testing only, and a user id names
    # no material.
    ("PUT", "/auth/users/{user_id}/password"): Entry(),
    ("DELETE", "/auth/users/{user_id}"): Entry(),
    ("GET", "/users/me"): Entry(testing=True),

    # The unit.
    ("GET", "/system/logs"): Entry(),
    ("GET", "/system/integrity"): Entry(),
    ("GET", "/system/temperature"): Entry(testing=True),
    ("GET", "/system/catalogue-backup"): Entry(),
    ("GET", "/system/unit"): Entry(testing=True),
    ("PUT", "/system/unit"): Entry(),
    ("GET", "/system/testing"): Entry(),
    ("PUT", "/system/testing"): Entry(),
    ("PATCH", "/system/testing/account"): Entry(),
    ("POST", "/system/testing/clear"): Entry(),
    # The USB drives a testing volume can be copied to.
    ("GET", "/system/testing/usb"): Entry(testing=True),
    ("GET", "/system/pairing"): Entry(),
    ("POST", "/system/pairing"): Entry(),
    ("POST", "/system/pairing/renew"): Entry(),
    ("DELETE", "/system/pairing"): Entry(),
    ("GET", "/system/storage"): Entry(),
    ("GET", "/system/storage/devices"): Entry(),
    ("POST", "/system/storage/mount"): Entry(),
    ("DELETE", "/system/storage/mount"): Entry(),
    ("POST", "/system/storage/activate"): Entry(),
    ("DELETE", "/system/storage/activate"): Entry(),
    ("POST", "/system/power"): Entry(testing=True),

    # Activity totals.
    ("GET", "/activity/collections"): Entry(lists="Collection", targets=(_q("project_id", PROJECT),)),
    ("GET", "/activity/daily"): Entry(lists="RecordImage"),

    # Digitisation lists. The listing joins each item to the volume with its stem.
    ("POST", "/imports/digitisation-list"): Entry(),
    ("GET", "/imports/digitisation-lists"): Entry(lists="Collection"),
    ("PUT", "/imports/digitisation-lists/{list_id}/items/{position}/done"): Entry(),
    ("GET", "/imports/digitisation-list/{list_id}/report"): Entry(),

    ("GET", "/fisqua/descriptions"): Entry(),

    # The network.
    ("GET", "/network/status"): Entry(testing=True),
    ("GET", "/network/wifi"): Entry(testing=True),
    ("GET", "/network/wifi/join"): Entry(testing=True),
    ("POST", "/network/wifi/join"): Entry(),
    ("DELETE", "/network/wifi/saved/{uuid}"): Entry(),
}

for _entry in ROUTES.values():
    for _target in _entry.targets:
        assert _target.where in (PATH, QUERY, BODY) and _target.kind in KINDS, _target


# The application's route objects mapped to their full path templates, built
# from the application on first use and again when a route is not found.
_FULL_PATHS: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def _full_paths(app, rebuild: bool = False) -> dict:
    from fastapi.routing import iter_route_contexts

    paths = None if rebuild else _FULL_PATHS.get(app)
    if paths is None:
        # Keyed by id(): a route object defines __eq__ and is not hashable.
        # Each value holds its route, so no id is reused while the map lives.
        paths = {}
        for context in iter_route_contexts(app.routes):
            paths[id(context.original_route)] = (context.original_route, context.path)
        _FULL_PATHS[app] = paths
    return paths


def route_key(request) -> Optional[Tuple[str, str]]:
    """(method, full path template) of the route that matched `request`, or None when no route matched.

    FastAPI puts the matched route in the scope as the route object its
    router holds, whose `path` lacks the include prefix; the full template
    is the one the application's route contexts give for that object.
    """
    route = request.scope.get("route")
    app = request.scope.get("app")
    if route is None or app is None:
        return None
    found = _full_paths(app).get(id(route))
    if found is None or found[0] is not route:
        found = _full_paths(app, rebuild=True).get(id(route))
    if found is None or found[0] is not route:
        return None
    return request.method.upper(), found[1]


def entry_for(key: Optional[Tuple[str, str]]) -> Optional[Entry]:
    """The table's entry for a (method, path) key, or None when the route has none."""
    if key is None:
        return None
    return ROUTES.get(key)
