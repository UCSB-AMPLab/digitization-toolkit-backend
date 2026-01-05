from fastapi import APIRouter, Depends, HTTPException, Query
from typing import List
from sqlalchemy.orm import Session

from app.api.deps import get_db_dependency
from app.api.auth import get_current_user
from app.models.project import Project
from app.models.document import DocumentImage
from app.models.user import User
from app.schemas.project import ProjectCreate, ProjectRead, ProjectBase

router = APIRouter()


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
# work in progress