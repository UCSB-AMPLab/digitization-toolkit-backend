import hmac

from fastapi import APIRouter, Depends, HTTPException, Security, Query, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session
from typing import List, Optional

from app.api.deps import get_db_dependency
from app.models.user import User
from app.schemas.user import UserCreate, UserLogin, UserRead, UserRoleUpdate, PasswordReset, PasswordResetRequest, TokenRefresh
from app.core.security import hash_password, verify_password, create_access_token, verify_access_token, needs_rehash
from app.core.config import settings, is_dev_env
from app.core.login_throttle import login_throttle
from app.core.audit import log_event

router = APIRouter()
users_router = APIRouter()  # mounted at /users in main.py
# auto_error=False on every HTTPBearer instance so the status code for a
# missing/malformed Authorization header is chosen by our code, not by the
# framework default (which has varied across FastAPI versions). A credential
# problem is always a 401 ("session problem"), never a 403 ("authorization
# answer"); each dependency below raises HTTPException(401, ...) explicitly.
# See NEH-167.
_optional_bearer = HTTPBearer(auto_error=False)


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
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_optional_bearer),
    bootstrap_token: Optional[str] = Header(default=None, alias="X-Bootstrap-Token"),
    db: Session = Depends(get_db_dependency),
):
    is_first_user = db.query(User).count() == 0

    if is_first_user:
        # Unauthenticated first-user bootstrap requires a local bootstrap token
        _authorize_bootstrap(bootstrap_token)
    else:
        # After bootstrap, only admins may create accounts.
        if not credentials:
            raise HTTPException(status_code=401, detail="Not authenticated")
        token_payload = verify_access_token(credentials.credentials)
        if not token_payload:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        caller = db.query(User).filter(
            User.id == int(token_payload.get("sub")), User.is_active == True
        ).first()
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
    return UserRead.model_validate(user)


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


@router.post("/login")
def login(payload: UserLogin, request: Request, db: Session = Depends(get_db_dependency)):
    # Throttle brute force per account and per client IP on the untrusted LAN.
    ip = _client_ip(request)
    acct_key = f"user:{payload.username.strip().lower()}"
    ip_key = f"ip:{ip}"

    retry = login_throttle.retry_after(acct_key, ip_key)
    if retry > 0:
        log_event(db, level="WARN", category="access", action="login_throttled",
                  actor=payload.username, detail=f"locked; retry after {retry}s")
        raise HTTPException(status_code=429, detail="Too many failed login attempts. Try again later.",
                            headers={"Retry-After": str(retry)})

    user = db.query(User).filter(User.username == payload.username).first()
    if not user or not verify_password(payload.password, user.hashed_password):
        login_throttle.record_failure(acct_key, ip_key)
        log_event(db, level="WARN", category="access", action="login_failed",
                  actor=payload.username)
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not user.is_active:
        log_event(db, level="WARN", category="access", action="login_failed",
                  actor=user.username, detail="cuenta inactiva")
        raise HTTPException(status_code=403, detail="User is inactive")

    # Success: clear the throttle counters and upgrade a legacy/weak hash while we still hold the plaintext
    login_throttle.reset(acct_key, ip_key)
    if needs_rehash(user.hashed_password):
        user.hashed_password = hash_password(payload.password)
        db.add(user)
        db.commit()

    log_event(db, level="INFO", category="access", action="login_success",
              actor=user.username)
    token = create_access_token(subject=str(user.id))
    return {"access_token": token, "token_type": "bearer"}


@router.post("/refresh", response_model=TokenRefresh)
def refresh_token(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_optional_bearer),
    db: Session = Depends(get_db_dependency),
):
    if not credentials:
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = credentials.credentials
    payload = verify_access_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    user_id = int(payload.get("sub"))
    user = db.query(User).filter(User.id == user_id, User.is_active == True).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    new_token = create_access_token(subject=str(user.id))
    return {"access_token": new_token, "token_type": "bearer"}


@router.post("/password-reset")
def reset_password(
    payload: PasswordReset,
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_optional_bearer),
    db: Session = Depends(get_db_dependency)
):
    if not credentials:
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = credentials.credentials
    token_payload = verify_access_token(token)
    if not token_payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user_id = int(token_payload.get("sub"))
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    if not verify_password(payload.old_password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Old password incorrect")

    user.hashed_password = hash_password(payload.new_password)
    db.add(user)
    db.commit()
    return {"detail": "password updated successfully"}


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_optional_bearer),
    token: Optional[str] = Query(default=None),
    db: Session = Depends(get_db_dependency)
):
    # Accept token from Authorization header OR ?token= query param (needed for <img src>)
    raw_token = credentials.credentials if credentials else token
    if not raw_token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = verify_access_token(raw_token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    user_id = int(payload.get("sub"))
    # Mirror refresh_token: reject deactivated users so deactivation cuts access on the next request
    user = db.query(User).filter(User.id == user_id, User.is_active == True).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    return user


class RoleChecker:
    def __init__(self, allowed_roles: list[str]):
        self.allowed_roles = allowed_roles

    def __call__(self, user: User = Depends(get_current_user)):
        if user.role not in self.allowed_roles:
            raise HTTPException(status_code=403, detail="Operation not permitted")
        return user


# Role checkers
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
    current_user: User = Depends(allow_admin),
    db: Session = Depends(get_db_dependency),
):
    """Change a user's role. Admin only."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == current_user.id:
        raise HTTPException(status_code=400, detail="Admins cannot change their own role")
    user.role = payload.role
    db.add(user)
    db.commit()
    db.refresh(user)
    return UserRead.model_validate(user)


@router.patch("/users/{user_id}/active", response_model=UserRead)
def set_user_active(
    user_id: int,
    is_active: bool,
    current_user: User = Depends(allow_admin),
    db: Session = Depends(get_db_dependency),
):
    """Activate or deactivate a user account. Admin only."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == current_user.id:
        raise HTTPException(status_code=400, detail="Admins cannot deactivate their own account")
    user.is_active = is_active
    db.add(user)
    db.commit()
    db.refresh(user)
    return UserRead.model_validate(user)


@router.delete("/users/{user_id}")
def delete_user(
    user_id: int,
    current_user: User = Depends(allow_admin),
    db: Session = Depends(get_db_dependency)
) -> dict:
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
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
    return {"detail": "user deleted successfully"}
