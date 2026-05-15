from typing import Optional, Literal
from pydantic import BaseModel, field_validator
from datetime import datetime
import re

VALID_ROLES = ("admin", "operator", "reviewer")


class UserCreate(BaseModel):
    username: str
    email: str
    password: str
    role: Literal["admin", "operator", "reviewer"] = "reviewer"

    @field_validator('email')
    @classmethod
    def validate_email(cls, v: str) -> str:
        """Basic email format validation for offline use."""
        if not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', v):
            raise ValueError('Invalid email format')
        return v.lower()


class UserLogin(BaseModel):
    username: str
    password: str


class UserRead(BaseModel):
    id: int
    username: str
    email: str
    role: str
    is_active: bool
    created_at: Optional[datetime]

    class Config:
        from_attributes = True


class UserRoleUpdate(BaseModel):
    role: Literal["admin", "operator", "reviewer"]


class PasswordReset(BaseModel):
    old_password: str
    new_password: str


class PasswordResetRequest(BaseModel):
    email: str


class TokenRefresh(BaseModel):
    access_token: str
    token_type: str
