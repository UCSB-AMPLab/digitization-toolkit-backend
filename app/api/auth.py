# Version: 0.3.0

import hmac
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Security, Query, Header, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from typing import List, Optional

from app.api.deps import get_db_dependency
from app.models.user import User
from app.schemas.user import UserCreate, UserLogin, UserRead, UserRoleUpdate, PasswordReset, PasswordResetRequest, PasswordSet, TokenRefresh
from app.core.security import hash_password, verify_password, create_access_token, verify_access_token, needs_rehash
from app.core.config import settings, is_dev_env
from app.core.login_throttle import login_throttle
from app.core.audit import log_event
from app.core.fisqua_queue import notify_operators_changed

logger = logging.getLogger(__name__)

router = APIRouter()
users_router = APIRouter()  # mounted at /users in main.py
# auto_error=False on every HTTPBearer instance so the status code for a
# missing/malformed Authorization header is chosen by our code, not by the
# framework default (which has varied across FastAPI versions). A credential
# problem is always a 401 ("session problem"), never a 403 ("authorization
# answer"); each dependency below raises HTTPException(401, ...) explicitly.
# See NEH-167.
_optional_bearer = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------------------
# Session cookie (NEH-61)
#
# The browser app authenticates with an httpOnly cookie that holds the same
# signed token login issues. No page script can read the cookie, and no URL
# carries the token: the browser attaches the cookie itself to fetch calls,
# <img src> requests and download links. API clients and the e2e scripts
# authenticate with `Authorization: Bearer`; when a request carries both, the
# backend uses the header and ignores the cookie. The backend reads no
# `?token=` query parameter anywhere.
#
# CSRF: with SameSite=Strict, the browser leaves the cookie off any request
# that a cross-site page starts. In addition, a cookie-authenticated request
# with a state-changing method must carry an X-Requested-With header. A
# cross-site page can add a custom header only through a CORS preflight, and
# CORSMiddleware answers the preflight only for the origins in CORS_ORIGINS,
# so a forged form post or a no-cors fetch from another site cannot include
# it. The backend checks this header rather than Origin because the allowed
# origins differ per unit (the unit's host name or IP, localhost in the
# kiosk, the dev server), and an Origin check would have to repeat CORS_ORIGINS exactly.
# Bearer requests skip the check: a browser never adds an Authorization header
# on its own.
#
# Tokens are signed, not stored. A token carries its user's session
# generation (`ver`, users.token_version; 0 when absent), and every route
# that reads a token to a user refuses one whose `ver` is not the user's
# current value (_token_is_current). Raising token_version ends every
# session of that user. Logout raises it for every role but testing
# (end_sessions), set_user_active when it deactivates a user, and
# reset_password with the user's own new password, set_user_password with
# the new password an administrator gives another user, each in one UPDATE
# that adds one in the database; app.core.testing_space raises the testing
# account's when it deactivates it or gives it a new password.
# ---------------------------------------------------------------------------

SESSION_COOKIE_NAME = "dtk_session"
CSRF_HEADER = "X-Requested-With"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _request_is_https(request: Request) -> bool:
    """True when the browser reached the backend over HTTPS, directly or through nginx.

    The appliance serves plain HTTP on the LAN (nginx.conf listens on 80
    only), and a browser never returns a Secure cookie over HTTP, so the
    backend sets Secure only when the request really is HTTPS. nginx passes
    the scheme it received in X-Forwarded-Proto; without that header the
    backend uses the scheme of the request it received.
    """
    forwarded = request.headers.get("x-forwarded-proto", "")
    if forwarded:
        return forwarded.split(",")[0].strip().lower() == "https"
    return request.url.scheme == "https"


def set_session_cookie(response: Response, request: Request, token: str) -> None:
    """Set the session cookie on `response`, with a Max-Age equal to the token's lifetime."""
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=settings.ACCESS_TOKEN_EXPIRE_SECONDS,
        path="/",
        httponly=True,
        samesite="strict",
        secure=_request_is_https(request),
    )


def clear_session_cookie(response: Response, request: Request) -> None:
    """Tell the browser to drop the session cookie (Max-Age=0, same Path and flags)."""
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        httponly=True,
        samesite="strict",
        secure=_request_is_https(request),
    )


def _require_csrf_header(request: Request) -> None:
    if not request.headers.get(CSRF_HEADER):
        raise HTTPException(status_code=403, detail=f"Missing {CSRF_HEADER} header")


