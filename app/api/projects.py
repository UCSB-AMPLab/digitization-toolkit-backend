from fastapi import APIRouter, Depends, HTTPException, Query
from typing import List, Optional
from sqlalchemy.orm import Session
from pydantic import BaseModel
import logging

from app.api.deps import get_db_dependency
from app.api.auth import get_current_user
from app.models.project import Project
from app.models.document import DocumentImage
from app.models.user import User
from app.schemas.project import ProjectCreate, ProjectRead, ProjectBase, ProjectUpdate

router = APIRouter()
logger = logging.getLogger(__name__)


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
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db_dependency)
):
    if db.query(Project).filter(Project.name == payload.name).first():
        raise HTTPException(status_code=409, detail="Project with this name already exists")
    p = Project(name=payload.name, description=payload.description, created_by=payload.created_by or current_user.username)
    db.add(p)
    db.commit()
    db.refresh(p)
    return ProjectRead.model_validate(p)


@router.get("/", response_model=List[ProjectRead])
def list_projects(
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=1000),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db_dependency),
):
    items = db.query(Project).offset(skip).limit(limit).all()
    return [ProjectRead.model_validate(i) for i in items]


@router.get("/{project_id}", response_model=ProjectRead)
def get_project(
    project_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db_dependency)
):
    p = db.query(Project).filter(Project.id == project_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")
    return ProjectRead.model_validate(p)


@router.post("/{project_id}/add_document/{doc_id}")
def add_document_to_project(
    project_id: int,
    doc_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db_dependency)
):
    p = db.query(Project).filter(Project.id == project_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")
    d = db.query(DocumentImage).filter(DocumentImage.id == doc_id).first()
    if not d:
        raise HTTPException(status_code=404, detail="Document not found")
    d.project_id = p.id
    db.add(d)
    db.commit()
    return {"detail": "document added"}


@router.post("/{project_id}/remove_document/{doc_id}")
def remove_document_from_project(
    project_id: int,
    doc_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db_dependency)
):
    p = db.query(Project).filter(Project.id == project_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")
    d = db.query(DocumentImage).filter(DocumentImage.id == doc_id, DocumentImage.project_id == p.id).first()
    if not d:
        raise HTTPException(status_code=404, detail="Document not found on this project")
    d.project_id = None
    db.add(d)
    db.commit()
    return {"detail": "document removed"}


@router.put("/{project_id}", response_model=ProjectRead)
def update_project(
    project_id: int,
    payload: ProjectUpdate,
    current_user: User = Depends(get_current_user),
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


@router.delete("/{project_id}")
def delete_project(
    project_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db_dependency)
):
    """
    Delete a project.
    
    Note: This only deletes the database record. Associated documents
    are unlinked but not deleted. Filesystem cleanup must be done separately.
    """
    p = db.query(Project).filter(Project.id == project_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")
    
    # Unlink all documents from this project
    db.query(DocumentImage).filter(DocumentImage.project_id == project_id).update(
        {"project_id": None}
    )
    
    db.delete(p)
    db.commit()
    return {"detail": "project deleted"}


@router.post("/{project_id}/initialize", response_model=ProjectInitResponse)
def initialize_project_filesystem(
    project_id: int,
    request: ProjectInitRequest,
    current_user: User = Depends(get_current_user),
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


@router.get("/{project_id}/documents", response_model=List)
def list_project_documents(
    project_id: int,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=1000),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db_dependency)
):
    """List all documents associated with a project."""
    p = db.query(Project).filter(Project.id == project_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")
    
    from app.schemas.document import DocumentRead
    docs = db.query(DocumentImage).filter(
        DocumentImage.project_id == project_id
    ).offset(skip).limit(limit).all()
    
    return [DocumentRead.model_validate(d) for d in docs]