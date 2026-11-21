# Version: 0.3.3

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
import threading

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
from app.api.fisqua import router as fisqua_router
from app.api.network import router as network_router
from app.api.space import enforce_space

# How long the shutdown waits for the CHDK start-up scan to end before it
# closes the backend. Closing waits for an enumeration in progress anyway;
# this bound covers a scan still building the backend.
CAMERA_STARTUP_JOIN_S = 30.0


def _start_camera_startup_scan(backend_name):
    """Build the CHDK backend and enumerate once, in a background thread.

    The backend is otherwise built on the first camera request, so until
    someone opens the dashboard no body is open and the keep-alive has no
    backend to run in (capture/backends/chdk_backend.py, _KEEPALIVE_INTERVAL_S).
    Start-up does not wait for the cameras, and a failure is logged at
    WARNING and is not fatal. Nothing is done for the other backends.

    Once shutdown has begun (restart_latch.begin_shutdown) no backend is
    built and no enumeration started; the shutdown joins this thread before
    it closes the backend.

    Returns:
        The thread, or None for a backend other than chdk.
    """
    if backend_name != "chdk":
        return None
    from app.core import restart_latch
    from capture import service as capture_service

    def scan():
        try:
            if restart_latch.shutting_down():
                return
            backend = capture_service.get_backend()
            if restart_latch.shutting_down():
                return
            backend.list_devices()
        except Exception as exc:
            capture_service.subprocess_logger.warning(
                f"[chdk] start-up camera scan failed ({exc!r}); the cameras "
                "are opened at the first camera request instead"
            )

    thread = threading.Thread(target=scan, name="chdk-startup-scan", daemon=True)
    thread.start()
    return thread


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

    camera_backend = assert_camera_backend_configured()
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
    # Write the unit's mode on first start after the upgrade that brought it,
    # with the mode the configuration implies (app.core.unit_mode); a no-op
    # once written. Before the schedulers, whose runs read it.
    from app.core import unit_mode

    unit_mode.initialise()
    # Write the unit's device id on first start (app.core.device_id); a
    # no-op once written. A file that does not hold one is logged and kept,
    # and the unit cannot ask Fisqua for a pairing code until it is repaired.
    from app.core import device_id

    device_id.initialise()
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
    # Remove expired testing material every hour, whether the testing space
    # is on or off (app.core.testing_space).
    from app.core.testing_space import testing_expiry_scheduler

    testing_expiry_scheduler.start()
    # Open the CHDK cameras now rather than at the first camera request, so
    # the keep-alive covers a body switched on before anyone opens the
    # dashboard. Last, so a start-up step that fails leaves no camera open.
    camera_scan = _start_camera_startup_scan(camera_backend)
    yield
    # First: from here no provisioning run starts and pairing starts no
    # scheduler (app.core.restart_latch).
    from app.core import restart_latch

    restart_latch.begin_shutdown()
    # End any pairing session (app.core.fisqua_pairing_service): its secret
    # is dropped and its thread joined.
    from app.core.fisqua_pairing_service import pairing_registry

    pairing_registry.shutdown()
    testing_expiry_scheduler.stop()
    fisqua_status_scheduler.stop()
    image_upload_scheduler.stop()
    catalogue_backup_scheduler.stop()
    # The CHDK start-up scan sees the shutdown flag and builds nothing more;
    # one already under way is waited for, so the backend it builds is the
    # one shutdown_backend closes.
    # Not covered: a scan that has passed its second shutdown check and
    # stalls for the whole bound before list_devices takes the layout lock
    # would enumerate a backend that cleanup has already closed. cleanup
    # leaves a backend usable, so nothing refuses that enumeration.
    if camera_scan is not None:
        camera_scan.join(CAMERA_STARTUP_JOIN_S)
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
# The browser session is an httpOnly cookie (app/api/auth.py), so credentials
# are allowed and the origins must be an explicit list, never "*"
# (app/core/config.py refuses to start with one). X-Requested-With is the
# CSRF header that a cookie-authenticated state change must carry.
# Methods and headers are restricted to those required by the app.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "X-Bootstrap-Token", "X-Requested-With"],
    expose_headers=["X-Capture-Seconds", "X-Capture-Bytes", "X-Focal-Length", "X-Page-Layout", "Retry-After"],
)

# Every router runs the testing space's access rule (app.api.space), so a
# route added to any of them later runs it too.
_SPACE = [Depends(enforce_space)]
app.include_router(records_router, prefix="/records", tags=["records"], dependencies=_SPACE)
app.include_router(cameras_router, prefix="/cameras", tags=["cameras"], dependencies=_SPACE)
app.include_router(projects_router, prefix="/projects", tags=["projects"], dependencies=_SPACE)
app.include_router(collections_router, prefix="/collections", tags=["collections"], dependencies=_SPACE)
app.include_router(auth_router, prefix="/auth", tags=["auth"], dependencies=_SPACE)
app.include_router(users_router, prefix="/users", tags=["users"], dependencies=_SPACE)
app.include_router(system_router, prefix="/system", tags=["system"], dependencies=_SPACE)
app.include_router(activity_router, prefix="/activity", tags=["activity"], dependencies=_SPACE)
app.include_router(imports_router, prefix="/imports", tags=["imports"], dependencies=_SPACE)
app.include_router(fisqua_router, prefix="/fisqua", tags=["fisqua"], dependencies=_SPACE)
app.include_router(network_router, prefix="/network", tags=["network"], dependencies=_SPACE)


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


from app.models.project import TestingSpaceError
from fastapi.exceptions import RequestValidationError
from app.core.validation_errors import plain_errors


@app.exception_handler(RequestValidationError)
async def validation_refused(request: Request, exc: RequestValidationError):
    """Every router's 422 (app.core.validation_errors.plain_errors).

    Errors raised at the request-validation boundary carry `type`, `loc` and
    `msg` and no submitted value. A route's own HTTPException message may
    name a non-credential parameter it refused (for example an unknown time
    zone or a resolution). No credential, hash or token is ever placed in a
    response or a log line.
    """
    return JSONResponse(status_code=422, content={"detail": plain_errors(exc.errors())})


@app.exception_handler(TestingSpaceError)
async def testing_space_refused(request: Request, exc: TestingSpaceError):
    """A write that would put testing and real work together, refused at a flush that no route checked first.

    The detail carries the code and its public message, never the stored
    values the exception names.
    """
    from app.core import testing_space

    code = exc.code or testing_space.CROSS_SPACE
    message = testing_space.NAMESPACE_MESSAGES.get(code, testing_space.CROSS_SPACE_MESSAGE)
    return JSONResponse(status_code=409, content={"detail": {"code": code, "message": message}})


@app.get("/health")
def health():
    return {"status": "ok"}
