from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional, Union
from datetime import datetime, timezone
from pathlib import Path
import uuid
import platform
import socket
import os
import json
import sys

backend_dir = Path(__file__).parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from .utils import compute_sha256, setup_rotating_logger

from app.core.config import settings

LOG_FILE = settings.log_dir / "capture_service.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

subprocess_logger = setup_rotating_logger(
    log_file=str(LOG_FILE),
    logger_name="capture_service"
)

@dataclass
class ProjectInfo:
    """
    General information about a project.
    """
    project_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    project_name: str = ""
    created_utc: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    created_by: str = field(default_factory=lambda: socket.gethostname())
    paths: Dict = field(default_factory=dict)  # e.g. images, metadata dirs
    
    software: Dict = field(default_factory=dict) # version, git commit, etc.
    
    hardware: Dict = field(default_factory=lambda: {
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "machine": platform.machine(),
    })
    
    default_camera_config: Optional[Dict] = None  # CameraConfig.to_dict()
    
    def to_dict(self) -> Dict:
        """Convert to dictionary for serialization."""
        return asdict(self)

@dataclass
class CaptureFile:
    """
    Metadata about one file produced in a capture.
    """
    role: str                    # e.g. "left", "right", "single"
    relative_path: str           # path relative to project capture dir
    bytes: int
    mimetype: str = "image/jpeg"
    sha256: Optional[str] = None


@dataclass
class CaptureCamera:
    """
    Metadata about one camera used in a capture.
    """
    camera_index: int
    config: Dict                 # CameraConfig.to_dict()
    model: Optional[str] = None
    serial: Optional[str] = None


@dataclass
class CaptureRecord:
    """
    Append-only record describing a single capture event.
    """

    capture_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    project_name: str = ""
    pair_id: Optional[str] = None          # shared ID for dual capture
    sequence: Optional[str] = None         # e.g. "0001", "page_01"

    timestamp_utc: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    files: List[CaptureFile] = field(default_factory=list)

    cameras: List[CaptureCamera] = field(default_factory=list)
    timing: Dict = field(default_factory=dict)   # durations, timestamps, etc.

    # --- Host (relevant for future implementations) ---
    host: Dict = field(default_factory=lambda: {
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "machine": platform.machine(),
    })

    status: str = "success"                 # success | failed | partial
    warnings: List[str] = field(default_factory=list)
    error: Optional[str] = None

    def to_dict(self) -> Dict:
        return asdict(self)


#### Helper functions ####

def generate_manifest_project(
    project_name: str,
    paths: Dict[str, str] = None,
    created_by: str = None,
    default_camera_config: Optional[Dict] = None
) -> ProjectInfo:
    """
    Generate a ProjectInfo object for a new project.
    
    Args:
        project_name: Name of the project
        default_camera_config: Optional default CameraConfig.to_dict()
    """
    return ProjectInfo(
        project_name=project_name,
        created_by=created_by or socket.gethostname(),
        paths=paths or {},
        software={
            "tool": "digitization-toolkit",
            "version": settings.app_version,
        },
        default_camera_config=default_camera_config
    )

def generate_manifest_record(
    project_name: str,
    img_paths: list,
    cam_configs: list,
    times: list = None,
    pair_id: str = None,
    stagger: int = None,
    roles: list = None) -> CaptureRecord:
    """
    Generate a manifest record for single or dual captures.
    
    Args:
        project_name: Name of the project
        img_paths: List of captured image paths
        cam_configs: List of CameraConfig objects used
        times: List of capture times in seconds (optional)
        pair_id: Shared ID for dual captures (optional, auto-generated)
        stagger: Delay between camera starts in ms (optional)
        roles: List of role names (e.g., ["left", "right"] or ["single"])
               If None, auto-assigns based on number of captures
    
    Returns:
        CaptureRecord object
    """
    # Auto-assign roles if not provided
    if roles is None:
        if len(img_paths) == 1:
            roles = ["single"]
        elif len(img_paths) == 2:
            roles = ["left", "right"]
        else:
            roles = [f"cam{i}" for i in range(len(img_paths))]
    
    # Build files list
    files = []
    for i, (path, config, role) in enumerate(zip(img_paths, cam_configs, roles)):
        files.append(CaptureFile(
            role=role,
            relative_path=str(Path("images/main") / Path(path).name),
            bytes=os.path.getsize(path),
            mimetype=f"image/{config.encoding}",
            sha256=compute_sha256(path)
        ))
    
    # Build cameras list
    cameras = []
    for config in cam_configs:
        cameras.append(CaptureCamera(
            camera_index=config.camera_index,
            config=config.to_dict()
        ))
    
    # Build timing dict
    timing = {}
    if times:
        for i, t in enumerate(times):
            timing[f'camera{i+1}_seconds'] = t
    if stagger is not None:
        timing['stagger_ms'] = stagger
    
    return CaptureRecord(
        project_name=project_name,
        pair_id=pair_id,
        files=files,
        cameras=cameras,
        timing=timing,
    )
    

def append_manifest_record(project_root: Path, record: Union[CaptureRecord, ProjectInfo], record_type: str = "capture"):
    """
    Append a capture or project record to the manifest file in the project directory.
    
    Args:
        project_root (Path): The root directory of the project.
        record (CaptureRecord, ProjectInfo): The capture or project record to append.
    """
    
    metadata_dir = project_root / "metadata"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    
    if record_type == "project":
        manifest_path = metadata_dir / "project_manifest.jsonl"
    elif record_type == "capture":
        manifest_path = metadata_dir / "manifest.jsonl"
    else:
        raise ValueError("record_type must be 'capture' or 'project'")
    
    with open(manifest_path, 'a', encoding="utf-8") as f:
        f.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
        
        if record_type == "project":
            subprocess_logger.info(f"Appended project record for '{record.project_name}' to manifest.")
        else:
            subprocess_logger.info(f"Appended capture record {record.capture_id} to manifest.")
        