def _session_token(
    request: Request, credentials: Optional[HTTPAuthorizationCredentials]
) -> tuple[Optional[str], Optional[str]]:
    """Return the raw token a request carries and where the backend found it.

    Returns (token, "bearer"), (token, "cookie") or (None, None). Raises 403
    when the token came from the cookie on a state-changing request that
    lacks the CSRF header.
    """
    if credentials:
        return credentials.credentials, "bearer"
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if not cookie:
        return None, None
    if request.method.upper() not in _SAFE_METHODS:
        _require_csrf_header(request)
    return cookie, "cookie"


def _token_is_current(payload: dict, user: User) -> bool:
    """Whether a token's session generation (`ver`, 0 when absent) is the user's current one."""
    try:
        return int(payload.get("ver", 0)) == int(user.token_version or 0)
    except (TypeError, ValueError):
        return False


def end_sessions(db: Session, user_id: int) -> None:
    """Raise the user's token_version by one, so every token issued to them before is refused; not committed.

    One UPDATE that adds one to the stored value, so raises made at the
    same time by other requests all count.
    """
    (db.query(User)
       .filter(User.id == user_id)
       .update({User.token_version: User.token_version + 1}, synchronize_session=False))


def _authorize_bootstrap(provided_token: Optional[str], config=settings) -> None:
    """Gate first-user admin bootstrap behind a local trust factor.

    On a fresh or re-flashed unit the first /register call creates an admin with
    no auth. BOOTSTRAP_TOKEN is generated per-unit at first boot and is
    only readable with local (console/SSH) access, so a network attacker cannot
    claim the admin account. Dev keeps the tokenless bootstrap when no token is set.
    """
    configured = config.BOOTSTRAP_TOKEN.strip()
    if is_dev_env(config.APP_ENV) and not configured:
        return
    if not configured:
        # Production with no token configured: fail closed rather than open the bootstrap.
        raise HTTPException(status_code=503, detail="First-user setup is unavailable: no bootstrap token configured")
    if not provided_token or not hmac.compare_digest(provided_token.strip(), configured):
        raise HTTPException(status_code=401, detail="Invalid or missing bootstrap token")


@router.get("/setup/status")
def setup_status(db: Session = Depends(get_db_dependency)):
    """Check whether initial setup is needed (no users exist yet). No auth required."""
    needs_setup = db.query(User).count() == 0
    return {"needs_setup": needs_setup}


@router.post("/register", response_model=UserRead)
def register(
    payload: UserCreate,
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_optional_bearer),
    bootstrap_token: Optional[str] = Header(default=None, alias="X-Bootstrap-Token"),
    background_tasks: BackgroundTasks = None,
    db: Session = Depends(get_db_dependency),
):
    is_first_user = db.query(User).count() == 0

    if is_first_user:
        # Unauthenticated first-user bootstrap requires a local bootstrap token
        _authorize_bootstrap(bootstrap_token)
    else:
        # After bootstrap, only admins may create accounts.
        raw_token, _ = _session_token(request, credentials)
        if not raw_token:
            raise HTTPException(status_code=401, detail="Not authenticated")
        token_payload = verify_access_token(raw_token)
        if not token_payload:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        caller = db.query(User).filter(
            User.id == int(token_payload.get("sub")), User.is_active == True
        ).first()
        if caller is not None and not _token_is_current(token_payload, caller):
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        if not caller or caller.role != "admin":
            raise HTTPException(status_code=403, detail="Only admins can register new users")

    # Only compare email when one was given (NEH-162): SQLAlchemy compiles
    # `User.email == None` to `users.email IS NULL`, which would match every
    # user without an email and raise a false 409, so the email predicate is
    # added only when the payload carries one.
    conflict_filter = User.username == payload.username
    if payload.email is not None:
        conflict_filter = conflict_filter | (User.email == payload.email)
    if db.query(User).filter(conflict_filter).first():
        raise HTTPException(status_code=409, detail="Username or email already exists")

    # First user becomes admin (bootstrap); all subsequent users start as reviewer.
    # Role is never taken from the request payload - use PATCH /auth/users/{id}/role to elevate.
    role = "admin" if is_first_user else "reviewer"

    user = User(
        username=payload.username,
        email=payload.email,
        hashed_password=hash_password(payload.password),
        role=role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    actor = caller.username if not is_first_user else "bootstrap"
    log_event(db, level="INFO", category="access", action="user_created",
              actor=actor, subject=user.username)
    _operators_changed(background_tasks)
    return UserRead.model_validate(user)


def _refuse_testing_account(user: User, status: int = 409) -> None:
    """The testing account is managed only through the unit's testing settings (app.core.testing_space)."""
    from app.core import testing_space
    try:
        testing_space.refuse_testing_account(user, status)
    except testing_space.SpaceRefused as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail())


