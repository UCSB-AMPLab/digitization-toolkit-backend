# Version: 0.1.0-beta

from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Boolean, DateTime, CheckConstraint, Index, text

from app.core.db import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(150), unique=True, index=True, nullable=False)
    # Optional: the appliance runs offline and nothing verifies an address
    # (NEH-162). PostgreSQL treats NULLs as distinct in a unique index, so
    # several users without an email are allowed.
    email = Column(String(255), unique=True, index=True, nullable=True)
    hashed_password = Column(String(255), nullable=False)
    # admin, operator, reviewer, or testing: the unit's one testing account
    # (app.core.testing_space), which no role allowlist admits.
    role = Column(String(50), default="reviewer", nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    # The account's session generation: every token carries it as `ver` (0 when
    # absent), and a token whose `ver` differs is refused (app.api.auth).
    token_version = Column(Integer, default=0, server_default=text("0"), nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    __table_args__ = (
        CheckConstraint("role IN ('admin','operator','reviewer','testing')", name='check_user_role'),
        # At most one account has the testing role.
        Index('uq_users_one_testing_account', 'role', unique=True,
              postgresql_where=text("role = 'testing'"), sqlite_where=text("role = 'testing'")),
    )
