# Backend Development Guide

## Critical Guidelines for AI-Assisted Development

When using AI assistants (GitHub Copilot, ChatGPT, etc.) to work on this codebase, **always** provide these instructions:

### [WARNING] Database Migrations

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

### [INFO] Authentication & Security

- **Custom token system is intentional** - This is a standalone/offline Raspberry Pi application
- Do NOT suggest replacing with JWT libraries (python-jose, PyJWT) or OAuth2
- Do NOT add `pydantic[email]` dependency - uses simple regex validation for offline compatibility
- Secret keys must come from environment variables, never hardcoded

### [WARNING] Dependency Manifests

**Two manifests must stay in lockstep: `requirements.txt` (Docker) and `pixi.toml` (native Pi)**

- The Docker image installs from `requirements.txt`; the Pi runs the backend natively through pixi
- Any dependency change updates **both** files in the same PR, then regenerates the lockfile: `pixi lock` (the committed `pixi.lock` only guarantees reproducible Pi installs when the updater runs `pixi install --locked`)
- The committed lockfile uses lock-file format v6; the unit's installed pixi must be recent enough to read the committed format — check `pixi --version` on target hardware before shipping a regenerated lock
- Version-equal is not behaviour-equal: conda-forge's `uvicorn` includes the `standard` extras, so the Pi runs **uvloop** while Docker runs plain asyncio, and `websockets` resolves to a different major on the Pi. Nothing in the app currently depends on either difference, but test on-device accordingly
- `CORSMiddleware(allow_private_network=...)` is currently commented out in `app/main.py`; it requires starlette ≥0.51.0, which the pinned 1.3.1 satisfies if it is ever re-enabled (see NEH-161)

### [INFO] PostgreSQL Configuration

- Use `postgresql+psycopg://` for psycopg3 (NOT `postgresql://`)
- Always test with PostgreSQL in development (matching production)
- Database URL format: `postgresql+psycopg://user:password@host:port/database`

### [INFO] Configuration Management

**Only add settings that are used by application code**

- Settings in `app/core/config.py` should be consumed by the application
- Infrastructure settings (uvicorn host/port) belong in `docker-compose.yml`, not Settings
- If you're adding a field to Settings, ensure it's actually used in the code

### [INFO] Docker Commands

- Use `docker compose` (not `docker-compose`) on Raspberry Pi
- Always exec into container for Alembic: `docker compose exec backend alembic ...`

## Common AI Mistakes to Avoid

1. [ERROR] Re-adding `create_all()` after it was intentionally removed
2. [ERROR] Suggesting JWT/OAuth2 for a standalone offline application  
3. [ERROR] Adding unused configuration fields "just in case"
4. [ERROR] Using `postgresql://` instead of `postgresql+psycopg://`
5. [ERROR] Creating migrations outside Docker (wrong database host)
6. [ERROR] Changing a dependency in `requirements.txt` but not `pixi.toml` (or vice versa) — they must move together, with `pixi lock` re-run

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