def _operators_changed(background_tasks: Optional[BackgroundTasks]) -> None:
    """Send the unit's users to Fisqua after the response (contract 1.7); a failure never reaches the route."""
    if background_tasks is not None:
        background_tasks.add_task(notify_operators_changed)


def _client_ip(request: Request) -> str:
    """Best-effort real client IP for per-IP throttling.

    Behind nginx the socket peer is the proxy, so a naive request.client.host would
    put every user in one bucket and let 5 failures lock the whole unit. nginx sets
    X-Real-IP / X-Forwarded-For (see nginx.conf), so prefer those; fall back to the
    socket peer for direct connections. Per-account throttling is the robust half;
    this just makes the per-IP half meaningful behind the reverse proxy.
    """
    xri = request.headers.get("x-real-ip")
    if xri:
        return xri.strip()
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


THROTTLED_DETAIL = "Too many failed login attempts. Try again later."


def _throttle_keys(username: str, request: Request) -> tuple[str, str]:
    """The login throttle's keys for an account and the request's client: `user:<username>` and `ip:<address>`."""
    return f"user:{username.strip().lower()}", f"ip:{_client_ip(request)}"


def _throttled(retry: int) -> HTTPException:
    """The 429 a throttled account or client is answered with, as POST /auth/login answers it."""
    return HTTPException(status_code=429, detail=THROTTLED_DETAIL, headers={"Retry-After": str(retry)})


@router.post("/login")
def login(payload: UserLogin, request: Request, response: Response, db: Session = Depends(get_db_dependency)):
    # Throttle brute force per account and per client IP on the untrusted LAN.
    acct_key, ip_key = _throttle_keys(payload.username, request)

    retry = login_throttle.retry_after(acct_key, ip_key)
    if retry > 0:
        log_event(db, level="WARN", category="access", action="login_throttled",
                  actor=payload.username, detail=f"locked; retry after {retry}s")
        raise _throttled(retry)

    def refuse_credentials():
        login_throttle.record_failure(acct_key, ip_key)
        log_event(db, level="WARN", category="access", action="login_failed",
                  actor=payload.username)
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # The login decides on this one read: the hash checked, the active flag
    # and the session generation the token carries are all taken here, so a
    # password change or a deactivation committed during the slow password
    # check leaves this login with a token of the generation it ended.
    user = db.query(User).filter(User.username == payload.username).first()
    if not user:
        refuse_credentials()
    user_id, username = user.id, user.username
    stored_hash, active, version = user.hashed_password, bool(user.is_active), int(user.token_version or 0)
    if not verify_password(payload.password, stored_hash):
        refuse_credentials()
    if not active:
        log_event(db, level="WARN", category="access", action="login_failed",
                  actor=username, detail="cuenta inactiva")
        raise HTTPException(status_code=403, detail="User is inactive")

    # Success: clear the throttle counters and upgrade a legacy/weak hash while we still hold the plaintext
    login_throttle.reset(acct_key, ip_key)
    if needs_rehash(stored_hash):
        # Written only over the hash that was verified: a password set
        # meanwhile is never replaced, and this login is refused.
        upgraded = (db.query(User)
                    .filter(User.id == user_id, User.hashed_password == stored_hash)
                    .update({User.hashed_password: hash_password(payload.password)},
                            synchronize_session=False))
        db.commit()
        if upgraded != 1:
            refuse_credentials()

    log_event(db, level="INFO", category="access", action="login_success",
              actor=username)
    token = create_access_token(subject=str(user_id), version=version)
    set_session_cookie(response, request, token)
    if request.headers.get(CSRF_HEADER):
        # The browser app sends X-Requested-With on every request and keeps
        # its session in the cookie, so the backend leaves the token out of
        # the body and no page script receives a token it could use elsewhere.
        return {"access_token": None, "token_type": "cookie"}
    # API clients and the e2e scripts read the token from the body.
    return {"access_token": token, "token_type": "bearer"}


