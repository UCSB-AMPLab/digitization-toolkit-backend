from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional
from datetime import datetime, timezone
from pathlib import Path
import uuid
import platform
import socket


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

    software: Dict = field(default_factory=dict) # version, git commit, etc.

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
