"""The testing space (NEH-391): testing work beside the real work, on a unit linked to Fisqua.

A unit may hold testing projects and testing volumes (`projects.testing`,
`collections.testing`) and one shared testing account, the one user with
the role `testing`. What holds now, whatever route reaches them:

* `testing` is set at insert and never changes; a volume's is its
  project's, or its parent's (app.models.collection,
  _a_volume_follows_its_project_or_parent).
* A testing volume is always kept on the unit (check_collection_testing_kept,
  and the ORM guard on kept_on_unit), so none of its images is copied
  offsite and none of its manifests is sent to Fisqua (app.core.kept_volumes).
* It is never released into real work: kept_volumes.release and keep refuse
  it (`testing_volume`, 409), and the switch out of offline mode with
  `keptVolumes: include` leaves it kept. It is not one of the kept volumes
  the unit lists or counts (kept_volumes.kept_volumes, the kept counts in
  image_upload.unit_upload_summary), and it calls for no kept-volumes
  decision on that switch (unit_mode._any_kept).
* Nothing moves between testing and real work: every route that changes a
  record's volume or project, or a volume's parent or project, calls
  refuse_cross_space (`cross_space`, 409). A record in no project and no
  volume is real work, so taking one out of a testing project is a
  crossing too. POST /collections/ reads the new volume's space before
  any Fisqua lookup, and a testing volume is kept, so it is not looked up.
* Testing work has a namespace of its own: a testing project's stored
  name, and a testing volume's file name stem and reference code, begin
  with app.core.project_names.TESTING_PREFIX, and no real project's name
  or directory name, and no real volume's stem or reference code, does
  (app.models.project and app.models.collection, `name_reserved` and
  `testing_name_required`). So no testing directory under the projects
  root is ever a real project's or volume's, and the space of a capture
  manifest is read from its directory's name alone (manifest_filter). No
  caller types or reads the prefix: every response gives a project's
  display name (Project.display_name), and a name a caller sends for a
  testing project is stored with the prefix (stored_project_name). The
  export's bag and everything written to disk or sent outside keep the
  stored name. Real work never looks a testing project up by name
  (unit_mode.default_project, list_import._find_project).
* The testing account never sets or changes a volume's file name stem or
  reference code (`testing_volume_codes`, 409, POST /collections/ and
  PATCH /collections/{id}). Every testing volume's stem is given by the
  backend from the reserved series, TESTING_PREFIX and its id (series_stem),
  and it has no reference code.
* The `testing` role is in no role allowlist. The account reaches only the
  routes app.core.space_routes admits it to (the one RoleChecker in
  app.api.auth, and app.api.space.enforce_space on every router), and on
  them only testing material. Operators and reviewers never reach testing
  material, and administrators reach it by id only on the routes marked
  for it: anything else is answered 404 `not_found`, as if it did not
  exist (space_of). Every list and count route lists the caller's space
  (scope_query); an administrator lists testing material with
  `space=testing`. It is never in the operators sent to
  Fisqua (fisqua_queue.operators_body takes admins and operators only).
  The role is never given or taken away through PATCH
  /auth/users/{id}/role, and the account is not deactivated, deleted or
  given a new password through the ordinary user routes, nor made a
  project's member
  (refuse_testing_account, `testing_account`): it is managed through the
  unit's testing settings only.
* An admin manages the space and the account through GET and PUT
  /system/testing and PATCH /system/testing/account (as_api, change,
  change_account), in every unit mode. _write_account is the one writer of
  the account's row: it creates it, renames it, gives it a password and
  makes it active or inactive. Switching the space off makes the account
  inactive, and a new password or a switch-off raises its token_version,
  so app.api.auth refuses every token issued before, also once the space
  is on again; the testing projects, volumes and files stay as they are.
  None of these changes sends the unit's users to Fisqua.

Removing testing files deletes exactly the files the testing rows name and
no others (_row_files, _remove_named_files): for each image row being
removed, its master (the capture backends in capture/backends, the upload in
app.api.records.add_image_to_record), a RAW master's `_preview.jpg`
(app.core.paths.preview_sidecar_of; the CHDK and gphoto2 backends), any
other master's `.raw` raw buffer unless another row owns it
(app.core.paths.raw_sidecar_of; Picamera2Backend), the `.part` name
capture.utils.atomic_write writes each of those under, and the row's
thumbnail (app.core.thumbnail.generate_thumbnail, app.core.rebuild_volume);
and in each testing directory, the manifests capture.manifestHandler writes,
by their names (`metadata/manifest.jsonl`, `manifest.jsonl.part`,
`project_manifest.jsonl`). Removal deletes those files, which the rows and
the directory name, and nothing else: other files can be left in a testing
directory (a thumbnail written by a concurrent request, a file from a capture
whose row was never committed, a project's `packages/`), and they are left
with their directory, which is logged (`testing_purge_file_left`). Nothing is
found by listing a directory. A stored image path is written by the server
from the row's own volume or project directory, never from a client
(tests/unit/test_testing_space_access.py,
test_stored_image_paths_are_written_by_the_server_only). The testing
account's own record and image deletes unlink only what passes the same
check (testing_unlinks). Before each unlink the path must
lie inside `<known root>/<testing directory>/` with no symlink below the
root, and a symlink as the file itself is not unlinked through
(_testing_place, _unlink_named); directories go with os.rmdir only, when
empty.

Accepted limit, the clock: expiry trusts the unit's clock. A unit without
network time restores its last saved time at start, so its clock is behind,
and both the dates written and the comparison use that same clock. A clock
set far ahead by hand would make every testing volume due.

Version: 0.3.0
"""

from __future__ import annotations

from typing import Optional, Tuple

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.kept_volumes import KeepRefused
from app.core.project_names import TESTING_PREFIX, TESTING_PROJECT_PREFIX
from app.models.user import User as _User

TESTING_ROLE = "testing"

# Problem codes.
CROSS_SPACE = "cross_space"
TESTING_VOLUME = "testing_volume"
TESTING_ACCOUNT = "testing_account"
NAME_RESERVED = "name_reserved"
TESTING_NAME_REQUIRED = "testing_name_required"
TESTING_VOLUME_CODES = "testing_volume_codes"
ACCOUNT_REQUIRED = "account_required"
SPACE_OFF = "space_off"
USERNAME_TAKEN = "username_taken"
NO_TESTING_ACCOUNT = "no_testing_account"
ACCOUNT_EXISTS = "account_exists"
PASSWORD_EMPTY = "password_empty"
PASSWORD_INVALID = "password_invalid"
TESTING_RESERVE = "testing_reserve"

# The bounds PUT /system/testing accepts for testing_expiry_days.
MIN_EXPIRY_DAYS = 1
MAX_EXPIRY_DAYS = 365
# The bounds PUT /system/testing accepts for testing_reserve_gb, and the bytes in one of its GB.
MIN_RESERVE_GB = 1
MAX_RESERVE_GB = 10000
BYTES_PER_GB = 10 ** 9
# The longest username the testing account may have: 150, or users.username's length if shorter.
USERNAME_MAX_LENGTH = min(150, _User.__table__.c.username.type.length or 150)

CROSS_SPACE_MESSAGE = (
    "One side of this move is testing work and the other is real work. Testing and real work "
    "are kept apart: nothing moves between them."
)
TESTING_VOLUME_MESSAGE = (
    "This is a testing volume. Captua always keeps testing volumes on the unit and never "
    "releases them into real work."
)
NAME_RESERVED_MESSAGE = (
    f"Names that begin with {TESTING_PREFIX}, in any capitals or accents, are kept for the testing space. "
    "Real work cannot use one."
)
TESTING_NAME_REQUIRED_MESSAGE = (
    f"A testing project's name begins with {TESTING_PROJECT_PREFIX}, and a testing volume's file name stem "
    f"and reference code with {TESTING_PREFIX}."
)
TESTING_VOLUME_CODES_MESSAGE = (
    "The testing account does not set or change a volume's file name stem or reference code."
)
# The message of each namespace code (namespace_problem).
NAMESPACE_MESSAGES = {NAME_RESERVED: NAME_RESERVED_MESSAGE, TESTING_NAME_REQUIRED: TESTING_NAME_REQUIRED_MESSAGE}
TESTING_ACCOUNT_MESSAGE = (
    "This is the unit's testing account. It is managed only through the unit's testing settings."
)

ACCOUNT_REQUIRED_MESSAGE = (
    "The unit has no testing account yet. Give a username and a password to create it with the space."
)
SPACE_OFF_MESSAGE = (
    "The testing space is being switched off. The testing account's username and password are changed "
    "only while it is on, or through the account settings."
)
USERNAME_TAKEN_MESSAGE = "Another user already has this username."
NO_TESTING_ACCOUNT_MESSAGE = "The unit has no testing account. Switch the testing space on to create it."
ACCOUNT_EXISTS_MESSAGE = "Another testing account was created meanwhile. A unit has only one."
PASSWORD_EMPTY_MESSAGE = "The testing account's password cannot be empty."
PASSWORD_INVALID_MESSAGE = "The testing account's password holds characters that cannot be stored."
TESTING_RESERVE_MESSAGE = (
    "The unit's storage is nearly full, and the space left is kept for real work. Testing captures "
    "are refused until an administrator frees space or lowers the reserve."
)

# (project_id, collection_id): where a record sits, or a volume's new place.
Place = Tuple[Optional[int], Optional[int]]


class SpaceRefused(KeepRefused):
    """A write refused because it would join testing and real work: an HTTP status and a {code, message} detail.

    A KeepRefused, so the routes that already answer the kept volumes'
    refusals answer these the same way.
    """


