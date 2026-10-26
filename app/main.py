# Version: 0.1.0-beta

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager

from app.core.db import init_db
from app.core.schema import assert_schema_at_head
from app.core.config import log_environment_names, settings
from app.core.logging_setup import configure_logging

# Before the routers are imported, so their import-time lines are kept too.
configure_logging()
# Which settings came under CAPTUA_ names and which under the older DTK_ names.
log_environment_names()

from app.api.records import router as records_router
from app.api.cameras import router as cameras_router
from app.api.projects import router as projects_router
from app.api.collections import router as collections_router
from app.api.auth import router as auth_router, users_router
from app.api.system import router as system_router
from app.api.activity import router as activity_router
from app.api.imports import router as imports_router

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
        # Move the files of each volume with a file name stem from the
        # name-based layout into `<stem>/`, completing a batch a crash
        # interrupted; a no-op once every such volume is done
        # (app.core.volume_layout).
        from app.core.volume_layout import migrate_volume_layout_at_startup
        migrate_volume_layout_at_startup(db)
    finally:
        db.close()
    # Schedule the offsite catalogue dump (NEH-249). A no-op on a unit with no
    # offsite remote configured.
    from app.core.catalogue_backup import catalogue_backup_scheduler

    catalogue_backup_scheduler.start()
    # Schedule the offsite image upload (NEH-227). A no-op on a unit with no
    # offsite remote or no tenant prefix configured.
    from app.core.image_upload import image_upload_scheduler

    image_upload_scheduler.start()
    # Poll Fisqua for the status of sent volumes, every ten minutes
    # (app.core.fisqua_outbox). A no-op on a unit with Fisqua not configured.
    from app.core.fisqua_outbox import fisqua_status_scheduler

    fisqua_status_scheduler.start()
    yield
    fisqua_status_scheduler.stop()
    image_upload_scheduler.stop()
    catalogue_backup_scheduler.stop()
    # The camera backends hold a PTP claim on each body for the life of the
    # process. Closing them here waits for whatever capture is still running
    # and releases the cameras, so a restart finds them free rather than
    # claimed by a process that has gone.
    from capture.service import shutdown_backend

    shutdown_backend()

app = FastAPI(lifespan=lifespan)

# Allow Svelte frontend API access. Origins use a deployment allowlist. On an
# appliance the kiosk opens the frontend container at http://localhost:3000
# while the API is reached through nginx at http://<host>/api, so the kiosk is
# cross-origin too, not only the dev server. A response header the frontend
# reads must be listed in expose_headers: Retry-After is, so the login page
# can say how long a lockout lasts.
# Methods and headers are restricted to those required by the app.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "X-Bootstrap-Token"],
    expose_headers=["X-Capture-Seconds", "X-Capture-Bytes", "Retry-After"],
)

app.include_router(records_router, prefix="/records", tags=["records"])
app.include_router(cameras_router, prefix="/cameras", tags=["cameras"])
app.include_router(projects_router, prefix="/projects", tags=["projects"])
app.include_router(collections_router, prefix="/collections", tags=["collections"])
app.include_router(auth_router, prefix="/auth", tags=["auth"])
app.include_router(users_router, prefix="/users", tags=["users"])
app.include_router(system_router, prefix="/system", tags=["system"])
app.include_router(activity_router, prefix="/activity", tags=["activity"])
app.include_router(imports_router, prefix="/imports", tags=["imports"])


from app.core.storage_override import StorageUnavailableError
from app.models.collection import (
    ReferenceCodeDiffersFromStemError,
    StemDirectoryCollisionError,
    StemHeldOffsiteError,
)


@app.exception_handler(StorageUnavailableError)
async def storage_unavailable(request: Request, exc: StorageUnavailableError):
    """A write into the projects tree refused because the override root is not usable."""
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.exception_handler(StemDirectoryCollisionError)
async def stem_directory_taken(request: Request, exc: StemDirectoryCollisionError):
    """A stem and a project's directory name refused at a flush that no route checked first."""
    return JSONResponse(status_code=409, content={"detail": {"message": str(exc)}})


@app.exception_handler(StemHeldOffsiteError)
async def stem_held_offsite(request: Request, exc: StemHeldOffsiteError):
    """A stem with offsite objects keyed under it, refused at a flush."""
    return JSONResponse(status_code=409, content={"detail": {"message": str(exc)}})


@app.exception_handler(ReferenceCodeDiffersFromStemError)
async def reference_code_differs_from_stem(request: Request, exc: ReferenceCodeDiffersFromStemError):
    """A volume's reference code and stem set to different values, refused at a flush."""
    return JSONResponse(status_code=409, content={"detail": {
        "message": str(exc), "reason": exc.reason,
        "external_id": exc.external_id, "filename_stem": exc.filename_stem,
    }})


@app.get("/health")
def health():
    return {"status": "ok"}