@router.post("/logout", status_code=204)
def logout(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_optional_bearer),
    db: Session = Depends(get_db_dependency),
):
    """Clear the session cookie and end the signed-out user's sessions.

    When the request carries a current token (the Authorization header,
    else the cookie) of an active user, that user's token_version is raised
    (end_sessions), so every token issued to them before is refused: an
    ordinary user signed in on two screens is signed out of both. The
    testing account is the exception: several people use it at once, so
    its sign-out clears the cookie only and its other sessions go on.

    Needs no session, so an operator whose token has expired can still log
    out: a missing, expired, stale or unreadable token is answered 204 too,
    and ends nothing. Needs the CSRF header, so a cross-site page cannot log
    the operator out.

    A database error while reading the user or raising the version is
    rolled back and logged at ERROR with the user id (never the token), and
    the answer is still 204 with the cookie cleared: the browser's cookie
    is gone, but the user's other sessions are not ended.
    """
    from app.core.testing_space import TESTING_ROLE

    _require_csrf_header(request)
    raw_token, _ = _session_token(request, credentials)
    payload = verify_access_token(raw_token) if raw_token else None
    user_id = payload.get("sub") if payload else None
    try:
        try:
            user, _ = _user_of_token(raw_token, db)
        except (TypeError, ValueError):
            user = None  # a signed token whose `sub` is not a user id
        if user is not None and user.role != TESTING_ROLE:
            end_sessions(db, user.id)
            db.commit()
    except SQLAlchemyError:
        try:
            db.rollback()
        except SQLAlchemyError:
            pass  # the error logged below is the one that matters
        logger.error("Sign-out of user %s cleared the cookie but could not end the user's sessions",
                     user_id, exc_info=True)
    response = Response(status_code=204)
    clear_session_cookie(response, request)
    return response


@router.post("/refresh", response_model=TokenRefresh)
def refresh_token(
    request: Request,
    response: Response,
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_optional_bearer),
    db: Session = Depends(get_db_dependency),
):
    token, source = _session_token(request, credentials)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = verify_access_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    user_id = int(payload.get("sub"))
    user = db.query(User).filter(User.id == user_id, User.is_active == True).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    if not _token_is_current(payload, user):
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    new_token = create_access_token(subject=str(user.id), version=user.token_version or 0)
    if source == "cookie":
        # A cookie session receives the new token in a new cookie only, so no
        # page script receives a token it could use elsewhere.
        set_session_cookie(response, request, new_token)
        return {"access_token": None, "token_type": "cookie"}
    return {"access_token": new_token, "token_type": "bearer"}


@router.post("/password-reset")
def reset_password(
    payload: PasswordReset,
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_optional_bearer),
    db: Session = Depends(get_db_dependency)
):
    token, _ = _session_token(request, credentials)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    token_payload = verify_access_token(token)
    if not token_payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user_id = int(token_payload.get("sub"))
    # Reject deactivated users, as get_current_user and refresh_token do.
    user = db.query(User).filter(User.id == user_id, User.is_active == True).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    if not _token_is_current(token_payload, user):
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    _refuse_testing_account(user, status=403)

    # The current password counts against the login throttle as a sign-in
    # does: while the account or the client is locked the route answers as
    # POST /auth/login does without checking it, a wrong one is recorded
    # against the same keys, and a change that is written clears them.
    acct_key, ip_key = _throttle_keys(user.username, request)
    retry = login_throttle.retry_after(acct_key, ip_key)
    if retry > 0:
        raise _throttled(retry)
    if not verify_password(payload.old_password, user.hashed_password):
        login_throttle.record_failure(acct_key, ip_key)
        raise HTTPException(status_code=401, detail="Old password incorrect")

    # The new password and the raise of token_version in one UPDATE, so every
    # session of the user ends, the one that sent this request included. It
    # is written only under the session generation this request's token was
    # checked against: a raise committed since (an administrator's new
    # password, a deactivation, a sign-out) ends this session too, and then
    # no row changes, nothing is written and the request is answered as any
    # ended session is, with nothing counted in the throttle.
    written = (db.query(User)
                 .filter(User.id == user.id, User.token_version == int(token_payload.get("ver", 0)))
                 .update({User.hashed_password: hash_password(payload.new_password),
                          User.token_version: User.token_version + 1},
                         synchronize_session=False))
    if written != 1:
        db.rollback()
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    db.commit()
    login_throttle.reset(acct_key, ip_key)
    return {"detail": "password updated successfully"}


