# Version: 0.3.0

from typing import Optional, Literal
from pydantic import BaseModel, field_validator
from datetime import datetime
import re

# Every role a user row may hold. `testing` is the unit's one testing account
# (app.core.testing_space): it is never given or taken away through
# UserCreate or UserRoleUpdate, which accept the other three only.
VALID_ROLES = ("admin", "operator", "reviewer", "testing")

# The rule every password a request sets is held to: POST /auth/register
# (UserCreate), PUT /auth/users/{user_id}/password (PasswordSet) and the new
# password of POST /auth/password-reset (PasswordReset). At least
# PASSWORD_MIN_LENGTH characters, counted as sent (no trimming), and
# encodable as UTF-8; nothing else. Passwords already stored are not held to
# it, so sign-in is unchanged, and the testing account's password keeps its
# own rule (app.core.testing_space), set through /system/testing only.
PASSWORD_MIN_LENGTH = 8
PASSWORD_TOO_SHORT = "password_too_short"
PASSWORD_INVALID = "password_invalid"
PASSWORD_TOO_SHORT_MESSAGE = f"A password needs at least {PASSWORD_MIN_LENGTH} characters."
PASSWORD_INVALID_MESSAGE = "The password holds characters that cannot be stored."


class PasswordRefused(Exception):
    """A new password the rule refuses: a code and a message, never the password.

    Raised from the schemas' validators, so pydantic does not turn it into
    a validation error (whose 422 body would carry the input); app.main
    answers it 422 {"detail": {code, message[, min_length]}}, as
    /system/testing answers its own password refusals.
    """

    def __init__(self, code: str, message: str, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra

    def detail(self) -> dict:
        return {"code": self.code, "message": self.message, **self.extra}


def check_new_password(value: str) -> str:
    """`value` when the rule takes it; PasswordRefused `password_invalid` or `password_too_short` otherwise."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise PasswordRefused(PASSWORD_INVALID, PASSWORD_INVALID_MESSAGE) from None
    if len(value) < PASSWORD_MIN_LENGTH:
        raise PasswordRefused(PASSWORD_TOO_SHORT, PASSWORD_TOO_SHORT_MESSAGE, min_length=PASSWORD_MIN_LENGTH)
    return value


class UserCreate(BaseModel):
    username: str
    # Optional: the appliance runs offline and nothing verifies an address
    # (NEH-162).
    email: Optional[str] = None
    password: str
    role: Literal["admin", "operator", "reviewer"] = "reviewer"

    @field_validator("password")
    @classmethod
    def _password_rule(cls, v: str) -> str:
        return check_new_password(v)

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
    # The current password is checked against the stored hash only, never against the rule.
    old_password: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def _password_rule(cls, v: str) -> str:
        return check_new_password(v)


class PasswordSet(BaseModel):
    """The body of PUT /auth/users/{user_id}/password: the new password, held to the rule UserCreate.password is."""
    new_password: str

    @field_validator("new_password")
    @classmethod
    def _password_rule(cls, v: str) -> str:
        return check_new_password(v)


class PasswordResetRequest(BaseModel):
    email: str


class TokenRefresh(BaseModel):
    # None for a cookie session: the backend puts the new token in the cookie only.
    access_token: Optional[str] = None
    token_type: str
