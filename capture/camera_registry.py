"""
Camera registry and hardware identification system.

Manages physical camera identification, calibration, and configuration
at a global level (PROJECTS_ROOT/cameras.json).
"""
import json
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Optional, List, Tuple
from picamera2 import Picamera2


class CameraRegistry:
    """
    Manages global camera registry with hardware identification.
    
    Cameras are identified by their unique hardware ID (model + serial),
    not by their current index (0, 1). This allows robust handling of:
    - Cable swapping
    - Camera replacement
    - Per-camera calibration that persists
    """
    
    def __init__(self, registry_path: Optional[Path] = None):
        """
        Initialize camera registry.
        
        Args:
            registry_path: Path to cameras.json. Defaults to PROJECTS_ROOT/cameras.json
        """
        if registry_path is None:
            from app.core.config import settings
            registry_path = Path(settings.projects_dir) / "cameras.json"
        
        self.registry_path = Path(registry_path)
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        self.cameras = self._load_registry()
    
    def _load_registry(self) -> Dict:
        """Load camera registry from disk."""
        if self.registry_path.exists():
            with open(self.registry_path, 'r') as f:
                return json.load(f)
        return {"cameras": {}, "version": "1.0"}
    
    def _save_registry(self):
        """Save camera registry to disk."""
        with open(self.registry_path, 'w') as f:
            json.dump(self.cameras, f, indent=2)
    
    @staticmethod
    def get_camera_hardware_id(camera_index: int) -> Tuple[Optional[str], Dict]:
        """
        Get hardware ID and info for a camera at given index.
        
        Args:
            camera_index: Current camera index (0, 1, etc.)
            
        Returns:
            Tuple of (hardware_id, camera_info_dict)
            hardware_id format: "model_i2cbus" using Id field
        """
        try:
            camera_info = Picamera2.global_camera_info()
            
            if camera_index >= len(camera_info):
                return None, {}
            
            info = camera_info[camera_index]
            model = info.get('Model', 'unknown')
            camera_id = info.get('Id', '')
            location = info.get('Location', '')
            
            # Build stable hardware ID from Id path
            # Example Id: '/base/axi/pcie@1000120000/rp1/i2c@88000/imx519@1a'
            # We use the i2c bus identifier (e.g., "88000")
            if camera_id:
                id_parts = camera_id.split('/')
                i2c_part = [p for p in id_parts if p.startswith('i2c@')]
                if i2c_part:
                    identifier = i2c_part[0].replace('i2c@', '')
                    hw_id = f"{model}_{identifier}"
                else:
                    # Fallback to last part of Id
                    hw_id = f"{model}_{id_parts[-1]}"
            else:
                # Ultimate fallback
                hw_id = f"{model}_idx{camera_index}"
            
            return hw_id, {
                "model": model,
                "serial": None,  # IMX519 doesn't expose serial number
                "location": str(location),
                "id": camera_id,
                "index": camera_index
            }
        
        except Exception as e:
            return None, {"error": str(e)}
    
    def detect_cameras(self) -> Dict[int, Tuple[str, Dict]]:
        """
        Detect all connected cameras and return their hardware IDs.
        
        Returns:
            Dict mapping camera_index -> (hardware_id, info)
        """
        detected = {}
        camera_info = Picamera2.global_camera_info()
        
        for idx in range(len(camera_info)):
            hw_id, info = self.get_camera_hardware_id(idx)
            if hw_id:
                detected[idx] = (hw_id, info)
        
        return detected
    
    def register_camera(
        self,
        camera_index: int,
        calibration_data: Optional[Dict] = None,
        force: bool = False
    ) -> Optional[str]:
        """
        Register a camera in the global registry.
        
        Args:
            camera_index: Current camera index
            calibration_data: Optional calibration data to store
            force: Force re-registration even if already exists
            
        Returns:
            Hardware ID of registered camera, or None if failed
        """
        hw_id, info = self.get_camera_hardware_id(camera_index)
        
        if not hw_id:
            return None
        
        now = datetime.now(timezone.utc).isoformat()
        
        # Check if camera already registered
        if hw_id in self.cameras["cameras"] and not force:
            # Update last_seen info
            self.cameras["cameras"][hw_id]["last_seen_index"] = camera_index
            self.cameras["cameras"][hw_id]["last_seen_at"] = now
        else:
            # New registration
            self.cameras["cameras"][hw_id] = {
                "model": info.get("model"),
                "serial": info.get("serial"),
                "location": info.get("location"),
                "machine_id": None,  # User-assigned ID (e.g., "CAM-001", "LEFT")
                "label": None,  # Human-readable description
                "last_seen_index": camera_index,
                "first_registered_at": now,
                "last_seen_at": now,
                "calibration": calibration_data or {}
            }
        
        self._save_registry()
        return hw_id
    
    def get_camera_by_id(self, hardware_id: str) -> Optional[Dict]:
        """Get camera data by hardware ID."""
        return self.cameras["cameras"].get(hardware_id)
    
    def get_camera_by_index(self, camera_index: int) -> Optional[Tuple[str, Dict]]:
        """
        Get camera data by current index.
        
        Returns:
            Tuple of (hardware_id, camera_data) or (None, None)
        """
        hw_id, _ = self.get_camera_hardware_id(camera_index)
        if hw_id:
            camera_data = self.get_camera_by_id(hw_id)
            return hw_id, camera_data
        return None, None
    
    def update_calibration(self, hardware_id: str, calibration_data: Dict):
        """Update calibration data for a camera."""
        if hardware_id in self.cameras["cameras"]:
            self.cameras["cameras"][hardware_id]["calibration"] = calibration_data
            self.cameras["cameras"][hardware_id]["calibrated_at"] = \
                datetime.now(timezone.utc).isoformat()
            self._save_registry()
    
    def set_camera_info(self, hardware_id: str, machine_id: Optional[str] = None, label: Optional[str] = None):
        """
        Set user-assigned identification for a camera.
        
        Useful for cameras without serial numbers (e.g., IMX519).
        The machine_id should match a physical label on the camera.
        
        Args:
            hardware_id: Hardware ID of the camera
            machine_id: User-assigned ID (e.g., "CAM-001", "LEFT", "A", "B")
            label: Human-readable description (e.g., "Left scanner camera")
            
        Example:
            registry.set_camera_info("imx519_88000", machine_id="CAM-L", label="Left Scanner")
        """
        if hardware_id in self.cameras["cameras"]:
            if machine_id is not None:
                self.cameras["cameras"][hardware_id]["machine_id"] = machine_id
            if label is not None:
                self.cameras["cameras"][hardware_id]["label"] = label
            self._save_registry()
    
    def list_cameras(self) -> List[Dict]:
        """List all registered cameras."""
        return list(self.cameras["cameras"].values())
    
    def get_current_camera_mapping(self) -> Dict[int, str]:
        """
        Get mapping of current indices to hardware IDs.
        
        Returns:
            Dict mapping index -> hardware_id for currently connected cameras
        """
        detected = self.detect_cameras()
        return {idx: hw_id for idx, (hw_id, _) in detected.items()}


