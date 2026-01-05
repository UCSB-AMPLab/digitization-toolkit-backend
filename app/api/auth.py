from fastapi import APIRouter, Depends, HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.api.deps import get_db_dependency
from app.models.user import User
from app.schemas.user import UserCreate, UserRead, PasswordReset, PasswordResetRequest, TokenRefresh
from app.core.security import hash_password, verify_password, create_access_token, verify_access_token

router = APIRouter()
security = HTTPBearer()


@router.post("/register", response_model=UserRead)
def register(payload: UserCreate, db: Session = Depends(get_db_dependency)):
    if db.query(User).filter((User.username == payload.username) | (User.email == payload.email)).first():
        raise HTTPException(status_code=409, detail="Username or email already exists")
    user = User(username=payload.username, email=payload.email, hashed_password=hash_password(payload.password))
    db.add(user)
    db.commit()
    db.refresh(user)
    return UserRead.model_validate(user)


@router.post("/login")
def login(payload: UserCreate, db: Session = Depends(get_db_dependency)):
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


def get_current_user(credentials: HTTPAuthorizationCredentials = Security(security), db: Session = Depends(get_db_dependency)):
    token = credentials.credentials
    payload = verify_access_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    user_id = int(payload.get("sub"))
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user


@router.delete("/{user_id}")
def delete_user(
    user_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db_dependency)
) -> dict:
    
    if current_user.id != user_id:
        raise HTTPException(status_code=403, detail="Not authorized to delete this user")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    db.delete(user)
    db.commit()
    return {"detail": "user deleted successfully"}
