from typing import Optional, Literal
from pydantic import BaseModel, field_validator
from datetime import datetime
import re

VALID_ROLES = ("admin", "operator", "reviewer")


class UserCreate(BaseModel):
    username: str
    # Optional: the appliance runs offline and nothing verifies an address
    # (NEH-162).
    email: Optional[str] = None
    password: str
    role: Literal["admin", "operator", "reviewer"] = "reviewer"

    @field_validator('email', mode='before')
    @classmethod
    def validate_email(cls, v: Optional[str]) -> Optional[str]:
        """Basic email format validation for offline use.

        Empty/whitespace-only or absent means "no email" and is stored as
        NULL; anything else must match the format regex.
        """
        if v is None:
            return None
        # A "before" validator receives the raw payload value, so a number,
        # a list or an object arrives here as is; anything but a string is
        # a format error, not a server error.
        if not isinstance(v, str):
            raise ValueError('Invalid email format')
        v = v.strip()
        if not v:
            return None
        if not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', v):
            raise ValueError('Invalid email format')
        return v.lower()


class UserLogin(BaseModel):
    username: str
    password: str


class UserRead(BaseModel):
    id: int
    username: str
    email: Optional[str] = None
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
