from fastapi import APIRouter, Depends, HTTPException, Query
from typing import List, Optional
from sqlalchemy.orm import Session
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from pydantic import BaseModel
import logging

from app.core.export_jobs import export_jobs
from app.core.storage_ops import (
    collection_and_descendants,
    collection_ids_under_project,
    collections_in_old_layout,
    image_in_old_layout,
    move_project_tree,
    rebase_stored_path,
    relocate_images_and_commit,
    remove_tree,
    retire_project_manifest,
    surviving_images_under,
)

from app.api.deps import get_db_dependency
from app.api.auth import get_current_user, RoleChecker
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.record import Record
# One definition of whether a record may change parents, and the loader that
# locks the row the decision is made from (NEH-260).
from app.api.records import _locked_record, assert_movable, RecordImage
from app.models.collection import Collection, stem_directory_collision
from app.models.user import User
from app.schemas.project import ProjectCreate, ProjectRead, ProjectBase, ProjectUpdate
from app.schemas.project_member import ProjectMemberCreate, ProjectMemberRead
from app.core.audit import log_event
from app.core.db_errors import integrity_conflict

router = APIRouter()
logger = logging.getLogger(__name__)

# Role checkers
allow_admin = RoleChecker(["admin"])
allow_contributor = RoleChecker(["admin", "operator"])
allow_read_only = RoleChecker(["admin", "operator", "reviewer"])

EXPORT_IN_PROJECT_MESSAGE = (
    "An export of a collection in this project is running. "
    "Wait for it to finish, then try again."
)


class ProjectInitRequest(BaseModel):
	"""Request body for project initialization."""
	resolution: str = "high"  # low, medium, high


class ProjectInitResponse(BaseModel):
	"""Response from project initialization."""
	success: bool
	project_path: Optional[str] = None
	error: Optional[str] = None


@router.post("/", response_model=ProjectRead)
def create_project(
    payload: ProjectCreate,
    current_user: User = Depends(allow_admin),
    db: Session = Depends(get_db_dependency)
):
    if db.query(Project).filter(Project.name == payload.name).first():
        raise HTTPException(status_code=409, detail="Project with this name already exists")
    _refuse_a_name_that_is_a_stem(db, payload.name)
    p = Project(name=payload.name, description=payload.description, fondo=payload.fondo, serie=payload.serie, signatura=payload.signatura, created_by=payload.created_by or current_user.username)
    db.add(p)
    db.commit()
    db.refresh(p)
    # Creator is an admin, always an implicit collaborator — no explicit
    # ProjectMember row needed (see list_project_members).
    return ProjectRead.model_validate(p)


@router.get("/", response_model=List[ProjectRead])
def list_projects(
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=1000),
    current_user: User = Depends(allow_read_only),
    db: Session = Depends(get_db_dependency),
):
    items = db.query(Project).offset(skip).limit(limit).all()
    return [ProjectRead.model_validate(i) for i in items]