def is_testing(db: Session, *, project_id: Optional[int] = None, collection_id: Optional[int] = None) -> bool:
    """Whether a place is testing work: the volume's flag when there is a volume, else the project's.

    A place with neither, or an id with no row, is real work.
    Read without a lock: `testing` never changes after insert.
    """
    from app.models.collection import Collection
    from app.models.project import Project

    if collection_id is not None:
        return bool(db.query(Collection.testing).filter(Collection.id == collection_id).scalar())
    if project_id is not None:
        return bool(db.query(Project.testing).filter(Project.id == project_id).scalar())
    return False


def refuse_cross_space(db: Session, source: Place, target: Place) -> None:
    """SpaceRefused (409, cross_space) for a move between testing and real work, in either direction.

    `source` and `target` are (project_id, collection_id): where a record
    is and where it goes, or a volume itself (None, its id) and its new
    parent (None, parent id) or new project (project id, None). Called
    before the kept-volume checks, so a move out of a testing volume is
    named for what it is.
    """
    if is_testing(db, project_id=source[0], collection_id=source[1]) != is_testing(
            db, project_id=target[0], collection_id=target[1]):
        raise SpaceRefused(409, CROSS_SPACE, CROSS_SPACE_MESSAGE)


def refuse_testing_volume(collection) -> None:
    """SpaceRefused (409, testing_volume) when `collection` is a testing volume."""
    if collection is not None and collection.testing:
        raise SpaceRefused(409, TESTING_VOLUME, TESTING_VOLUME_MESSAGE)


def namespace_problem(testing: bool, value, project: bool = False) -> Optional[str]:
    """The code refusing `value` as a name, stem or reference code of work in this space, or None.

    For testing work (`testing`), a value that does not begin with
    TESTING_PREFIX, or for a project's name (`project`) with
    TESTING_PROJECT_PREFIX, is `testing_name_required`; for real work, a
    value in the reserved namespace (project_names.in_reserved_namespace,
    which reads the directory name too, and covers both prefixes) is
    `name_reserved`. None passes.
    """
    from app.core.project_names import in_reserved_namespace

    if value is None:
        return None
    if testing:
        prefix = TESTING_PROJECT_PREFIX if project else TESTING_PREFIX
        return None if isinstance(value, str) and value.startswith(prefix) else TESTING_NAME_REQUIRED
    return NAME_RESERVED if in_reserved_namespace(value) else None


def refuse_namespace(testing: bool, *values, project: bool = False) -> None:
    """SpaceRefused (409, name_reserved or testing_name_required) for the first of `values` namespace_problem refuses."""
    for value in values:
        code = namespace_problem(testing, value, project=project)
        if code is not None:
            raise SpaceRefused(409, code, NAMESPACE_MESSAGES[code])


def display_name_max_length() -> int:
    """The longest display name a testing project takes: projects.name's length less the project prefix's."""
    from app.models.project import Project

    return Project.__table__.c.name.type.length - len(TESTING_PROJECT_PREFIX)


def stored_project_name(testing: bool, name):
    """The name a project of this space is stored under, for the name a caller sends.

    A testing project's is the name with TESTING_PROJECT_PREFIX before it,
    whatever the name holds; a real project's is the name as sent. The
    inverse of project_names.display_project_name.
    """
    if testing and isinstance(name, str):
        return TESTING_PROJECT_PREFIX + name
    return name


def series_stem(volume_id: int) -> str:
    """A testing volume's file name stem: TESTING_PREFIX and the volume's id, zero-padded to six digits.

    Wider when the id has more digits. Given by POST /collections/ in the
    transaction that inserts the volume, so every testing volume has its
    stem before anything can capture into it; the model's guards keep it
    from changing once the volume has captured. With the prefix's 15
    characters, ids of up to 12 digits fit FILENAME_STEM_MAX_LENGTH (27),
    and the stem matches FILENAME_STEM_PATTERN. It never begins with
    TESTING_PROJECT_PREFIX, so no testing project's directory is a stem
    (app.core.project_names, TESTING_PROJECT_PREFIX). A testing volume has no
    reference code: external_id stays null, which the model allows beside
    a stem.
    """
    return f"{TESTING_PREFIX}{int(volume_id):06d}"


def refuse_testing_volume_codes(*values) -> None:
    """SpaceRefused (409, testing_volume_codes) when any of `values` (a stem, a reference code) is set."""
    if any(value is not None for value in values):
        raise SpaceRefused(409, TESTING_VOLUME_CODES, TESTING_VOLUME_CODES_MESSAGE)


def refuse_testing_account(user, status: int = 409) -> None:
    """SpaceRefused (`status`, testing_account) when `user` is the testing account.

    For the ordinary user routes: the role change, activation, deletion
    (409, an admin acting on the account) and the password reset (403, the
    account acting on itself).
    """
    if user is not None and user.role == TESTING_ROLE:
        raise SpaceRefused(status, TESTING_ACCOUNT, TESTING_ACCOUNT_MESSAGE)


# ---------------------------------------------------------------------------
# The space's settings and its account (GET/PUT /system/testing, PATCH /system/testing/account)
# ---------------------------------------------------------------------------

def testing_account(db: Session):
    """The unit's testing account, the one user with the testing role, or None."""
    from app.models.user import User

    return db.query(User).filter(User.role == TESTING_ROLE).first()


def free_bytes() -> Optional[int]:
    """Free bytes on the file system that holds the active projects root, or None when they cannot be read.

    Read as GET /system/storage reads them (storage_override.projects_root_usage).
    """
    from app.core.storage_override import projects_root_usage

    usage, _problem = projects_root_usage()
    return None if usage is None else usage.free


