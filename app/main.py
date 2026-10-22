from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

# initialize DB tables
from app.core.db import init_db
from app.core.schema import assert_schema_at_head
from app.core.config import settings

# routers
from app.api.records import router as records_router
from app.api.cameras import router as cameras_router
from app.api.projects import router as projects_router
from app.api.collections import router as collections_router
from app.api.auth import router as auth_router, users_router
from app.api.system import router as system_router
from app.api.activity import router as activity_router

# Define lifespan event to initialize the database
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Refuse to serve against an un-migrated schema so the failure is loud at startup rather than a later 500 in the field
    assert_schema_at_head()
    # Refuse to serve with a CAMERA_BACKEND this build does not have. Checked
    # here rather than where the backend is built, because that happens lazily
    # on the first capture: the service would otherwise start, answer /health,
    # and fail only when an operator tried to take a photograph (NEH-268).
    # Imported inside the lifespan, as shutdown_backend is, so importing the
    # application does not pull in the capture stack.
    from capture.service import assert_camera_backend_configured

    assert_camera_backend_configured()
    # Finish or discard any project rename interrupted by a crash/power loss before serving requests
    from app.core.db import SessionLocal
    from app.core.storage_ops import reconcile_pending_rename
    db = SessionLocal()
    try:
        reconcile_pending_rename(db)
    finally:
        db.close()
    # Schedule the offsite catalogue dump (NEH-249). A no-op on a unit with no
    # offsite remote configured.
    from app.core.catalogue_backup import catalogue_backup_scheduler

    catalogue_backup_scheduler.start()
    yield
    catalogue_backup_scheduler.stop()
    # The camera backends hold a PTP claim on each body for the life of the
    # process. Closing them here waits for whatever capture is still running
    # and releases the cameras, so a restart finds them free rather than
    # claimed by a process that has gone.
    from capture.service import shutdown_backend

    shutdown_backend()

# Create FastAPI app with lifespan
app = FastAPI(lifespan=lifespan)

# Allow Svelte frontend API access. Production is same-origin via nginx;
# CORS applies only to the dev server. Origins use a deployment allowlist.
# Methods and headers are restricted to those required by the app.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "X-Bootstrap-Token"],
    expose_headers=["X-Capture-Seconds", "X-Capture-Bytes"],
)

app.include_router(records_router, prefix="/records", tags=["records"])
app.include_router(cameras_router, prefix="/cameras", tags=["cameras"])
app.include_router(projects_router, prefix="/projects", tags=["projects"])
app.include_router(collections_router, prefix="/collections", tags=["collections"])
app.include_router(auth_router, prefix="/auth", tags=["auth"])
app.include_router(users_router, prefix="/users", tags=["users"])
app.include_router(system_router, prefix="/system", tags=["system"])
app.include_router(activity_router, prefix="/activity", tags=["activity"])


@app.get("/health")
def health():
    return {"status": "ok"}