def _user_of_token(raw_token: Optional[str], db: Session) -> tuple[Optional[User], Optional[str]]:
    """The active user a token names and is current for, else None and the 401's detail."""
    if not raw_token:
        return None, "Not authenticated"
    payload = verify_access_token(raw_token)
    if not payload:
        return None, "Invalid or expired token"
    user_id = int(payload.get("sub"))
    # Mirror refresh_token: reject deactivated users so deactivation cuts access on the next request
    user = db.query(User).filter(User.id == user_id, User.is_active == True).first()
    if not user:
        return None, "User not found or inactive"
    if not _token_is_current(payload, user):
        return None, "Invalid or expired token"
    return user, None


def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_optional_bearer),
    db: Session = Depends(get_db_dependency)
):
    # The Authorization header, else the session cookie; never a query parameter.
    raw_token, _ = _session_token(request, credentials)
    user, problem = _user_of_token(raw_token, db)
    if user is None:
        raise HTTPException(status_code=401, detail=problem)
    return user


def session_user_or_none(request: Request, db: Session) -> Optional[User]:
    """The user get_current_user would answer for this request, or None where it would raise.

    Raises nothing: app.api.space.enforce_space reads the caller with it and
    leaves every refusal of the session itself (401, the CSRF 403) to the
    route's own dependencies.
    """
    # The credentials _optional_bearer (HTTPBearer, auto_error=False) gives
    # get_current_user for the same request: none unless the header is a
    # Bearer scheme with a value.
    from fastapi.security.utils import get_authorization_scheme_param

    authorization = request.headers.get("Authorization")
    scheme, value = get_authorization_scheme_param(authorization)
    credentials = None
    if authorization and scheme and value and scheme.lower() == "bearer":
        credentials = HTTPAuthorizationCredentials(scheme=scheme, credentials=value)
    try:
        raw_token, _ = _session_token(request, credentials)
    except HTTPException:
        return None
    user, _ = _user_of_token(raw_token, db)
    return user


class RoleChecker:
    """The one role check of the application: a route's allowed roles, and the testing account where admitted.

    Every API module makes its instances from this class. A caller whose
    role is in `allowed_roles` passes. The testing account, whose role is
    in no list, passes exactly on the routes app.core.space_routes admits
    it to (`testing=True`), found by the route that matched the request;
    on every other route, and on a route with no entry, it is refused.
    """

    def __init__(self, allowed_roles: list[str]):
        self.allowed_roles = allowed_roles

    def admits(self, user: User, route_key) -> bool:
        """Whether `user` passes this check on the route keyed (method, full path)."""
        from app.core import space_routes
        from app.core.testing_space import TESTING_ROLE

        if user.role == TESTING_ROLE:
            entry = space_routes.entry_for(route_key)
            return entry is not None and entry.testing
        return user.role in self.allowed_roles

    def __call__(self, request: Request, user: User = Depends(get_current_user)):
        from app.core import space_routes

        if not self.admits(user, space_routes.route_key(request)):
            raise HTTPException(status_code=403, detail="Operation not permitted")
        return user


allow_admin = RoleChecker(["admin"])
allow_contributor = RoleChecker(["admin", "operator"])
allow_read_only = RoleChecker(["admin", "operator", "reviewer"])


# ---------------------------------------------------------------------------
# /users/me - current authenticated user's profile
# ---------------------------------------------------------------------------

@users_router.get("/me", response_model=UserRead)
def get_me(current_user: User = Depends(get_current_user)):
    """Return the authenticated user's profile including their role."""
    return UserRead.model_validate(current_user)


# ---------------------------------------------------------------------------
# User management (admin only)
# ---------------------------------------------------------------------------

@router.get("/users", response_model=List[UserRead])
def list_users(
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=1000),
    current_user: User = Depends(allow_admin),
    db: Session = Depends(get_db_dependency),
):
    """List all registered users. Admin only."""
    users = db.query(User).offset(skip).limit(limit).all()
    return [UserRead.model_validate(u) for u in users]


@router.get("/users/{user_id}", response_model=UserRead)
def get_user(
    user_id: int,
    current_user: User = Depends(allow_admin),
    db: Session = Depends(get_db_dependency),
):
    """Get a user by ID. Admin only."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return UserRead.model_validate(user)


@router.patch("/users/{user_id}/role", response_model=UserRead)
def update_user_role(
    user_id: int,
    payload: UserRoleUpdate,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(allow_admin),
    db: Session = Depends(get_db_dependency),
):
    """Change a user's role. Admin only.

    The testing role is never given (UserRoleUpdate does not accept it) and
    the testing account's role is never changed (409 `testing_account`).
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    _refuse_testing_account(user)
    if user.id == current_user.id:
        raise HTTPException(status_code=400, detail="Admins cannot change their own role")
    user.role = payload.role
    db.add(user)
    db.commit()
    db.refresh(user)
    _operators_changed(background_tasks)
    return UserRead.model_validate(user)


