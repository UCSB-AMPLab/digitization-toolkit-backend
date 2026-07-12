from fastapi import APIRouter, Depends, HTTPException, Query
from typing import List, Optional
from sqlalchemy.orm import Session
from sqlalchemy import func
from pydantic import BaseModel
import logging

from app.core.storage_ops import (
    collection_and_descendants,
    move_project_tree,
    rebase_stored_path,
    relocate_image,
    remove_tree,
    surviving_images_under,
)

from app.api.deps import get_db_dependency
from app.api.auth import get_current_user, RoleChecker
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.record import Record, RecordImage
from app.models.collection import Collection
from app.models.user import User
from app.schemas.project import ProjectCreate, ProjectRead, ProjectBase, ProjectUpdate
from app.schemas.project_member import ProjectMemberCreate, ProjectMemberRead
from app.core.audit import log_event

router = APIRouter()
logger = logging.getLogger(__name__)

# Role checkers
allow_admin = RoleChecker(["admin"])
allow_contributor = RoleChecker(["admin", "operator"])
allow_read_only = RoleChecker(["admin", "operator", "reviewer"])


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
    current_user: User = Depends(allow_contributor),
    db: Session = Depends(get_db_dependency)
):
    if db.query(Project).filter(Project.name == payload.name).first():
        raise HTTPException(status_code=409, detail="Project with this name already exists")
    p = Project(name=payload.name, description=payload.description, fondo=payload.fondo, serie=payload.serie, signatura=payload.signatura, created_by=payload.created_by or current_user.username)
    db.add(p)
    db.commit()
    db.refresh(p)
    # Auto-add creator as explicit member (unless they're an admin - admins are always implicit)
    if current_user.role != "admin":
        member = ProjectMember(
            project_id=p.id,
            user_id=current_user.id,
            role=current_user.role,
            added_by="system",
        )
        db.add(member)
        db.commit()
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
    r = db.query(Record).filter(Record.id == rec_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="Record not found")
    r.project_id = p.id
    db.add(r)
    db.commit()
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
    r = db.query(Record).filter(Record.id == rec_id, Record.project_id == p.id).first()
    if not r:
        raise HTTPException(status_code=404, detail="Record not found on this project")
    r.project_id = None
    db.add(r)
    db.commit()
    return {"detail": "record removed"}


@router.put("/{project_id}", response_model=ProjectRead)
def update_project(
    project_id: int,
    payload: ProjectUpdate,
    current_user: User = Depends(allow_contributor),
    db: Session = Depends(get_db_dependency)
):
    """Update a project's details, moving its on-disk tree if the name changes."""
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

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(p, field, value)

    # The whole on-disk tree (images, manifest, packages, collection subdirs) is
    # keyed by name, so a rename moves the entire directory as a unit and rewrites
    # the stored paths. This keeps every capture and its manifest together and
    # leaves no stranded old-name directory for delete_project to miss later.
    if name_changed:
        from capture.project_manager import secure_project_filename, project_capture_root

        # secure_project_filename collapses many display names onto one directory,
        # so only touch the disk when the directory name actually changes.
        if secure_project_filename(old_name) != secure_project_filename(p.name):
            old_root = project_capture_root(old_name)
            new_root = project_capture_root(p.name)
            try:
                moved = move_project_tree(old_root, new_root)
            except FileExistsError:
                raise HTTPException(status_code=409, detail="A project directory with this name already exists on disk")
            except OSError as e:
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
    db.commit()
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

    # Re-parent the top-level collections; descendants follow via parent FK
    moved = (
        db.query(Collection)
        .filter(Collection.project_id == project_id)
        .update({"project_id": payload.target_project_id}, synchronize_session=False)
    )

    # Relocate the image files so they physically live under the target project
    if moved_collection_ids:
        names = {
            c.id: c.name
            for c in db.query(Collection).filter(Collection.id.in_(moved_collection_ids)).all()
        }
        records = db.query(Record).filter(Record.collection_id.in_(moved_collection_ids)).all()
        for r in records:
            target_dir = image_output_dir(target.name, names.get(r.collection_id))
            for img in r.images:
                relocate_image(img, target_dir)

    db.commit()
    log_event(db, level="INFO", category="activity", action="collections_moved",
              actor=current_user.username,
              subject=f"{source.name} -> {target.name} ({moved} collections)")
    return {"moved": moved, "target_project_id": payload.target_project_id}


@router.delete("/{project_id}")
def delete_project(
    project_id: int,
    current_user: User = Depends(allow_admin),
    db: Session = Depends(get_db_dependency)
):
    """
    Delete an empty project and its (now unused) directory.

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

def _assert_can_manage_members(project: Project, current_user: User, db: Session) -> None:
    """Raise 403 if current_user may not manage members for this project."""
    if current_user.role == "admin":
        return
    if current_user.role == "operator":
        # Operator may manage if they created the project or are already a member
        is_creator = project.created_by == current_user.username
        is_member  = db.query(ProjectMember).filter(
            ProjectMember.project_id == project.id,
            ProjectMember.user_id    == current_user.id
        ).first() is not None
        if is_creator or is_member:
            return
    raise HTTPException(status_code=403, detail="Not authorised to manage members for this project")


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
    current_user: User = Depends(allow_contributor),
    db: Session = Depends(get_db_dependency),
):
    """Add a user to a project with a given role (operator|reviewer)."""
    p = db.query(Project).filter(Project.id == project_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")
    _assert_can_manage_members(p, current_user, db)

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
    current_user: User = Depends(allow_contributor),
    db: Session = Depends(get_db_dependency),
):
    """Remove a user from a project."""
    p = db.query(Project).filter(Project.id == project_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")
    _assert_can_manage_members(p, current_user, db)

    m = db.query(ProjectMember).filter(
        ProjectMember.project_id == project_id,
        ProjectMember.user_id    == user_id,
    ).first()
    if not m:
        raise HTTPException(status_code=404, detail="Member not found")
    db.delete(m)
    db.commit()