@router.get("/{project_id}", response_model=ProjectRead)
def get_project(
    project_id: int,
    current_user: User = Depends(allow_read_only),
    db: Session = Depends(get_db_dependency)
):
    p = db.query(Project).filter(Project.id == project_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")
    return ProjectRead.model_validate(p)


@router.post("/{project_id}/add_record/{rec_id}")
def add_record_to_project(
    project_id: int,
    rec_id: int,
    current_user: User = Depends(allow_contributor),
    db: Session = Depends(get_db_dependency)
):
    p = db.query(Project).filter(Project.id == project_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")
    # Locked and guarded for the same reason the other move routes are: this
    # changes a record's parent, and doing that to a handed-over record
    # withdraws it from review one record at a time (NEH-260). An adversarial
    # round reached the rule through here in two calls while the other two
    # routes were guarded.
    r = _locked_record(db, rec_id, with_images=False)
    if not r:
        raise HTTPException(status_code=404, detail="Record not found")
    if (r.project_id, r.collection_id) != (p.id, None):
        assert_movable(r)
        # A record whose images are in the bucket stays in its volume: their
        # keys are under the volume's stem and never change. Checked under the
        # record's lock, which the uploader takes before it writes a first
        # offsite row (image_upload._claim). A record already outside every
        # volume keeps none here, so only its project changes, as PATCH
        # /records/{id} allows; its way back into a volume is PATCH's.
        if r.collection_id is not None:
            from app.core.image_upload import STORED_OFFSITE_MESSAGE, records_stored_offsite
            if records_stored_offsite(db, [r.id]):
                raise HTTPException(status_code=409, detail=STORED_OFFSITE_MESSAGE)
    # A record has one parent: attaching it to a project detaches it from any collection
    r.project_id = p.id
    r.collection_id = None
    db.add(r)
    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
        raise integrity_conflict(e)
    return {"detail": "record added"}


@router.post("/{project_id}/remove_record/{rec_id}")
def remove_record_from_project(
    project_id: int,
    rec_id: int,
    current_user: User = Depends(allow_contributor),
    db: Session = Depends(get_db_dependency)
):
    p = db.query(Project).filter(Project.id == project_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")
    r = db.query(Record).filter(
        Record.id == rec_id, Record.project_id == p.id
    ).with_for_update(of=Record).first()
    if not r:
        raise HTTPException(status_code=404, detail="Record not found on this project")
    # Detaching is a parent change too, and it orphans the record (NEH-260).
    assert_movable(r)
    r.project_id = None
    db.add(r)
    db.commit()
    return {"detail": "record removed"}


def _refuse_a_name_that_is_a_stem(db: Session, name: str) -> None:
    """409 when the project's directory would be named as a volume's stem (app.models.collection)."""
    message = stem_directory_collision(db, project_name=name)
    if message:
        raise HTTPException(status_code=409, detail=message)


@router.put("/{project_id}", response_model=ProjectRead)
def update_project(
    project_id: int,
    payload: ProjectUpdate,
    current_user: User = Depends(allow_contributor),
    db: Session = Depends(get_db_dependency)
):
    """Update a project's details, moving its on-disk tree if the name changes.

    A rename is refused with 409 while an export of any volume with files in
    the project's tree is queued or running (collections_in_old_layout).
    Migrated volumes keep their files in `<stem>/`, outside the tree, and a
    rename touches none of them. Files of a volume given a stem since the last
    startup are still in the tree: they move with it and their rows follow.
    """
    p = db.query(Project).filter(Project.id == project_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")

    # Check for name conflicts if name is being changed
    old_name = p.name
    name_changed = bool(payload.name) and payload.name != old_name
    if name_changed:
        existing = db.query(Project).filter(Project.name == payload.name).first()
        if existing:
            raise HTTPException(status_code=409, detail="Project with this name already exists")
        # Before the tree moves: its new directory may not be a volume's stem.
        _refuse_a_name_that_is_a_stem(db, payload.name)

    # A rename moves the project's whole tree, so every volume with files in
    # it is held against exports from before the change until the commit: a
    # volume without a stem, and one whose stem was set since the last
    # startup, which still has files there (collections_in_old_layout). A
    # migrated volume has its files in `<stem>/`, outside the tree.
    held_ids = collections_in_old_layout(db, collection_ids_under_project(db, p.id)) if name_changed else []
    with export_jobs.hold_for_file_changes(held_ids, EXPORT_IN_PROJECT_MESSAGE):
        return _update_project_held(db, p, payload, old_name, name_changed)


def _update_project_held(db: Session, p: Project, payload: ProjectUpdate, old_name: str, name_changed: bool) -> ProjectRead:
    """The body of update_project, run while the project's collections are held against exports."""
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(p, field, value)

    # The whole on-disk tree (images, manifest, packages, the name-based
    # directories of volumes) is keyed by name, so a rename moves the entire
    # directory as a unit and rewrites every stored path under it, those of a
    # volume given a stem since the last startup included. A migrated volume
    # has its files in `<stem>/` under the projects root, outside the tree: no
    # row of it is under old_root, so none is rewritten. This keeps
    # every capture and its manifest together and leaves no stranded old-name
    # directory for delete_project to miss later.
    moved = False
    wrote_journal = False
    old_root = new_root = None
    if name_changed:
        from capture.project_manager import secure_project_filename, project_capture_root
        from app.core.storage_ops import write_rename_journal, clear_rename_journal

        # secure_project_filename collapses many display names onto one directory,
        # so only touch the disk when the directory name actually changes.
        if secure_project_filename(old_name) != secure_project_filename(p.name):
            old_root = project_capture_root(old_name)
            new_root = project_capture_root(p.name)
            # Record intent durably before the move so a crash between the move
            # and the commit is repairable at startup rather than left dangling.
            write_rename_journal(p.id, old_name, p.name, old_root, new_root)
            wrote_journal = True
            try:
                moved = move_project_tree(old_root, new_root)
            except FileExistsError:
                clear_rename_journal()
                raise HTTPException(status_code=409, detail="A project directory with this name already exists on disk")
            except OSError as e:
                clear_rename_journal()
                logger.error(f"Failed to move project directory {old_root} -> {new_root}: {e}")
                raise HTTPException(status_code=500, detail="Failed to move project files on disk")

            if moved:
                # Rewrite absolute paths for every image that lived under old_root
                # (captures under the project or any of its collections).
                for img in db.query(RecordImage).all():
                    new_fp = rebase_stored_path(img.file_path, old_root, new_root)
                    if new_fp:
                        img.file_path = new_fp
                    new_tp = rebase_stored_path(img.thumbnail_path, old_root, new_root)
                    if new_tp:
                        img.thumbnail_path = new_tp

    db.add(p)
    try:
        db.commit()
    except Exception:
        db.rollback()
        # Undo the on-disk move so the filesystem matches the rolled-back DB.
        if moved and old_root is not None:
            try:
                move_project_tree(new_root, old_root)
            except OSError:
                logger.exception("Failed to roll back project directory move; startup reconciliation will repair")
        if wrote_journal:
            clear_rename_journal()
        raise
    if wrote_journal:
        clear_rename_journal()
    db.refresh(p)
    return ProjectRead.model_validate(p)


class MoveCollectionsRequest(BaseModel):
    target_project_id: int


@router.post("/{project_id}/move-collections")
def move_collections(
    project_id: int,
    payload: MoveCollectionsRequest,
    current_user: User = Depends(allow_contributor),
    db: Session = Depends(get_db_dependency)
):
    """
    Move all top-level collections from this project to another project.
    Sub-collections follow automatically through their parent FK.

    The files of each moved volume that are in the name-based layout (every
    file of a volume without a stem, and those of a volume given a stem since
    the last startup) move to its name-based directory under the target
    project, and the rows follow; files in `<stem>/` stay where they are. Refused with 409 while an export of
    any of the moved collections is queued or running.
    """
    from capture.project_manager import image_output_dir

    source = db.query(Project).filter(Project.id == project_id).first()
    if not source:
        raise HTTPException(status_code=404, detail="Source project not found")
    target = db.query(Project).filter(Project.id == payload.target_project_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Target project not found")
    if project_id == payload.target_project_id:
        raise HTTPException(status_code=400, detail="Source and target project must be different")

    # Full moved subtree (top-level collections plus their nested descendants)
    moved_collection_ids: list[int] = []
    for c in db.query(Collection).filter(Collection.project_id == project_id).all():
        moved_collection_ids.extend(collection_and_descendants(db, c.id))

    # The moved volumes' files in the old layout leave the paths an export of
    # any of them reads, so those volumes are held against exports until the
    # move is committed. A migrated volume moves no file and is not held.
    old_layout = collections_in_old_layout(db, moved_collection_ids)
    top_level = [(c.id, c.name) for c in
                 db.query(Collection).filter(Collection.project_id == project_id).order_by(Collection.id).all()]
    with export_jobs.hold_for_file_changes(old_layout, EXPORT_IN_PROJECT_MESSAGE):
        # Re-parent the top-level collections; descendants follow via parent FK
        moved = (
            db.query(Collection)
            .filter(Collection.project_id == project_id)
            .update({"project_id": payload.target_project_id}, synchronize_session=False)
        )

        # Relocate the image files so they physically live under the target project
        not_moved: list[str] = []
        moves: list = []
        # Only files in the name-based layout are under the project's
        # directory; they move to the volume's name-based directory under the
        # target. Files in `<stem>/` stay where they are.
        cols = {c.id: c for c in db.query(Collection).filter(Collection.id.in_(old_layout)).all()} \
            if old_layout else {}
        if cols:
            records = db.query(Record).filter(Record.collection_id.in_(list(cols))).all()
            for r in records:
                c = cols[r.collection_id]
                target_dir = image_output_dir(target.name, c.name)
                moves.extend((img, target_dir) for img in r.images if image_in_old_layout(c, img.file_path))

        # A failed move or commit puts every file already moved back.
        relocate_images_and_commit(
            db, moves, not_moved,
            f"Move of collections from project {project_id} to {payload.target_project_id}",
        )

    for volume_id, volume_name in top_level:
        log_event(db, level="INFO", category="activity", action="collection_moved",
                  actor=current_user.username, subject=volume_name,
                  detail=(f"id={volume_id}; project {project_id!r} -> {payload.target_project_id!r}; "
                          f"parent None -> None"))
    log_event(db, level="INFO", category="activity", action="collections_moved",
              actor=current_user.username,
              subject=f"{source.name} -> {target.name} ({moved} collections)")
    body = {"moved": moved, "target_project_id": payload.target_project_id}
    if not_moved:
        # Preview sidecars that stayed in the old directory (see relocate_image)
        body["not_moved"] = not_moved
    return body


@router.delete("/{project_id}")
def delete_project(
    project_id: int,
    current_user: User = Depends(allow_admin),
    db: Session = Depends(get_db_dependency)
):
    """
    Delete an empty project and its (now unused) directory.

    The project's capture manifest is not deleted: it is moved to
    `<projects root>/.retired-manifests/` first (retire_project_manifest),
    where the fixity index keeps reading it.

    Refuses (409) if the project still has collections or records (those must
    be moved or deleted first) or if its directory still holds files that a
    surviving record points at. A move relocates files, so the honest flow is
    "move contents out, then delete the emptied project".
    """
    from capture.project_manager import project_capture_root

    p = db.query(Project).filter(Project.id == project_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")

    project_name = p.name

    # Refuse to delete a non-empty project; report the blocking counts
    collection_count = db.query(func.count(Collection.id)).filter(Collection.project_id == project_id).scalar()
    record_count = db.query(func.count(Record.id)).filter(Record.project_id == project_id).scalar()
    if collection_count or record_count:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot delete a non-empty project: {collection_count} collection(s) and {record_count} record(s) remain. Move or delete them first.",
        )

    project_dir = project_capture_root(project_name)

    # Hard guard: never remove a directory that still holds a surviving record's files
    if surviving_images_under(db, project_dir):
        raise HTTPException(
            status_code=409,
            detail="Project directory still holds files referenced by other records. Move those records first.",
        )

    # A capture manifest is never deleted: it may hold the capture-time
    # checksums of volumes that have left the project. The project's own is
    # retired out of the tree first; any other one in the tree stops the delete.
    others = [m for m in project_dir.glob("**/metadata/manifest.jsonl")
              if m != project_dir / "metadata" / "manifest.jsonl"] if project_dir.is_dir() else []
    if others:
        raise HTTPException(
            status_code=409,
            detail="Project directory holds a capture manifest other than its own. Move it out first.",
        )
    try:
        retire_project_manifest(project_dir)
    except OSError as e:
        logger.error(f"Failed to retire the capture manifest of {project_dir}: {e}")
        raise HTTPException(status_code=500, detail="Failed to keep the project's capture manifest; nothing was deleted")

    # Remove files first so a filesystem failure aborts before touching the DB
    try:
        remove_tree(project_dir)
    except OSError as e:
        logger.error(f"Failed to remove project directory {project_dir}: {e}")
        raise HTTPException(status_code=500, detail="Failed to remove project files from disk")

    db.delete(p)
    db.commit()
    log_event(db, level="WARN", category="activity", action="project_deleted",
              actor=current_user.username, subject=project_name)

    return {"detail": "project deleted"}


@router.post("/{project_id}/initialize", response_model=ProjectInitResponse)
def initialize_project_filesystem(
    project_id: int,
    request: ProjectInitRequest,
    current_user: User = Depends(allow_contributor),
    db: Session = Depends(get_db_dependency)
):
    """
    Initialize the filesystem structure for a project.
    
    Creates the directory structure and camera configurations needed
    for capturing images. Should be called after creating a project
    and before starting captures.
    """
    p = db.query(Project).filter(Project.id == project_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")
    
    try:
        from capture.project_manager import project_init
    except ImportError as e:
        return ProjectInitResponse(
            success=False,
            error=f"Project manager not available: {e}"
        )
    
    try:
        project_path = project_init(
            project_name=p.name,
            default_resolution=request.resolution
        )
        return ProjectInitResponse(
            success=True,
            project_path=str(project_path)
        )
    except Exception as e:
        logger.exception(f"Failed to initialize project filesystem: {e}")
        return ProjectInitResponse(success=False, error=str(e))


@router.get("/{project_id}/records", response_model=List)
def list_project_records(
    project_id: int,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=1000),
    current_user: User = Depends(allow_read_only),
    db: Session = Depends(get_db_dependency)
):
    """List all records associated with a project."""
    p = db.query(Project).filter(Project.id == project_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")
    
    from app.schemas.record import RecordRead
    recs = db.query(Record).filter(
        Record.project_id == project_id
    ).offset(skip).limit(limit).all()
    
    return [RecordRead.model_validate(r) for r in recs]


# ---------------------------------------------------------------------------
# MEMBER ENDPOINTS
# ---------------------------------------------------------------------------

@router.get("/{project_id}/members", response_model=List[ProjectMemberRead])
def list_project_members(
    project_id: int,
    current_user: User = Depends(allow_read_only),
    db: Session = Depends(get_db_dependency),
):
    """List all logical collaborators for a project.

    Returns:
    - Implicit: all active admins (always collaborators, cannot be removed)
    - Explicit: users added via project_members (operators / reviewers)
    Admins who also appear in project_members are shown only as implicit.
    """
    p = db.query(Project).filter(Project.id == project_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")

    # Implicit: all active admins
    admins = db.query(User).filter(User.role == "admin", User.is_active == True).all()
    admin_ids = {u.id for u in admins}
    implicit = [
        ProjectMemberRead(
            project_id=project_id,
            user_id=u.id,
            role="admin",
            added_at=p.created_at,
            added_by=None,
            username=u.username,
            email=u.email,
            is_implicit=True,
        )
        for u in admins
    ]

    # Explicit: project_members who are not already counted as admins
    rows = (
        db.query(ProjectMember, User)
        .join(User, User.id == ProjectMember.user_id)
        .filter(ProjectMember.project_id == project_id)
        .all()
    )
    explicit = [
        ProjectMemberRead(
            project_id=m.project_id,
            user_id=m.user_id,
            role=m.role,
            added_at=m.added_at,
            added_by=m.added_by,
            username=u.username,
            email=u.email,
            is_implicit=False,
        )
        for m, u in rows
        if u.id not in admin_ids  # admins are already listed as implicit
    ]

    return implicit + explicit


@router.post("/{project_id}/members", response_model=ProjectMemberRead, status_code=201)
def add_project_member(
    project_id: int,
    payload: ProjectMemberCreate,
    current_user: User = Depends(allow_admin),
    db: Session = Depends(get_db_dependency),
):
    """Add a user to a project with a given role (operator|reviewer). Admin only."""
    p = db.query(Project).filter(Project.id == project_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")

    if payload.role not in ("operator", "reviewer"):
        raise HTTPException(status_code=422, detail="role must be 'operator' or 'reviewer'")

    target_user = db.query(User).filter(User.id == payload.user_id).first()
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found")

    existing = db.query(ProjectMember).filter(
        ProjectMember.project_id == project_id,
        ProjectMember.user_id    == payload.user_id,
    ).first()
    if existing:
        # Update role if already a member
        existing.role = payload.role
        db.commit()
        db.refresh(existing)
        m = existing
    else:
        m = ProjectMember(
            project_id=project_id,
            user_id=payload.user_id,
            role=payload.role,
            added_by=current_user.username,
        )
        db.add(m)
        db.commit()
        db.refresh(m)

    return ProjectMemberRead(
        project_id=m.project_id,
        user_id=m.user_id,
        role=m.role,
        added_at=m.added_at,
        added_by=m.added_by,
        username=target_user.username,
        email=target_user.email,
    )


@router.delete("/{project_id}/members/{user_id}", status_code=204)
def remove_project_member(
    project_id: int,
    user_id: int,
    current_user: User = Depends(allow_admin),
    db: Session = Depends(get_db_dependency),
):
    """Remove a user from a project. Admin only."""
    p = db.query(Project).filter(Project.id == project_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")

    m = db.query(ProjectMember).filter(
        ProjectMember.project_id == project_id,
        ProjectMember.user_id    == user_id,
    ).first()
    if not m:
        raise HTTPException(status_code=404, detail="Member not found")
    db.delete(m)
    db.commit()