# Backend Development Guide

## Critical Guidelines for AI-Assisted Development

When using AI assistants (GitHub Copilot, ChatGPT, etc.) to work on this codebase, **always** provide these instructions:

### ⚠️ Database Migrations

**NEVER add `Base.metadata.create_all()` to `app/core/db.py`**

- Tables are managed **exclusively through Alembic migrations**
- The `init_db()` function should ONLY import models, not create tables
- This ensures schema changes are tracked, versioned, and reversible

**Correct pattern:**
```python
def init_db() -> None:
    """Import all models to register them with SQLAlchemy."""
    import app.models.document
    import app.models.camera
    import app.models.project
    import app.models.user
    # NO create_all() here!
```

**When adding/modifying models:**
1. Update the model in `app/models/`
2. Generate migration: `docker compose exec backend alembic revision --autogenerate -m "description"`
3. Review the generated migration file
4. Apply: `docker compose exec backend alembic upgrade head`

### 🔐 Authentication & Security

- **Custom token system is intentional** - This is a standalone/offline Raspberry Pi application
- Do NOT suggest replacing with JWT libraries (python-jose, PyJWT) or OAuth2
- Do NOT add `pydantic[email]` dependency - uses simple regex validation for offline compatibility
- Secret keys must come from environment variables, never hardcoded

### 🐘 PostgreSQL Configuration

- Use `postgresql+psycopg://` for psycopg3 (NOT `postgresql://`)
- Always test with PostgreSQL in development (matching production)
- Database URL format: `postgresql+psycopg://user:password@host:port/database`

### 📦 Configuration Management

**Only add settings that are used by application code**

- Settings in `app/core/config.py` should be consumed by the application
- Infrastructure settings (uvicorn host/port) belong in `docker-compose.yml`, not Settings
- If you're adding a field to Settings, ensure it's actually used in the code

### 🔧 Docker Commands

- Use `docker compose` (not `docker-compose`) on Raspberry Pi
- Always exec into container for Alembic: `docker compose exec backend alembic ...`

## Common AI Mistakes to Avoid

1. ✗ Re-adding `create_all()` after it was intentionally removed
2. ✗ Suggesting JWT/OAuth2 for a standalone offline application  
3. ✗ Adding unused configuration fields "just in case"
4. ✗ Using `postgresql://` instead of `postgresql+psycopg://`
5. ✗ Creating migrations outside Docker (wrong database host)

## Quick Reference

### Run migrations
```bash
docker compose exec backend alembic upgrade head
```

### Generate migration after model changes
```bash
docker compose exec backend alembic revision --autogenerate -m "description"
```

### Check database tables
```bash
docker compose exec backend python -c "from app.core.db import engine; from sqlalchemy import inspect; print(inspect(engine).get_table_names())"
```

### Run tests
```bash
docker compose exec backend python test_api.py
```

## Context for AI Assistants

**Project Type:** Digitization toolkit for standalone Raspberry Pi deployment  
**Environment:** Offline-capable, single-instance  
**Database:** PostgreSQL with psycopg3  
**Migrations:** Alembic (strict - no auto table creation)  
**Auth:** Custom HMAC tokens (no JWT libraries)  

---

**When in doubt, check git history before making changes that might revert previous intentional decisions.**