def initialize_camera_system(run_calibration: bool = True) -> Dict:
    """
    Initialize the camera system on a new machine.
    
    This detects all cameras, registers them, and optionally calibrates them.
    
    Args:
        run_calibration: Whether to run calibration for each camera
        
    Returns:
        Dictionary with registration results
    """
    from .calibration import CameraCalibration
    
    registry = CameraRegistry()
    detected = registry.detect_cameras()
    
    results = {
        "detected_count": len(detected),
        "cameras": {}
    }
    
    print(f"\n{'='*70}")
    print("CAMERA SYSTEM INITIALIZATION")
    print(f"{'='*70}")
    print(f"Detected {len(detected)} camera(s)\n")
    
    for idx, (hw_id, info) in detected.items():
        print(f"Camera {idx}: {info['model']}")
        print(f"  Hardware ID: {hw_id}")
        
        # Register camera
        registered_id = registry.register_camera(idx)
        
        # Run calibration if requested
        if run_calibration:
            print(f"  Running calibration...")
            cal = CameraCalibration(idx)
            focus_result = cal.calibrate_focus(verbose=False)
            
            if focus_result["success"]:
                print(f"  ✅ Focus calibrated: {focus_result['lens_position']:.2f} dioptres")
                registry.update_calibration(hw_id, cal.calibration_data)
            else:
                print(f"  ❌ Calibration failed")
        
        results["cameras"][idx] = {
            "hardware_id": hw_id,
            "model": info['model'],
            "registered": registered_id is not None
        }
        
        print()
    
    print(f"{'='*70}")
    print(f"Registry saved to: {registry.registry_path}")
    print(f"{'='*70}\n")
    
    return results
