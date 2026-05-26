from fastapi import APIRouter, Depends, HTTPException, Query
from typing import List, Optional
from sqlalchemy.orm import Session
from pydantic import BaseModel
import logging

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
    # Auto-add creator as explicit member (unless they're an admin — admins are always implicit)
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
    """Update a project's details."""
    p = db.query(Project).filter(Project.id == project_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")
    
    # Check for name conflicts if name is being changed
    if payload.name and payload.name != p.name:
        existing = db.query(Project).filter(Project.name == payload.name).first()
        if existing:
            raise HTTPException(status_code=409, detail="Project with this name already exists")
    
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(p, field, value)
    
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
    source = db.query(Project).filter(Project.id == project_id).first()
    if not source:
        raise HTTPException(status_code=404, detail="Source project not found")
    target = db.query(Project).filter(Project.id == payload.target_project_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Target project not found")
    if project_id == payload.target_project_id:
        raise HTTPException(status_code=400, detail="Source and target project must be different")

    moved = (
        db.query(Collection)
        .filter(Collection.project_id == project_id)
        .update({"project_id": payload.target_project_id}, synchronize_session=False)
    )
    db.commit()
    log_event(db, level="INFO", category="activity", action="collections_moved",
              actor=current_user.username,
              subject=f"{source.name} → {target.name} ({moved} collections)")
    return {"moved": moved, "target_project_id": payload.target_project_id}


@router.delete("/{project_id}")
def delete_project(
    project_id: int,
    current_user: User = Depends(allow_contributor),
    db: Session = Depends(get_db_dependency)
):
    """
    Delete a project and its filesystem directory.

    Deletes the DB record, unlinks associated records, then removes the
    project directory (including all collection subdirectories and images).
    """
    import shutil
    from app.core.config import settings
    from capture.project_manager import secure_project_filename

    p = db.query(Project).filter(Project.id == project_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")

    project_name = p.name
    # Unlink all records from this project
    db.query(Record).filter(Record.project_id == project_id).update(
        {"project_id": None}
    )

    db.delete(p)
    db.commit()
    log_event(db, level="WARN", category="activity", action="project_deleted",
              actor=current_user.username, subject=project_name)

    # Remove the project directory from disk.
    # Two candidate paths are tried because project_init() sanitizes the name
    # (spaces → underscores) while capture_image() uses the raw name directly.
    safe_name = secure_project_filename(project_name)
    candidates = [settings.projects_dir / project_name]
    if safe_name != project_name:
        candidates.append(settings.projects_dir / safe_name)

    for candidate in candidates:
        if candidate.exists() and candidate.is_dir():
            try:
                shutil.rmtree(candidate)
                logger.info(f"Removed project directory: {candidate}")
            except Exception as e:
                logger.warning(f"Could not remove project directory {candidate}: {e}")
            break

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