def free_gb(free: Optional[int] = None, *, read: bool = True) -> Optional[float]:
    """Free space in GB of 10^9 bytes, rounded down to one decimal; None when it cannot be read.

    Rounded down, so the figure shown never claims more than there is; the
    reserve is compared in bytes (refuse_below_reserve). `free` is used
    when given; otherwise, with `read`, it is read now (free_bytes).
    """
    if free is None and read:
        free = free_bytes()
    return None if free is None else (free // (BYTES_PER_GB // 10)) / 10


def refuse_below_reserve(db: Session) -> None:
    """HTTPException 507 `testing_reserve` when free space on the active projects root is below the reserve.

    Free space that cannot be read is refused the same way. The detail
    carries `free_gb` (null when unreadable) and `reserve_gb`; the message
    names no figure. The routes that write files for testing material call
    it for the testing account before any camera is touched, any number
    taken or any file written: POST /cameras/capture and /cameras/capture/dual,
    POST /records/{id}/images and POST /collections/. Real work is never
    passed to it. The thumbnail GET is not checked: it rewrites a small file
    derived from an image already stored.

    The reserve is the larger of the testing reserve and the storage reserve
    (settings.STORAGE_RESERVE_GB, app.core.storage_override), so the testing
    account never writes into space real work is refused; the code stays
    `testing_reserve` and `reserve_gb` is that larger figure.
    """
    from fastapi import HTTPException
    from app.core import unit_mode
    from app.core.config import settings

    reserve_gb = max(unit_mode.testing_settings(db).reserve_gb, max(0, int(settings.STORAGE_RESERVE_GB)))
    free = free_bytes()
    if free is not None and free >= reserve_gb * BYTES_PER_GB:
        return
    raise HTTPException(status_code=507, detail={
        "code": TESTING_RESERVE, "message": TESTING_RESERVE_MESSAGE,
        "free_gb": free_gb(free, read=False), "reserve_gb": reserve_gb,
    })


def as_api(db: Session) -> dict:
    """The GET /system/testing body. Never carries the password or its hash."""
    from app.core import unit_mode
    from app.models.collection import Collection
    from app.models.project import Project

    settings = unit_mode.testing_settings(db)
    account = testing_account(db)
    return {
        "enabled": settings.enabled,
        "username": account.username if account is not None else None,
        "account_active": bool(account.is_active) if account is not None else False,
        "expiry_days": settings.expiry_days,
        "reserve_gb": settings.reserve_gb,
        "free_gb": free_gb(),
        "projects": db.query(Project).filter(Project.testing.is_(True)).count(),
        "volumes": db.query(Collection).filter(Collection.testing.is_(True)).count(),
    }


def _log(db: Session, category: str, action: str, actor: str, subject: Optional[str], detail: str,
         level: str = "INFO") -> None:
    """A system_logs row added to `db`, committed with the change it records."""
    from app.models.system_log import SystemLog

    db.add(SystemLog(level=level, category=category, action=action, actor=actor[:150] if actor else None,
                     subject=subject[:300] if subject else None, detail=detail[:500]))


def _hashed(password: Optional[str]) -> Optional[str]:
    """The password's hash by app.core.security.hash_password, as POST /auth/register stores it; None for None.

    SpaceRefused (422, password_empty) for an empty password, and (422,
    password_invalid) for one that cannot be encoded as UTF-8, such as a
    lone surrogate; neither message carries the value. Called before
    the settings row is locked, since hashing takes long on the unit and the
    lock holds back the creation of volumes.
    """
    from app.core.security import hash_password

    if password is None:
        return None
    if password == "":
        raise SpaceRefused(422, PASSWORD_EMPTY, PASSWORD_EMPTY_MESSAGE)
    try:
        password.encode("utf-8")
    except UnicodeEncodeError:
        raise SpaceRefused(422, PASSWORD_INVALID, PASSWORD_INVALID_MESSAGE) from None
    return hash_password(password)


def _write_account(db: Session, account, *, actor: str, username: Optional[str] = None,
                   hashed_password: Optional[str] = None, active: Optional[bool] = None):
    """Create the testing account (when `account` is None) or change it; flushed, not committed.

    The one writer of the account's row. A new account needs `username`
    and `hashed_password` (_hashed), and is active. A new password, and a
    change from active to inactive, each raise the account's token_version
    by one, so every token issued before is refused (app.api.auth); the raise
    is one UPDATE that adds one to the stored value (app.api.auth.end_sessions),
    in the transaction of the password or the flag, and the account's
    token_version is read again afterwards, so no flush writes an older
    value back and raises made at the same time all count; a new
    username and a return to active leave it. A username another user
    holds is SpaceRefused (409, username_taken); a second testing account
    that committed after `account` was read is refused by
    uq_users_one_testing_account and answered SpaceRefused (409,
    account_exists) after a rollback. Logs `testing_account_created`, or
    `testing_account_changed` for a new username or password, the password
    named only as "password"; making the account active or inactive is
    logged with the space's change by the caller.
    """
    from app.models.user import User

    if username is not None:
        holder = db.query(User).filter(User.username == username).first()
        if holder is not None and (account is None or holder.id != account.id):
            db.rollback()
            raise SpaceRefused(409, USERNAME_TAKEN, USERNAME_TAKEN_MESSAGE)
    created = account is None
    changes = []
    if created:
        account = User(username=username, hashed_password=hashed_password,
                       role=TESTING_ROLE, is_active=True, token_version=0)
        db.add(account)
    else:
        ends_sessions = False
        if username is not None and username != account.username:
            changes.append(f"username {account.username} -> {username}")
            account.username = username
        if hashed_password is not None:
            changes.append("password")
            account.hashed_password = hashed_password
            ends_sessions = True
        if active is not None:
            if account.is_active and not active:
                ends_sessions = True
            account.is_active = bool(active)
        if ends_sessions:
            from app.api.auth import end_sessions
            end_sessions(db, account.id)
            db.expire(account, ["token_version"])
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        if username is not None and db.query(User).filter(User.username == username).first() is not None:
            raise SpaceRefused(409, USERNAME_TAKEN, USERNAME_TAKEN_MESSAGE)
        raise SpaceRefused(409, ACCOUNT_EXISTS, ACCOUNT_EXISTS_MESSAGE)
    if created:
        _log(db, "access", "testing_account_created", actor, account.username, "role testing, active")
    elif changes:
        _log(db, "access", "testing_account_changed", actor, account.username, "; ".join(changes))
    return account


def change(db: Session, *, actor: str, enabled: bool, username: Optional[str] = None,
           password: Optional[str] = None, expiry_days: Optional[int] = None,
           reserve_gb: Optional[int] = None) -> None:
    """Apply PUT /system/testing; commits, or raises SpaceRefused with nothing changed.

    Switching on creates the account when there is none (`username` and
    `password` both needed, else 422 account_required), or makes the
    existing one active and applies the `username` and `password` sent;
    a new password ends the account's sessions. Switching off makes the
    account inactive and ends its sessions, which stay ended when the
    space is switched on again, and leaves the testing material as it is;
    credentials sent with it are 422 space_off. An empty password is 422
    password_empty. A new `expiry_days` is stored and committed with the
    rest; then every testing volume's testing_expires_at no transaction
    holds is shifted by the difference, in a statement of its own that
    waits for no lock (_shift_expiry says what holds for the dates). `reserve_gb` is stored as given. The
    password is hashed first, then the settings row is locked
    (unit_mode.lock_testing_settings). Logs `testing_space_changed` when
    `enabled`, `expiry_days` or `reserve_gb` changes.
    """
    from app.core import unit_mode

    if not enabled and (username is not None or password is not None):
        raise SpaceRefused(422, SPACE_OFF, SPACE_OFF_MESSAGE)
    hashed_password = _hashed(password)
    before = unit_mode.lock_testing_settings(db)
    account = testing_account(db)
    if enabled and account is None and (username is None or hashed_password is None):
        db.rollback()
        raise SpaceRefused(422, ACCOUNT_REQUIRED, ACCOUNT_REQUIRED_MESSAGE)
    if enabled:
        account = _write_account(db, account, actor=actor, username=username,
                                 hashed_password=hashed_password, active=True)
    elif account is not None:
        account = _write_account(db, account, actor=actor, active=False)
    expiry = before.expiry_days if expiry_days is None else expiry_days
    reserve = before.reserve_gb if reserve_gb is None else reserve_gb
    unit_mode.store_testing_settings(db, actor=actor, enabled=enabled,
                                     user_id=account.id if account is not None else before.user_id,
                                     expiry_days=expiry, reserve_gb=reserve)
    changes = []
    if before.enabled != bool(enabled):
        changes.append(f"enabled {before.enabled} -> {bool(enabled)}")
    if before.expiry_days != expiry:
        changes.append(f"expiry_days {before.expiry_days} -> {expiry}")
    if before.reserve_gb != reserve:
        changes.append(f"reserve_gb {before.reserve_gb} -> {reserve}")
    if changes:
        _log(db, "system", "testing_space_changed", actor, None, "; ".join(changes))
    db.commit()
    # After the commit, so the settings row is never held while volumes are
    # waited for: the shift waits for no lock (_shift_expiry).
    if expiry != before.expiry_days:
        _shift_expiry(db, expiry - before.expiry_days)
        db.commit()


def change_account(db: Session, *, actor: str, username: Optional[str] = None,
                   password: Optional[str] = None) -> None:
    """Apply PATCH /system/testing/account: a new username, a new password, or both; commits.

    SpaceRefused (404, no_testing_account) when the unit has none, and
    (422, password_empty) for an empty password. A new password ends the
    account's sessions: every token issued before is refused. A new
    username leaves them. The account's active flag and the space's
    settings are left as they are. The password is hashed first, then the
    settings row is locked.
    """
    from app.core import unit_mode

    hashed_password = _hashed(password)
    unit_mode.lock_testing_settings(db)
    account = testing_account(db)
    if account is None:
        db.rollback()
        raise SpaceRefused(404, NO_TESTING_ACCOUNT, NO_TESTING_ACCOUNT_MESSAGE)
    _write_account(db, account, actor=actor, username=username, hashed_password=hashed_password)
    db.commit()


# ---------------------------------------------------------------------------
# Who reaches which space (app.core.space_routes, app.api.space.enforce_space)
# ---------------------------------------------------------------------------

NOT_FOUND = "not_found"
NOT_FOUND_MESSAGE = "Not found."
SPACE_ADMIN_ONLY = "space_admin_only"
SPACE_ADMIN_ONLY_MESSAGE = "Only an administrator lists the testing space's material."

# The value of the `space` query parameter on the list and count routes.
TESTING_SPACE = "testing"


def not_found_detail() -> dict:
    """The detail of the 404 that answers material in a space the caller does not reach."""
    return {"code": NOT_FOUND, "message": NOT_FOUND_MESSAGE}


def _project_space(db: Session, project_id) -> Optional[bool]:
    from app.models.project import Project

    row = db.query(Project.testing).filter(Project.id == project_id).first()
    return None if row is None else bool(row.testing)


def _project_name_space(db: Session, name, testing_caller: bool = False) -> Optional[bool]:
    """The space of the project a caller names by name.

    The testing account sends a display name, which names the testing
    project stored under stored_project_name(True, name): testing, or None
    when there is none; it never names a real project. Anyone else sends
    the name as stored, compared exactly as the capture routes compare it:
    a real project's name, or None. A name only a testing project is
    stored under (one that begins with the prefix) names testing work, so
    it is refused like any other testing target rather than left to a
    handler that would find the testing project by it.
    """
    from app.models.project import Project

    if testing_caller:
        row = (db.query(Project.testing)
               .filter(Project.name == stored_project_name(True, name), Project.testing.is_(True)).first())
    else:
        row = db.query(Project.testing).filter(Project.name == name).first()
    return None if row is None else bool(row.testing)


def _collection_space(db: Session, collection_id) -> Optional[bool]:
    from app.models.collection import Collection

    row = db.query(Collection.testing).filter(Collection.id == collection_id).first()
    return None if row is None else bool(row.testing)


def _record_space(db: Session, record_id) -> Optional[bool]:
    """A record's space: its volume's when it is in one, else its project's, else real work."""
    from app.models.record import Record

    row = db.query(Record.project_id, Record.collection_id).filter(Record.id == record_id).first()
    if row is None:
        return None
    return is_testing(db, project_id=row.project_id, collection_id=row.collection_id)


def _image_space(db: Session, image_id) -> Optional[bool]:
    from app.models.record import RecordImage

    row = db.query(RecordImage.record_id).filter(RecordImage.id == image_id).first()
    return None if row is None else _record_space(db, row.record_id)


def _annotation_space(db: Session, annotation_id) -> Optional[bool]:
    from app.models.record import RecordAnnotation

    row = db.query(RecordAnnotation.record_id).filter(RecordAnnotation.id == annotation_id).first()
    return None if row is None else _record_space(db, row.record_id)


def _camera_settings_space(db: Session, settings_id) -> Optional[bool]:
    from app.models.camera import CameraSettings

    row = db.query(CameraSettings.record_image_id).filter(CameraSettings.id == settings_id).first()
    return None if row is None else _image_space(db, row.record_image_id)


def manifest_space(manifest, projects_dir) -> bool:
    """Whether a capture manifest is the testing space's: read from its directory's name alone.

    The directory is the first part, under the projects root, of the
    directory the manifest's paths are relative to
    (app.core.integrity.manifest_base): a project's directory, a stemmed
    volume's `<stem>/`, or, for a retired manifest under
    `.retired-manifests/`, the `<dir>` its file name carries. It is the
    testing space's when its name begins with the prefix
    (project_names.is_testing_directory): a testing project's directory
    and a testing volume's stem do, and no real one does. Every other
    manifest is real work's, one in a directory no row names included.
    """
    from pathlib import Path

    from app.core.integrity import manifest_base
    from app.core.project_names import is_testing_directory

    root = Path(projects_dir)
    base = manifest_base(Path(manifest), root)
    try:
        parts = base.relative_to(root).parts
    except ValueError:
        return False
    return bool(parts) and is_testing_directory(parts[0])


def manifest_filter(db, testing: bool, projects_dir):
    """include(manifest) for the manifests read on behalf of work in one space: those of that space only.

    A manifest's space is manifest_space, read from its directory's name;
    nothing is read from the database, and `db` is not used. Used by the
    export's bag manifest and by every fixity check made for one volume or
    one space (app.core.integrity.build_fixity_index).
    """
    want = bool(testing)

    def include(manifest) -> bool:
        return manifest_space(manifest, projects_dir) is want

    return include


def manifest_filter_for_images(images, projects_dir):
    """manifest_filter for the space of `images` (one volume's or one move's), or None when there are none.

    The images' space is read from the database (their record's volume or
    project); the manifests' from their directories' names.
    """
    from sqlalchemy.orm import object_session

    images = [img for img in images if img is not None]
    if not images:
        return None
    db = object_session(images[0])
    if db is None:
        return None
    return manifest_filter(db, bool(_record_space(db, images[0].record_id)), projects_dir)


def _capture_spaces(db: Session, capture_id) -> frozenset:
    """The spaces of the images that carry a capture id, from the database only.

    No manifest is read on a request: a testing capture line that no image
    carries is kept out of real work by the export's and the fixity
    checks' manifest filter (manifest_filter).
    """
    from app.models.record import RecordImage

    spaces = set()
    for (record_id,) in db.query(RecordImage.record_id).filter(RecordImage.capture_id == capture_id).all():
        space = _record_space(db, record_id)
        if space is not None:
            spaces.add(space)
    return frozenset(spaces)


_RESOLVERS = {
    "project": _project_space,
    "project_name": _project_name_space,
    "collection": _collection_space,
    "record": _record_space,
    "image": _image_space,
    "annotation": _annotation_space,
    "camera_settings": _camera_settings_space,
}


def space_of(db: Session, kind: str, value, testing_caller: bool = False) -> Optional[bool]:
    """Whether the material a target names is testing work (True) or real work (False); None when there is no such row.

    One resolver per kind: a project and a volume by their own flag, a
    project named by its exact name as the capture routes find it, a
    record by is_testing over its volume or project, an image and an
    annotation by their record, a camera settings row by its image. A
    record list is resolved one record at a time by the caller. Read
    without a lock: `testing` never changes after insert, and a record or
    image that moves keeps its space (refuse_cross_space). A project name
    is read in the namespace of the caller (`testing_caller` for the
    testing account; _project_name_space).
    """
    if kind == "project_name":
        return _project_name_space(db, value, testing_caller)
    return _RESOLVERS[kind](db, value)


def spaces_of(db: Session, kind: str, value, testing_caller: bool = False) -> frozenset:
    """The spaces of the material a target names: empty when it names nothing.

    One space for every kind space_of resolves; a capture id (kind
    `capture`) is in the space of every image that carries it, so it can be
    in both.
    """
    if kind == "capture":
        return _capture_spaces(db, value)
    space = space_of(db, kind, value, testing_caller)
    return frozenset() if space is None else frozenset({space})


def _record_testing(record):
    """SQL truth of "this record is testing work", for `record` (the model or an alias); never NULL."""
    from sqlalchemy import and_, exists, or_
    from sqlalchemy.orm import aliased
    from app.models.collection import Collection
    from app.models.project import Project

    # Aliases, so a query that already joins collections or projects is not
    # correlated with them.
    volume = aliased(Collection)
    project = aliased(Project)
    return or_(
        and_(record.collection_id.isnot(None),
             exists().where(volume.id == record.collection_id, volume.testing.is_(True))),
        and_(record.collection_id.is_(None),
             exists().where(project.id == record.project_id, project.testing.is_(True))),
    )


def _image_testing(image):
    from sqlalchemy import exists
    from sqlalchemy.orm import aliased
    from app.models.record import Record

    record = aliased(Record)
    return exists().where(record.id == image.record_id, _record_testing(record))


def testing_clause(model):
    """SQL truth of "this row is testing work" for a row of `model`, as space_of decides it; never NULL.

    `model` is Project, Collection, Record, RecordImage, CameraSettings or
    RecordStatusChange. A status change belongs to its record's space
    while the record exists, and to the space of the volume stored on it
    once the record is deleted.
    """
    from sqlalchemy import and_, exists, or_
    from sqlalchemy.orm import aliased
    from app.models.camera import CameraSettings
    from app.models.collection import Collection
    from app.models.project import Project
    from app.models.record import Record, RecordImage, RecordStatusChange

    if model is Project or model is Collection:
        return model.testing.is_(True)
    if model is Record:
        return _record_testing(Record)
    if model is RecordImage:
        return _image_testing(RecordImage)
    if model is CameraSettings:
        image = aliased(RecordImage)
        return exists().where(image.id == CameraSettings.record_image_id, _image_testing(image))
    if model is RecordStatusChange:
        record = aliased(Record)
        volume = aliased(Collection)
        return or_(
            and_(RecordStatusChange.record_id.isnot(None),
                 exists().where(record.id == RecordStatusChange.record_id, _record_testing(record))),
            and_(RecordStatusChange.record_id.is_(None),
                 exists().where(volume.id == RecordStatusChange.collection_id, volume.testing.is_(True))),
        )
    raise ValueError(f"no testing clause for {model!r}")


def lists_testing(user, space: Optional[str]) -> bool:
    """Whether a list or count route lists testing rows for `user`, who asked for `space`.

    The testing account lists testing rows only; an operator and a reviewer
    real rows only; an administrator real rows, or testing rows when it
    asks with `space=testing`. `space=testing` from anyone but an
    administrator is a 403 (`space_admin_only`).
    """
    from fastapi import HTTPException

    if space == TESTING_SPACE and user.role != "admin":
        raise HTTPException(status_code=403, detail={"code": SPACE_ADMIN_ONLY, "message": SPACE_ADMIN_ONLY_MESSAGE})
    return user.role == TESTING_ROLE or space == TESTING_SPACE


def scope_query(query, model, user, space: Optional[str]):
    """`query` filtered to the rows of `model` in the space `user` lists (lists_testing).

    Used by every list and count route over projects, volumes, records,
    images, camera settings rows and status changes; the filter is a
    correlated EXISTS on `model`, so `model` must be in the query's FROM.
    """
    from sqlalchemy import not_

    clause = testing_clause(model)
    return query.filter(clause if lists_testing(user, space) else not_(clause))


def space_query_param():
    """The `space` query parameter of a list or count route: absent, or `testing`."""
    from fastapi import Query

    return Query(None, pattern=f"^{TESTING_SPACE}$",
                 description="`testing` lists the testing space's material; an administrator only.")


# ---------------------------------------------------------------------------
# Expiry, purge and clearing
# ---------------------------------------------------------------------------
#
# A testing volume's testing_expires_at is its last capture plus the term
# (the testing settings' expiry_days, app.core.unit_mode), stored as naive UTC, as the unit's
# settings and the outbox store their times. It is set when the volume is
# created (POST /collections/), and at every capture event or upload into
# it (app.core.capture_naming.take_filename_index, which every path that
# adds an image passes through, under the volume's row lock), on the volume
# and on every ancestor. A new term shifts every stored date by the
# difference, in the change's transaction (change).
#
# A purge (purge_volume) takes the rows it removes with FOR UPDATE SKIP
# LOCKED (_lock_free): the volume and its sub-volumes, as a capture takes
# its volume (app.core.volume_stem.locked_volume_query), and their records,
# as a retake or an upload takes its record. A row a capture holds is not
# waited for: the purge leaves the whole volume for a later round. A
# capture that arrives while a purge holds the rows waits for the purge's
# commit, then finds no volume: 404. The files go first, then the rows in
# one transaction: a crash between the two leaves every row, so the next
# run finds the volume again and completes it, and a crash during the
# files leaves rows that name files already gone, which the next run
# removes the same way. The reverse order would leave, after a crash,
# directories that no row names, which nothing would find again.

import logging as _logging
import threading as _threading
from datetime import datetime as _datetime, timedelta as _timedelta, timezone as _timezone

from app.core.scheduler_control import StoppableWorker as _StoppableWorker

_logger = _logging.getLogger(__name__)

# The hourly run (TestingExpiryScheduler): how long it waits after start-up, then between runs.
PURGE_START_DELAY_SECONDS = 300
PURGE_INTERVAL_SECONDS = 3600

# One purge run at a time: the scheduler's tick and POST /system/testing/clear.
_purge_lock = _threading.Lock()


# The testing_expires_at a purge writes on every volume it is removing, committed before any file
# goes: a volume carrying it takes no capture or upload (refuse_volume_being_removed), no refresh or
# new term moves it, and the next run or clear completes its removal (purge_volume).
REMOVING_AT = _datetime(1970, 1, 1)

TESTING_VOLUME_BUSY = "testing_volume_busy"
TESTING_VOLUME_BUSY_MESSAGE = "A capture is writing to this volume. Try again once it has finished."
TESTING_VOLUME_COPYING_MESSAGE = "This volume is being copied to a USB drive. Try again once the copy has finished."
TESTING_RECORD_NEEDS_VOLUME = "testing_record_needs_volume"
TESTING_RECORD_NEEDS_VOLUME_MESSAGE = "A testing record always belongs to a testing volume."


class NotTestingWork(RuntimeError):
    """A purge asked to remove something that is not testing work. Nothing is removed."""


def _now() -> _datetime:
    return _datetime.now(_timezone.utc).replace(tzinfo=None)


def _naive(value: Optional[_datetime]) -> Optional[_datetime]:
    if value is not None and value.tzinfo is not None:
        return value.astimezone(_timezone.utc).replace(tzinfo=None)
    return value


def refuse_real_work(volume) -> None:
    """NotTestingWork unless `volume` is a testing volume whose stem lies in the testing namespace.

    Read from the row and its stem both, so neither alone lets a purge
    reach a real volume. Raised, never asserted, so no interpreter flag
    removes it.
    """
    stem = getattr(volume, "filename_stem", None)
    if volume is None or getattr(volume, "testing", None) is not True or not isinstance(stem, str) \
            or not stem.startswith(TESTING_PREFIX):
        raise NotTestingWork(f"volume {getattr(volume, 'id', None)} is not testing work; nothing is removed")


def _refuse_real_project(project) -> None:
    name = getattr(project, "name", None)
    if project is None or getattr(project, "testing", None) is not True or not isinstance(name, str) \
            or not name.startswith(TESTING_PROJECT_PREFIX):
        raise NotTestingWork(f"project {getattr(project, 'id', None)} is not testing work; nothing is removed")


def effective_expiry(volume, term: _timedelta) -> _datetime:
    """When a testing volume is due to be removed: its testing_expires_at, or with none, its creation plus `term`.

    Both are read as naive UTC: an aware value is converted to UTC, and a
    naive one, as SQLite and a PostgreSQL `timestamp` column return it, is
    taken to be UTC already.
    """
    when = _naive(volume.testing_expires_at)
    if when is not None:
        return when
    return _naive(volume.created_at) + term


def refuse_volume_being_removed(volume) -> None:
    """HTTPException 404 `not_found` for a testing volume a purge has marked (REMOVING_AT): it is going."""
    from fastapi import HTTPException

    if volume is not None and volume.testing and _naive(volume.testing_expires_at) == REMOVING_AT:
        raise HTTPException(status_code=404, detail=not_found_detail())


def refuse_joining_a_marked_tree(db: Session, parent_id) -> None:
    """HTTPException 404 `not_found` when `parent_id`, or any of its ancestors, is a volume a purge has marked.

    Called where POST /collections/ and PATCH /collections/{id} hold the
    parent's row, so nothing joins a tree that is being removed.
    """
    from fastapi import HTTPException
    from app.models.collection import Collection

    seen = set()
    current = parent_id
    while current is not None and current not in seen:
        seen.add(current)
        row = db.query(Collection.testing, Collection.testing_expires_at, Collection.parent_collection_id).filter(
            Collection.id == current).first()
        if row is None:
            return
        if row.testing and _naive(row.testing_expires_at) == REMOVING_AT:
            raise HTTPException(status_code=404, detail=not_found_detail())
        current = row.parent_collection_id


def refuse_record_without_volume(collection_id) -> None:
    """HTTPException 400 `testing_record_needs_volume` when a testing account's record would be in no volume."""
    from fastapi import HTTPException

    if collection_id is None:
        raise HTTPException(status_code=400, detail={"code": TESTING_RECORD_NEEDS_VOLUME,
                                                     "message": TESTING_RECORD_NEEDS_VOLUME_MESSAGE})


def expires_at(db: Session, now: Optional[_datetime] = None) -> _datetime:
    """`now` (the present when not given) plus the term, as testing_expires_at stores it."""
    from app.core import unit_mode

    return (_naive(now) or _now()) + _timedelta(days=unit_mode.testing_settings(db).expiry_days)


def refresh_expiry(db: Session, collection_id: Optional[int]) -> None:
    """A capture into `collection_id`: its testing_expires_at and every ancestor's set to now plus the term.

    Nothing for a real volume. Added to the caller's transaction; the
    caller holds the volume's row lock, and the ancestors' rows are taken
    by the update. A volume a purge has marked (REMOVING_AT) keeps its mark.
    """
    from sqlalchemy import or_
    from app.models.collection import Collection

    if collection_id is None:
        return
    row = db.query(Collection.testing, Collection.parent_collection_id).filter(
        Collection.id == collection_id).first()
    if row is None or not row.testing:
        return
    ids, parent, seen = [collection_id], row.parent_collection_id, {collection_id}
    while parent is not None and parent not in seen:
        seen.add(parent)
        ids.append(parent)
        parent = db.query(Collection.parent_collection_id).filter(Collection.id == parent).scalar()
    db.query(Collection).filter(
        Collection.id.in_(ids), Collection.testing.is_(True),
        or_(Collection.testing_expires_at.is_(None), Collection.testing_expires_at != REMOVING_AT),
    ).update({Collection.testing_expires_at: expires_at(db)}, synchronize_session=False)


def _shift_statement(days: int, dialect: str):
    """The UPDATE moving every testing volume's date by `days`, on the volumes it can lock at once.

    The rows are chosen by a subquery FOR UPDATE SKIP LOCKED on PostgreSQL
    (SQLite takes no row locks), so the statement never waits: a volume a
    capture or a purge holds is left as it is. A volume a purge has marked
    (REMOVING_AT) is never moved.
    """
    from sqlalchemy import func, select, update
    from app.models.collection import Collection

    column = Collection.testing_expires_at
    chosen = (select(Collection.id)
              .where(Collection.testing.is_(True), column.isnot(None), column != REMOVING_AT)
              .with_for_update(skip_locked=True))
    shifted = func.datetime(column, f"{days:+d} days") if dialect == "sqlite" else column + _timedelta(days=days)
    return (update(Collection)
            .where(Collection.id.in_(chosen), column.isnot(None), column != REMOVING_AT)
            .values(testing_expires_at=shifted)
            .execution_options(synchronize_session=False))


def _shift_expiry(db: Session, days: int) -> None:
    """Every testing volume's date moved by `days`, in a transaction of its own that holds no settings row.

    Called by change() after the new term is committed. What holds for the
    dates after a term change: every testing volume that no transaction
    held when the statement ran is moved by the difference, so it is its
    last capture plus the new term. A volume a purge held is going. A
    volume a capture held keeps the date that capture writes: the
    capture's refresh (refresh_expiry) reads the term when it runs, without
    a lock, so a refresh that ran after the term's commit wrote last
    capture plus the new term, and one that ran before it wrote last
    capture plus the old term, which stays until the volume's next capture.
    A capture whose refresh read the new term and that committed in the
    moment between the term's commit and this statement is moved once more,
    by the difference again; that window is one request's two statements.
    """
    if not days:
        return
    db.execute(_shift_statement(days, db.get_bind().dialect.name))


def _lock_query(db: Session, model, ids):
    """The rows of `model` with these ids, FOR UPDATE SKIP LOCKED: a row another transaction holds is left out."""
    return db.query(model.id).filter(model.id.in_(list(ids))).with_for_update(skip_locked=True)


def _lock_free(db: Session, model, ids) -> set:
    """The ids of `ids` whose rows this transaction now holds; those another transaction holds are missing."""
    ids = list(ids)
    if not ids:
        return set()
    return {row[0] for row in _lock_query(db, model, ids).all()}


def _subtree(db: Session, volume_id: int) -> list:
    """The volume's id and every descendant's, the volume first."""
    from app.models.collection import Collection

    found, frontier = [volume_id], [volume_id]
    while frontier:
        children = [i for (i,) in db.query(Collection.id).filter(
            Collection.parent_collection_id.in_(frontier)).all() if i not in found]
        found.extend(children)
        frontier = children
    return found


def _purge_hook(stage: str, db: Session, ids) -> None:
    """Called by purge_volume at "before_lock" (its first read of the tree done) and "after_mark" (the mark committed). Does nothing."""


# How many times a lock pass reads the tree again before purge_volume leaves it for a later run.
MAX_LOCK_ROUNDS = 5


def _lock_tree(db: Session, root_id: int, stage: Optional[str]):
    """Lock a volume's tree and its records; (volume ids, record ids) read and locked in one pass, or None.

    The tree is read, locked (SKIP LOCKED), and read again under the locks;
    a tree that changed in between is locked again with its new set, up to
    MAX_LOCK_ROUNDS times. None, after a rollback, when a row is held or the
    tree keeps changing. A record is added to a volume only under that
    volume's row (app.core.record_sequence, the capture paths), so the
    records read once the volumes are held are all of them.
    """
    from app.models.collection import Collection
    from app.models.record import Record

    ids = _subtree(db, root_id)
    if stage is not None:
        _purge_hook(stage, db, ids)
    for _ in range(MAX_LOCK_ROUNDS):
        if _lock_free(db, Collection, ids) != set(ids):
            db.rollback()
            return None
        again = _subtree(db, root_id)
        if set(again) != set(ids):
            ids = again
            continue
        record_ids = [i for (i,) in db.query(Record.id).filter(Record.collection_id.in_(ids)).all()]
        if _lock_free(db, Record, record_ids) != set(record_ids):
            db.rollback()
            return None
        return ids, record_ids
    db.rollback()
    return None


def _outside_child(db: Session, ids) -> bool:
    """Whether a volume outside `ids` has its parent inside them: deleting `ids` would cascade to it."""
    from app.models.collection import Collection

    ids = list(ids)
    return db.query(Collection.id).filter(
        Collection.parent_collection_id.in_(ids), Collection.id.notin_(ids)).first() is not None


def _known_roots() -> list:
    """Every projects root the unit knows, as (path, problem): the default root, and the override root when one is set.

    `problem` is None for a root that can be read now, and the reason it
    cannot otherwise: an override root override_root_problem refuses (a
    drive that is not mounted), or a root that exists and cannot be listed.
    A default root that does not exist holds nothing and has no problem.
    """
    import os
    from pathlib import Path
    from app.core import storage_override
    from app.core.config import settings

    found = []
    default = Path(settings.default_projects_dir)
    try:
        if default.exists():
            os.listdir(default)
        found.append((default, None))
    except OSError as exc:
        found.append((default, f"the default projects root cannot be read ({type(exc).__name__})"))
    override, _corrupt = storage_override.get_storage_override_or_fallback()
    if override and Path(override) != default:
        root = Path(override)
        problem = storage_override.override_root_problem(root)
        if problem is None:
            try:
                os.listdir(root)
            except OSError as exc:
                problem = f"the storage root cannot be read ({type(exc).__name__})"
        found.append((root, problem))
    return found


# The files a testing directory holds by a known relative name, beside its images' own files: the
# manifests capture.manifestHandler writes (`manifest.jsonl`, its `manifest.jsonl.part` while a write
# is in progress, and a project's `project_manifest.jsonl`).
_METADATA_FILES = ("metadata/manifest.jsonl", "metadata/manifest.jsonl.part", "metadata/project_manifest.jsonl")
# The directories capture.project_manager creates in a volume's `<stem>/` (app.core.paths.VOLUME_SUBDIRS)
# and in a project's directory (the name-based layout), removed with os.rmdir when empty.
_KNOWN_SUBDIRS = ("captures", "previews", "thumbnails", "metadata", "images/main", "images/thumbnails", "images")


def _part_of(path):
    """The temporary name capture.utils.atomic_write writes under before it renames: `<stem>.part<suffix>`."""
    return path.with_name(f"{path.stem}.part{path.suffix}")


def _row_files(db: Session, rows, removed_masters) -> list:
    """Every file the capture and derivative code writes for these image rows, by name; nothing is listed from disk.

    For each row (file_path, thumbnail_path):
    - the master, written by the capture backends (capture.service, the
      CHDK, gphoto2 and Picamera2 backends) or by the upload
      (app.api.records.add_image_to_record);
    - for a RAW master, its `<stem>_preview.jpg` (app.core.paths.preview_sidecar_of),
      written by the CHDK and gphoto2 backends;
    - for any other master, its `<stem>.raw` raw buffer (app.core.paths.raw_sidecar_of),
      written by Picamera2Backend, unless a row not being removed has that
      file as its own master (app.core.paths.raw_buffer_of's rule);
    - the `.part` name capture.utils.atomic_write gives each of those three
      while it writes, which a crash can leave;
    - its thumbnail, as stored on the row (app.core.thumbnail.generate_thumbnail,
      app.core.rebuild_volume).
    """
    from pathlib import Path
    from app.core.paths import is_raw_suffix, preview_sidecar_of, raw_sidecar_of, same_file
    from app.core.storage_ops import raw_buffer_candidate_names, rows_master_paths_named

    removed = {str(m) for m in removed_masters}
    files = []
    for file_path, thumbnail_path in rows:
        if file_path:
            master = Path(file_path)
            files += [master, _part_of(master)]
            if is_raw_suffix(master):
                preview = preview_sidecar_of(master)
                files += [preview, _part_of(preview)]
            else:
                buffer = raw_sidecar_of(master)
                owners = [m for m in rows_master_paths_named(db, raw_buffer_candidate_names(buffer))
                          if str(m) not in removed]
                if not any(same_file(buffer, m) for m in owners):
                    files += [buffer, _part_of(buffer)]
        if thumbnail_path:
            files.append(Path(thumbnail_path))
    return files


def _testing_place(path, roots):
    """Where a path lies: (normalised path, its testing directory, None), or (path, directory or None, why it is refused).

    The one positive check before an unlink or an rmdir: the path lies, as
    written, inside `<known root>/<a directory in the testing namespace>/`
    for a readable root of _known_roots, and no component between that root
    and the path's parent is a symlink (os.path.realpath of the parent
    equals the root's real path joined with the same parts).
    """
    import os
    from app.core.project_names import is_testing_directory

    norm = os.path.normpath(os.path.abspath(str(path)))
    best = None
    for root, problem in roots:
        if problem is not None:
            continue
        r = os.path.normpath(os.path.abspath(str(root)))
        if norm.startswith(r + os.sep) and (best is None or len(r) > len(best)):
            best = r
    if best is None:
        return norm, None, "outside every projects root"
    parts = os.path.relpath(norm, best).split(os.sep)
    if not is_testing_directory(parts[0]):
        return norm, None, "outside every testing directory"
    directory = os.path.join(best, parts[0])
    parent = os.path.dirname(norm)
    if os.path.realpath(parent) != os.path.join(os.path.realpath(best), *parts[:-1]):
        return norm, directory, "a symlink lies in its path"
    return norm, directory, None


def _unlink_named(path, roots, left: list):
    """Unlink one named file when it passes _testing_place and is not itself a symlink; the testing directory, or None.

    A file that is absent is gone. A refused one is appended to `left` with
    its reason and kept. Raises OSError for an unlink that fails.
    """
    import os
    import stat

    norm, directory, refusal = _testing_place(path, roots)
    try:
        st = os.lstat(norm)
    except FileNotFoundError:
        return directory if refusal is None else None
    if refusal is not None:
        left.append(f"{norm}: {refusal}")
        return None
    if stat.S_ISLNK(st.st_mode):
        left.append(f"{norm}: a symlink, not followed")
        return None
    if stat.S_ISDIR(st.st_mode):
        left.append(f"{norm}: a directory where a file is named")
        return None
    os.unlink(norm)
    return directory


def _rmdir_up(directories, deleted_parents, roots, left: list) -> None:
    """Remove, with os.rmdir only and bottom-up, the known subdirectories and the parents of deleted files, then each testing directory.

    A directory is removed only when it passes _testing_place (the testing
    directory itself: its parent is a known root), is not a symlink, and is
    empty. A testing directory that is still there afterwards is appended
    to `left`.
    """
    import os

    candidates = set()
    for directory in directories:
        for sub in _KNOWN_SUBDIRS:
            candidates.add(os.path.join(directory, sub))
    for parent in deleted_parents:
        current = parent
        while any(current.startswith(d + os.sep) for d in directories):
            candidates.add(current)
            current = os.path.dirname(current)
    for candidate in sorted(candidates, key=lambda p: p.count(os.sep), reverse=True):
        _norm, _directory, refusal = _testing_place(os.path.join(candidate, "x"), roots)
        if refusal is not None or os.path.islink(candidate) or not os.path.isdir(candidate):
            continue
        try:
            os.rmdir(candidate)
        except OSError:
            pass
    for directory in directories:
        if os.path.islink(directory):
            left.append(f"{directory}: a symlink, not followed")
            continue
        try:
            os.rmdir(directory)
        except FileNotFoundError:
            pass
        except OSError:
            pass
        if os.path.lexists(directory):
            left.append(f"{directory}: not empty, left")


def _remove_named_files(db: Session, directory_names, image_ids):
    """Remove the files the rows name and the testing directories' known files; the paths left, or None.

    None, with nothing removed, when a known projects root cannot be read:
    the caller deletes no row. Otherwise every file of _row_files for the
    image rows, and each directory's _METADATA_FILES on every readable
    known root, is unlinked when it passes _unlink_named; the directories
    are then removed when empty (_rmdir_up). The answer lists every path
    left: a file refused by the check, and a testing directory left
    non-empty. Raises OSError for a removal that fails.
    """
    import os
    from pathlib import Path
    from app.core.project_names import is_testing_directory
    from app.models.record import RecordImage

    roots = _known_roots()
    if any(problem is not None for _root, problem in roots):
        return None
    image_ids = list(image_ids)
    rows = (db.query(RecordImage.file_path, RecordImage.thumbnail_path)
            .filter(RecordImage.id.in_(image_ids)).all()) if image_ids else []
    left: list = []
    directories = set()
    deleted_parents = set()
    named = _row_files(db, rows, [fp for fp, _ in rows if fp])
    for name in directory_names:
        if not is_testing_directory(name):
            left.append(f"{name}: not a testing directory, left")
            continue
        for root, _problem in roots:
            base = Path(root) / name
            directories.add(os.path.normpath(os.path.abspath(str(base))))
            named += [base / rel for rel in _METADATA_FILES]
    for path in named:
        directory = _unlink_named(path, roots, left)
        if directory is not None:
            directories.add(directory)
            deleted_parents.add(os.path.dirname(os.path.normpath(os.path.abspath(str(path)))))
    _rmdir_up(sorted(directories), deleted_parents, roots, left)
    return left


def _log_left(db: Session, left: list) -> None:
    for detail in left:
        _log(db, "system", "testing_purge_file_left", "system", detail[:300], detail, level="WARN")


def remove_project_directory(db: Session, project_name: str):
    """A testing project's directory on every known root: its manifests by name, then the directory when empty.

    The paths left (_remove_named_files), or None when a known projects root
    cannot be read. Not committed; DELETE /projects/{id} logs what is left.
    """
    from app.core.paths import project_dir_name
    from app.core.project_names import is_testing_directory

    name = project_dir_name(project_name)
    return _remove_named_files(db, [name] if name and is_testing_directory(name) else [], [])


def testing_unlinks(db: Session, paths) -> list:
    """The files of a testing row's delete that pass the purge's check; each other existing one is left and logged.

    For DELETE /records/{id} and DELETE /records/images/{id} by the testing
    account: a path is kept for the unlink only when it lies inside a
    testing directory of a readable known root with no symlink below the
    root and is not itself a symlink (_testing_place). A refused file that
    exists writes one `testing_purge_file_left` row, added to `db` and
    committed with the delete.
    """
    import os
    from pathlib import Path

    roots = _known_roots()
    kept, left = [], []
    for path in paths:
        norm, _directory, refusal = _testing_place(path, roots)
        if not os.path.lexists(norm):
            continue
        if refusal is None and os.path.islink(norm):
            refusal = "a symlink, not followed"
        if refusal is not None:
            left.append(f"{norm}: {refusal}")
            continue
        kept.append(Path(norm))
    _log_left(db, left)
    return kept


def log_files_left(db: Session, left: list) -> None:
    """One `testing_purge_file_left` row (WARN) per path left; added to `db`, committed with the caller's change."""
    _log_left(db, left)


def _remove_volume_files(db: Session, *, stems, image_ids):
    """_remove_named_files for a volume tree: its stems' directories and its images' files."""
    return _remove_named_files(db, stems, image_ids)


def _delete_volume_rows(db: Session, volume_ids, record_ids, image_ids) -> None:
    """Every row of these volumes, records and images, children first; not committed."""
    from sqlalchemy import or_
    from app.models.camera import CameraSettings
    from app.models.collection import Collection
    from app.models.fisqua import FisquaOutbox, FisquaStatus
    from app.models.offsite_object import OffsiteObject
    from app.models.record import (ExifData, Record, RecordAnnotation, RecordImage, RecordRejection,
                                   RecordStatusChange)

    volume_ids, record_ids, image_ids = list(volume_ids), list(record_ids), list(image_ids)
    if image_ids:
        for model in (CameraSettings, ExifData, OffsiteObject):
            db.query(model).filter(model.record_image_id.in_(image_ids)).delete(synchronize_session=False)
        db.query(RecordImage).filter(RecordImage.id.in_(image_ids)).delete(synchronize_session=False)
    if record_ids:
        for model in (RecordAnnotation, RecordRejection):
            db.query(model).filter(model.record_id.in_(record_ids)).delete(synchronize_session=False)
    db.query(RecordStatusChange).filter(or_(
        RecordStatusChange.record_id.in_(record_ids), RecordStatusChange.collection_id.in_(volume_ids),
    )).delete(synchronize_session=False)
    if record_ids:
        db.query(Record).filter(Record.id.in_(record_ids)).delete(synchronize_session=False)
    for model in (FisquaOutbox, FisquaStatus):
        db.query(model).filter(model.collection_id.in_(volume_ids)).delete(synchronize_session=False)
    db.query(Collection).filter(Collection.id.in_(volume_ids), Collection.testing.is_(True)).delete(
        synchronize_session=False)


# Why purge_volume is called: the hourly expiry reads the dates again under the locks; a clear and the
# testing account's own delete remove the volume whatever its date.
FOR_EXPIRY = "expiry"
FOR_CLEAR = "clear"
FOR_DELETE = "delete"


def _tree_due(db: Session, ids, now: _datetime) -> bool:
    """Whether a locked tree is still due: marked already, or every volume's effective_expiry past `now`, read now."""
    from app.core import unit_mode
    from app.models.collection import Collection

    term = _timedelta(days=unit_mode.testing_settings(db).expiry_days)
    volumes = db.query(Collection).filter(Collection.id.in_(list(ids))).populate_existing().all()
    if any(_naive(v.testing_expires_at) == REMOVING_AT for v in volumes):
        return True
    return all(effective_expiry(v, term) < now for v in volumes)


def purge_volume(db: Session, volume, *, reason: str = FOR_CLEAR, now: Optional[_datetime] = None) -> bool:
    """Remove one testing volume completely: its sub-volumes, records, images and their rows, its files, its rows.

    NotTestingWork, before anything is read or removed, for a volume that
    is not testing work. Answers True once it is gone, and False, with no
    row removed, when it is left for a later run: a capture holds a row of
    its tree, the tree keeps changing, a known projects root cannot be read
    or a file cannot be removed, a USB copy is reading a volume of the tree
    (app.core.usb_copy), or, for `reason` FOR_EXPIRY, the tree is no longer
    due once its locks are held. From the first lock pass to its end it
    holds the tree with export_jobs.hold_for_file_changes, so no USB copy of
    it starts meanwhile.

    The rows removed are the volumes' records and, from those,
    record_images, camera_settings, exif_data, offsite_objects,
    record_annotations, record_rejections and record_status_changes (also
    those that name the volumes once their record is gone), and the
    volumes' fisqua_outbox and fisqua_status rows. On every projects root
    the unit knows, each image's files (_row_files) and each `<stem>/`'s
    manifests are unlinked when they pass the check (_testing_place), the
    manifest removed and never retired, and the directories removed when
    empty. A file refused by the check, and a directory left non-empty, are
    logged `testing_purge_file_left` and do not hold the rows.

    In order: the tree is read and locked, and read again under the locks
    (_lock_tree); for expiry its dates are read again (_tree_due); every
    volume's testing_expires_at is set to REMOVING_AT and that alone
    committed; the tree is read and locked again; the files go; the rows go,
    in one commit, and only for the set read and locked in that pass, and
    only when no volume outside it has its parent inside it. A crash after
    the mark leaves every row, each volume marked REMOVING_AT, and part or
    none of the files: such a volume takes no capture, upload or new
    sub-volume (404), keeps its mark, and is removed by the next expiry run
    or clear.
    """
    refuse_real_work(volume)
    from contextlib import ExitStack

    with ExitStack() as holds:
        return _purge_volume(db, volume, reason, now, holds)


def _hold_against_copies(holds, ids) -> bool:
    """export_jobs.hold_for_file_changes on `ids`, kept until `holds` closes; False when a USB copy holds one of them.

    While it is held no USB copy of these volumes starts (export_jobs.hold_for_copy).
    """
    from fastapi import HTTPException
    from app.core.export_jobs import USB_COPY_MESSAGE, export_jobs

    try:
        holds.enter_context(export_jobs.hold_for_file_changes(list(ids), USB_COPY_MESSAGE))
    except HTTPException:
        return False
    return True


def copy_running_in(db: Session, volume_id: int) -> bool:
    """Whether a USB copy holds this volume or one of its sub-volumes now."""
    from app.core.export_jobs import export_jobs

    return export_jobs.copying(_subtree(db, volume_id))


def _purge_volume(db: Session, volume, reason: str, now, holds) -> bool:
    """purge_volume's work, with `holds` open for its holds against USB copies."""
    from app.models.collection import Collection
    from app.models.record import RecordImage

    root_id = volume.id
    locked = _lock_tree(db, root_id, "before_lock")
    if locked is None:
        return False
    ids, _records = locked
    # A tree a USB copy is reading is left; from here no copy of it starts.
    if not _hold_against_copies(holds, ids):
        db.rollback()
        return False
    for row in db.query(Collection).filter(Collection.id.in_(ids)).populate_existing().all():
        refuse_real_work(row)
    if reason == FOR_EXPIRY and not _tree_due(db, ids, _naive(now) or _now()):
        db.rollback()
        return False
    # The mark, committed alone before any file goes: from here the volumes
    # take nothing new, and a crash leaves them to the next run.
    db.query(Collection).filter(Collection.id.in_(ids), Collection.testing.is_(True)).update(
        {Collection.testing_expires_at: REMOVING_AT}, synchronize_session=False)
    db.commit()
    _purge_hook("after_mark", db, ids)

    locked = _lock_tree(db, root_id, None)
    if locked is None:
        return False
    ids, record_ids = locked
    if not _hold_against_copies(holds, ids):
        db.rollback()
        return False
    volumes = db.query(Collection).filter(Collection.id.in_(ids)).populate_existing().all()
    for row in volumes:
        refuse_real_work(row)
    if _outside_child(db, ids):
        db.rollback()
        _logger.error("Testing volume %s has a sub-volume outside the locked tree; it is left", root_id)
        return False
    image_ids = [i for (i,) in db.query(RecordImage.id).filter(RecordImage.record_id.in_(record_ids)).all()] \
        if record_ids else []
    left = _remove_volume_files(db, stems=[row.filename_stem for row in volumes], image_ids=image_ids)
    if left is None:
        db.rollback()
        return False
    _purge_hook("after_files", db, ids)
    if _outside_child(db, ids):
        db.rollback()
        _logger.error("Testing volume %s has a sub-volume outside the locked tree; it is left", root_id)
        return False
    _log_left(db, left)
    _delete_volume_rows(db, ids, record_ids, image_ids)
    db.commit()
    db.expire_all()
    return True


def _purge_project(db: Session, project) -> bool:
    """Remove a testing project that holds no volume: its records in no volume, its directory, manifest included, its row.

    False, with nothing removed, when it holds a volume or a record in one,
    a transaction holds its row or one of its records, a known projects
    root cannot be read, or a file cannot be removed. Its records' files and
    its directory's manifests are removed by name, and the directory removed
    when empty, on every projects root the unit knows (_remove_named_files). A
    testing project is never the unit's default project
    (unit_mode.stored_default_project passes it over), and the default
    project's foreign key is set null on delete.
    """
    from pathlib import Path
    from app.core.paths import project_dir_name
    from app.core.project_names import is_testing_directory
    from app.models.collection import Collection
    from app.models.project import Project
    from app.models.project_member import ProjectMember
    from app.models.record import Record, RecordImage

    _refuse_real_project(project)
    if _lock_free(db, Project, [project.id]) != {project.id}:
        db.rollback()
        return False
    if db.query(Collection.id).filter(Collection.project_id == project.id).first() is not None \
            or db.query(Record.id).filter(Record.project_id == project.id,
                                          Record.collection_id.isnot(None)).first() is not None:
        db.rollback()
        return False
    # Its records in no volume (written before every testing record had to
    # be in one, or directly) go with it, images and files included.
    record_ids = [i for (i,) in db.query(Record.id).filter(
        Record.project_id == project.id, Record.collection_id.is_(None)).all()]
    if _lock_free(db, Record, record_ids) != set(record_ids):
        db.rollback()
        return False
    image_ids = [i for (i,) in db.query(RecordImage.id).filter(RecordImage.record_id.in_(record_ids)).all()] \
        if record_ids else []
    directory_name = project_dir_name(project.name)
    names = [directory_name] if directory_name and is_testing_directory(directory_name) else []
    left = _remove_named_files(db, names, image_ids)
    if left is None:
        db.rollback()
        return False
    _log_left(db, left)
    _delete_volume_rows(db, [], record_ids, image_ids)
    db.query(ProjectMember).filter(ProjectMember.project_id == project.id).delete(synchronize_session=False)
    db.query(Project).filter(Project.id == project.id, Project.testing.is_(True)).delete(synchronize_session=False)
    db.commit()
    db.expire_all()
    return True


def _record_failure(failures: list, kind: str, item_id, exc: BaseException) -> None:
    _logger.error("Removing testing %s %s failed (%s); it is left for the next run", kind, item_id,
                  type(exc).__name__, exc_info=exc)
    failures.append(f"{kind} {item_id}: {type(exc).__name__}")


def _log_failures(db: Session, failures: list) -> None:
    for detail in failures:
        _log(db, "system", "testing_purge_item_failed", "system", None, detail, level="ERR")


def _still_due(db: Session, volume_id: int, now: _datetime) -> bool:
    """Whether a volume expiry left behind is still due (counted as skipped), rather than given a later date."""
    from app.core import unit_mode
    from app.models.collection import Collection

    db.rollback()
    row = db.query(Collection).filter(Collection.id == volume_id).populate_existing().first()
    if row is None:
        return False
    term = _timedelta(days=unit_mode.testing_settings(db).expiry_days)
    return effective_expiry(row, term) < now


def _purge_volumes(db: Session, roots: list, sizes: dict, failures: list, *, reason: str,
                   now: Optional[_datetime] = None) -> tuple:
    """purge_volume for each of `roots`, each in its own try; (volumes removed, volumes left)."""
    from app.models.collection import Collection

    removed = left = 0
    for volume_id in roots:
        try:
            row = db.get(Collection, volume_id)
            if row is None:
                continue
            done = purge_volume(db, row, reason=reason, now=now)
        except NotTestingWork:
            db.rollback()
            raise
        except Exception as exc:
            db.rollback()
            _record_failure(failures, "volume", volume_id, exc)
            left += sizes[volume_id]
            continue
        if done:
            removed += sizes[volume_id]
        elif reason != FOR_EXPIRY or _still_due(db, volume_id, now):
            left += sizes[volume_id]
    return removed, left


def _purge_projects(db: Session, projects: list, failures: list) -> tuple:
    """_purge_project for each of `projects`, each in its own try; (projects removed, failures counted)."""
    removed = failed = 0
    for project in projects:
        project_id = project.id
        try:
            if _purge_project(db, project):
                removed += 1
        except NotTestingWork:
            db.rollback()
            raise
        except Exception as exc:
            db.rollback()
            _record_failure(failures, "project", project_id, exc)
            failed += 1
    return removed, failed


def _empty_testing_projects(db: Session, created_before: Optional[_datetime] = None) -> list:
    from app.models.collection import Collection
    from app.models.project import Project
    from app.models.record import Record

    found = []
    for project in db.query(Project).filter(Project.testing.is_(True)).order_by(Project.id).all():
        if created_before is not None and not (_naive(project.created_at) < created_before):
            continue
        if db.query(Collection.id).filter(Collection.project_id == project.id).first() is not None:
            continue
        if db.query(Record.id).filter(Record.project_id == project.id,
                                      Record.collection_id.isnot(None)).first() is not None:
            continue
        found.append(project)
    return found


def _volume_tree(db: Session, term: Optional[_timedelta] = None) -> tuple:
    """Every testing volume as {id: (parent id, effective_expiry)}, and {id: [child ids]}; no dates without `term`."""
    from app.models.collection import Collection

    rows = {r.id: (r.parent_collection_id, effective_expiry(r, term) if term is not None else None)
            for r in db.query(Collection.id, Collection.parent_collection_id, Collection.testing_expires_at,
                              Collection.created_at).filter(Collection.testing.is_(True)).all()}
    children = {i: [] for i in rows}
    for i, (parent, _) in rows.items():
        if parent in children:
            children[parent].append(i)
    return rows, children


def _size(children: dict, volume_id: int) -> int:
    return 1 + sum(_size(children, c) for c in children.get(volume_id, []))


def _log_unreadable_roots(db: Session) -> None:
    """One system-log row per known projects root that cannot be read now, once per run."""
    for root, problem in _known_roots():
        if problem is not None:
            _log(db, "system", "testing_purge_root_unreadable", "system", str(root)[:300],
                 f"{root}: {problem}; the testing material on it is left for a later run", level="WARN")


def _purge_expired(db: Session, now: _datetime) -> dict:
    from app.core import unit_mode

    term = _timedelta(days=unit_mode.testing_settings(db).expiry_days)
    rows, children = _volume_tree(db, term)
    memo = {}

    def expired(volume_id) -> bool:
        # A volume a purge has marked goes with all it holds; any other goes
        # once its date and every descendant's are past.
        if volume_id not in memo:
            when = rows[volume_id][1]
            memo[volume_id] = when == REMOVING_AT or (
                when < now and all(expired(c) for c in children[volume_id]))
        return memo[volume_id]

    gone = {i for i in rows if expired(i)}
    roots = sorted(i for i in gone if rows[i][0] not in gone)
    db.rollback()
    failures = []
    removed, left = _purge_volumes(db, roots, {i: _size(children, i) for i in roots}, failures,
                                   reason=FOR_EXPIRY, now=now)
    projects, failed = _purge_projects(db, _empty_testing_projects(db, created_before=now - term), failures)
    result = {"volumes": removed, "projects": projects, "skipped": left + failed}
    _log_unreadable_roots(db)
    _log_failures(db, failures)
    if removed or projects or left or failed:
        _log(db, "system", "testing_expired_purged", "system", None,
             f"volumes {removed}; projects {projects}; skipped {left + failed}")
    db.commit()
    return result


def purge_expired(db: Session, now: Optional[_datetime] = None) -> dict:
    """Remove the expired testing volumes and the old empty testing projects; {volumes, projects, skipped}.

    A volume goes once its own date and every descendant's are past `now`,
    with its sub-volumes (a parent with a live child stays); a volume's date
    is its testing_expires_at, or with none its creation plus the term
    (effective_expiry, in Python). A volume a purge has marked goes
    whatever its sub-volumes' dates. The dates are read again under the
    locks (purge_volume, FOR_EXPIRY): a volume a capture moved forward
    meanwhile stays and is not counted. Then every testing project that
    holds no volume and no record in one and was created more than the
    term ago goes, with its records in no volume and its directory. Each
    volume tree and each project is removed in its own try: a failure is
    logged (`testing_purge_item_failed`, with the item's id and the error's
    class), counted in `skipped`, and the run goes on. `skipped` also
    counts the volumes left because a capture held them or a projects root
    could not be read. Runs whether the space is on or off. Idempotent.
    {"status": "busy"}, with nothing done, while another run or a clear is
    under way.
    """
    if not _purge_lock.acquire(blocking=False):
        return {"status": "busy"}
    try:
        return _purge_expired(db, _naive(now) or _now())
    finally:
        _purge_lock.release()


class PurgeRunning(Exception):
    """A clear asked for while a scheduled run holds the purge lock."""


TESTING_PURGE_RUNNING = "testing_purge_running"
TESTING_PURGE_RUNNING_MESSAGE = "Expired testing material is being removed now. Try again in a moment."


def clear(db: Session, *, actor: str) -> dict:
    """POST /system/testing/clear: every testing volume, then every testing project, whatever its date; committed.

    Answers {projects, volumes, skipped}: what was removed, and what was
    left: the volumes a capture held (a held sub-volume leaves its whole
    top-level volume, and every volume in it is counted), the volumes left
    while a projects root cannot be read, and each volume tree or project
    whose removal failed (logged `testing_purge_item_failed`). A project
    that still holds a volume, or a record in one, is left; its records in
    no volume go with it. The testing account and the settings stay. Always logged
    `testing_space_cleared`. PurgeRunning, with nothing done, while a
    scheduled run holds the purge lock.
    """
    if not _purge_lock.acquire(blocking=False):
        raise PurgeRunning()
    try:
        failures = []
        removed = left = projects = failed = 0
        try:
            rows, children = _volume_tree(db)
            roots = sorted(i for i, (parent, _) in rows.items() if parent not in rows)
            db.rollback()
            removed, left = _purge_volumes(db, roots, {i: _size(children, i) for i in roots}, failures,
                                           reason=FOR_CLEAR)
            projects, failed = _purge_projects(db, _empty_testing_projects(db), failures)
        finally:
            db.rollback()
            _log_unreadable_roots(db)
            _log_failures(db, failures)
            _log(db, "system", "testing_space_cleared", actor, None,
                 f"volumes {removed}; projects {projects}; skipped {left + failed}")
            db.commit()
        return {"projects": projects, "volumes": removed, "skipped": left + failed}
    finally:
        _purge_lock.release()


class TestingExpiryScheduler(_StoppableWorker):
    """The hourly run of purge_expired, in a daemon thread, as the unit's other schedulers run.

    Started and stopped by the application's lifespan (app.main), whether
    the space is on or off; tests never enter the lifespan, so it never
    runs under them unless a test starts it.
    """

    def __init__(self) -> None:
        self._stop = _threading.Event()
        self._thread: Optional[_threading.Thread] = None

    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = _threading.Thread(target=self._loop, name="testing-expiry", daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        # A worker still alive is kept, so worker_alive() never reports a live thread as gone.
        if thread is not None and not thread.is_alive() and self._thread is thread:
            self._thread = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _loop(self) -> None:
        if self._stop.wait(PURGE_START_DELAY_SECONDS):
            return
        while True:
            self.tick()
            if self._stop.wait(PURGE_INTERVAL_SECONDS):
                return

    def tick(self) -> Optional[dict]:
        """One run on its own session. A failure is logged to the system log and answered None."""
        from app.core.db import SessionLocal

        db = SessionLocal()
        try:
            return purge_expired(db, _now())
        except Exception as exc:
            _logger.exception("The scheduled removal of expired testing material raised")
            db.rollback()
            try:
                _log(db, "system", "testing_purge_failed", "system", None, f"{type(exc).__name__}: {exc}", level="ERR")
                db.commit()
            except Exception:
                db.rollback()
                _logger.exception("The failure of the testing material removal could not be logged")
            return None
        finally:
            db.close()


# Module-level singleton, as for the other schedulers: one backend process.
testing_expiry_scheduler = TestingExpiryScheduler()
