from fastapi import APIRouter, Depends, HTTPException, Security, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session
from typing import List, Optional

from app.api.deps import get_db_dependency
from app.models.user import User
from app.schemas.user import UserCreate, UserLogin, UserRead, UserRoleUpdate, PasswordReset, PasswordResetRequest, TokenRefresh
from app.core.security import hash_password, verify_password, create_access_token, verify_access_token

router = APIRouter()
users_router = APIRouter()  # mounted at /users in main.py
security = HTTPBearer()
# auto_error=False so the dependency doesn't raise when header is absent
# (allows falling back to ?token= query param for browser src= requests)
_optional_bearer = HTTPBearer(auto_error=False)


@router.post("/register", response_model=UserRead)
def register(payload: UserCreate, db: Session = Depends(get_db_dependency)):
    if db.query(User).filter((User.username == payload.username) | (User.email == payload.email)).first():
        raise HTTPException(status_code=409, detail="Username or email already exists")

    # First user in the system becomes admin (bootstrap); all subsequent users are reviewers.
    # Role is never taken from the request payload — use PATCH /auth/users/{id}/role to elevate.
    is_first_user = db.query(User).count() == 0
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
    return UserRead.model_validate(user)


@router.post("/login")
def login(payload: UserLogin, db: Session = Depends(get_db_dependency)):
    user = db.query(User).filter(User.username == payload.username).first()
    if not user or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="User is inactive")
    token = create_access_token(subject=str(user.id))
    return {"access_token": token, "token_type": "bearer"}


@router.post("/refresh", response_model=TokenRefresh)
def refresh_token(credentials: HTTPAuthorizationCredentials = Security(security), db: Session = Depends(get_db_dependency)):
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
    credentials: HTTPAuthorizationCredentials = Security(security),
    db: Session = Depends(get_db_dependency)
):
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
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
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
# /users/me — current authenticated user's profile
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


@router.delete("/{user_id}")
def delete_user(
    user_id: int,
    current_user: User = Depends(allow_admin),
    db: Session = Depends(get_db_dependency)
) -> dict:
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    db.delete(user)
    db.commit()
    return {"detail": "user deleted successfully"}