@router.patch("/users/{user_id}/active", response_model=UserRead)
def set_user_active(
    user_id: int,
    is_active: bool,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(allow_admin),
    db: Session = Depends(get_db_dependency),
):
    """Activate or deactivate a user account. Admin only; not the testing account (409 `testing_account`).

    Deactivating an active user raises their token_version in the same
    UPDATE, so every token issued to them before stays refused once they
    are active again; making a user active leaves it.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    _refuse_testing_account(user)
    if user.id == current_user.id:
        raise HTTPException(status_code=400, detail="Admins cannot deactivate their own account")
    if is_active:
        user.is_active = True
        db.add(user)
    else:
        (db.query(User)
           .filter(User.id == user.id, User.is_active == True)
           .update({User.is_active: False, User.token_version: User.token_version + 1},
                   synchronize_session=False))
    db.commit()
    db.refresh(user)
    _operators_changed(background_tasks)
    return UserRead.model_validate(user)


USER_NOT_FOUND = "user_not_found"
USER_NOT_FOUND_MESSAGE = "No user has this id."
USE_OWN_PASSWORD_CHANGE = "use_own_password_change"
USE_OWN_PASSWORD_CHANGE_MESSAGE = (
    "This is your own account. You change your own password with your current password."
)


@router.put("/users/{user_id}/password", response_model=UserRead)
def set_user_password(
    user_id: int,
    payload: PasswordSet,
    current_user: User = Depends(allow_admin),
    db: Session = Depends(get_db_dependency),
):
    """Give another user a new password. Admin only; ends every session of that user.

    The new password is held to the rule POST /auth/register holds a
    password to (app.schemas.user.check_new_password, on PasswordSet as on
    UserCreate) and hashed with hash_password. The hash and the raise of the user's token_version
    are one UPDATE, so every token issued to them before is refused; no
    other user's sessions change. Refused with {code, message} and nothing
    written: 404 `user_not_found`; the testing account, 409 `testing_account`
    (its password is set through /system/testing only); the administrator's
    own account, 409 `use_own_password_change`: an administrator changes
    their own password through POST /auth/password-reset, which needs the
    current password.
    An administrator may set another administrator's password. Logs
    `user_password_set` with the administrator as actor and the user as
    subject; neither the password nor its hash is returned or logged.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail={"code": USER_NOT_FOUND, "message": USER_NOT_FOUND_MESSAGE})
    _refuse_testing_account(user)
    if user.id == current_user.id:
        raise HTTPException(status_code=409, detail={"code": USE_OWN_PASSWORD_CHANGE,
                                                     "message": USE_OWN_PASSWORD_CHANGE_MESSAGE})
    # Unconditional on the target's current hash and session generation: an
    # administrator overrides whatever the user's password was, and a user's
    # own change in flight is then refused by its own guarded UPDATE
    # (reset_password).
    (db.query(User)
       .filter(User.id == user.id)
       .update({User.hashed_password: hash_password(payload.new_password),
                User.token_version: User.token_version + 1},
               synchronize_session=False))
    db.commit()
    db.refresh(user)
    log_event(db, level="INFO", category="access", action="user_password_set",
              actor=current_user.username, subject=user.username)
    return UserRead.model_validate(user)


@router.delete("/users/{user_id}")
def delete_user(
    user_id: int,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(allow_admin),
    db: Session = Depends(get_db_dependency)
) -> dict:
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    # The testing account is managed through the unit's testing settings only.
    _refuse_testing_account(user)
    if user.id == current_user.id:
        raise HTTPException(status_code=400, detail="Admins cannot delete their own account")
    # Refuse to remove the last active admin: a headless appliance with no active
    # admin can't manage users/storage/logs and can't mint a new admin (register
    # needs an admin token once users exist) — an unrecoverable lockout (NEH-57).
    if user.role == "admin" and user.is_active:
        other_active_admins = db.query(User).filter(
            User.role == "admin", User.is_active == True, User.id != user.id
        ).count()
        if other_active_admins == 0:
            raise HTTPException(status_code=400, detail="Cannot delete the last active admin")
    deleted_username = user.username
    db.delete(user)
    db.commit()
    log_event(db, level="INFO", category="access", action="user_deleted",
              actor=current_user.username, subject=deleted_username)
    _operators_changed(background_tasks)
    return {"detail": "user deleted successfully"}
