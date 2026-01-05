from typing import Optional
from pydantic import BaseModel, field_validator
from datetime import datetime
import re


class UserCreate(BaseModel):
    username: str
    email: str
    password: str
    
    @field_validator('email')
    @classmethod
    def validate_email(cls, v: str) -> str:
        """Basic email format validation for offline use."""
        if not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', v):
            raise ValueError('Invalid email format')
        return v.lower()


class UserRead(BaseModel):
    id: int
    username: str
    email: str
    is_active: bool
    created_at: Optional[datetime]

    class Config:
        from_attributes = True


class PasswordReset(BaseModel):
    old_password: str
    new_password: str


class PasswordResetRequest(BaseModel):
    email: str


class TokenRefresh(BaseModel):
    access_token: str
    token_type: str
