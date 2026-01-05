"""
Camera calibration module for finding optimal settings.

This module provides calibration functions to determine optimal camera settings
for specific setups. Calibration profiles can be saved and loaded for reuse.
"""
import json
import time
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any
from picamera2 import Picamera2


class CameraCalibration:
    """
    Manages camera calibration for optimal settings.
    
    Supports:
    - Autofocus position calibration
    - White balance calibration (future)
    - Exposure calibration (future)
    """
    
    def __init__(self, camera_index: int = 0):
        """
        Initialize calibration for a specific camera.
        
        Args:
            camera_index: Index of the camera to calibrate (0 or 1)
        """
        self.camera_index = camera_index
        self.calibration_data = {
            "camera_index": camera_index,
            "calibrated_at": None,
            "focus": {},
            "white_balance": {},
            "exposure": {}
        }
    
    def calibrate_focus(self, img_size=(4624, 3472), verbose=True) -> Dict[str, Any]:
        """
        Run autofocus to find optimal lens position for current setup.
        
        This is useful for fixed-distance setups (e.g., book scanning)
        where the camera-to-subject distance doesn't change.
        
        Args:
            img_size: Image resolution tuple (width, height)
            verbose: Print progress messages
            
        Returns:
            Dictionary with focus calibration results:
            {
                "lens_position": float,  # Optimal position in dioptres
                "distance_meters": float,  # Approximate distance
                "af_time": float,  # Time autofocus took
                "success": bool  # Whether AF succeeded
            }
        """
        if verbose:
            print(f"\n{'='*70}")
            print(f"FOCUS CALIBRATION - Camera {self.camera_index}")
            print(f"{'='*70}")
            print("⚠️  Ensure subject is at normal working distance!")
            print(f"Resolution: {img_size[0]}x{img_size[1]}")
        
        picam2 = Picamera2(self.camera_index)
        config = picam2.create_still_configuration(main={"size": img_size})
        picam2.configure(config)
        picam2.start()
        
        # Set autofocus to Auto mode
        picam2.set_controls({"AfMode": 1})
        
        if verbose:
            print("\n🔍 Running autofocus...")
        
        # Run autofocus cycle and time it
        af_start = time.time()
        success = picam2.autofocus_cycle()
        af_time = time.time() - af_start
        
        result = {
            "success": success,
            "af_time": af_time,
            "lens_position": None,
            "distance_meters": None
        }
        
        if success:
            # Get lens position from metadata
            metadata = picam2.capture_metadata()
            lens_position = metadata.get("LensPosition")
            
            if lens_position:
                distance_meters = 1 / lens_position if lens_position > 0 else float('inf')
                
                result["lens_position"] = lens_position
                result["distance_meters"] = distance_meters
                
                if verbose:
                    print(f"✅ Autofocus succeeded in {af_time:.2f}s")
                    print(f"📍 Optimal LensPosition: {lens_position:.2f} dioptres")
                    print(f"📏 Approximate distance: {distance_meters:.2f} meters ({distance_meters*100:.0f} cm)")
                    print(f"⚡ Using manual focus will be ~100x faster than AF")
        else:
            if verbose:
                print(f"❌ Autofocus failed after {af_time:.2f}s")
        
        picam2.stop()
        picam2.close()
        
        # Store in calibration data
        self.calibration_data["focus"] = result
        self.calibration_data["calibrated_at"] = datetime.utcnow().isoformat()
        
        return result
    
    def calibrate_white_balance(self, img_size=(4624, 3472), verbose=True) -> Dict[str, Any]:
        """
        Calibrate white balance for current lighting conditions.
        
        TODO: Implementation for custom white balance calibration
        
        Args:
            img_size: Image resolution tuple
            verbose: Print progress messages
            
        Returns:
            Dictionary with white balance calibration results
        """
        if verbose:
            print(f"\n{'='*70}")
            print(f"WHITE BALANCE CALIBRATION - Camera {self.camera_index}")
            print(f"{'='*70}")
            print("⚠️  TODO: Not yet implemented")
        
        result = {
            "implemented": False,
            "note": "Future feature - will calibrate custom white balance"
        }
        
        self.calibration_data["white_balance"] = result
        
        return result
    
    def save_profile(self, filepath: str):
        """
        Save calibration profile to JSON file.
        
        Args:
            filepath: Path to save the calibration profile
        """
        filepath = Path(filepath)
        filepath.parent.mkdir(parents=True, exist_ok=True)
        
        with open(filepath, 'w') as f:
            json.dump(self.calibration_data, f, indent=2)
        
        print(f"\n💾 Calibration profile saved to: {filepath}")
    
    def load_profile(self, filepath: str) -> Dict[str, Any]:
        """
        Load calibration profile from JSON file.
        
        Args:
            filepath: Path to the calibration profile
            
        Returns:
            Loaded calibration data
        """
        with open(filepath, 'r') as f:
            self.calibration_data = json.load(f)
        
        print(f"📂 Loaded calibration profile from: {filepath}")
        return self.calibration_data
    
    def get_recommended_config(self) -> Dict[str, Any]:
        """
        Get recommended CameraConfig parameters based on calibration.
        
        Returns:
            Dictionary of recommended config values
        """
        recommendations = {}
        
        # Focus recommendations
        if self.calibration_data["focus"].get("success"):
            lens_pos = self.calibration_data["focus"]["lens_position"]
            recommendations["autofocus_on_capture"] = False
            recommendations["lens_position"] = lens_pos
            recommendations["_focus_note"] = f"Manual focus at {lens_pos:.2f} dioptres"
        
        # White balance recommendations (future)
        # if self.calibration_data["white_balance"].get("custom_gains"):
        #     recommendations["awb"] = "custom"
        #     recommendations["awb_gains"] = self.calibration_data["white_balance"]["custom_gains"]
        
        return recommendations


def calibrate_camera_interactive(camera_index: int = 0, save_path: Optional[str] = None):
    """
    Interactive calibration workflow for a camera.
    
    Args:
        camera_index: Index of camera to calibrate
        save_path: Optional path to save calibration profile
    """
    print(f"\n{'='*70}")
    print(f"CAMERA CALIBRATION - Camera {camera_index}")
    print(f"{'='*70}")
    
    cal = CameraCalibration(camera_index)
    
    # Focus calibration
    focus_result = cal.calibrate_focus()
    
    # Future: Add more calibration types
    # wb_result = cal.calibrate_white_balance()
    
    # Show recommendations
    print(f"\n{'='*70}")
    print("RECOMMENDED CONFIGURATION")
    print(f"{'='*70}")
    
    recommendations = cal.get_recommended_config()
    for key, value in recommendations.items():
        if not key.startswith('_'):
            print(f"  {key} = {value}")
    
    # Save if path provided
    if save_path:
        cal.save_profile(save_path)
    else:
        # Default save location
        default_path = f"calibration_camera{camera_index}.json"
        cal.save_profile(default_path)
    
    return cal


if __name__ == "__main__":
    import sys
    
    # Parse camera index from command line
    camera_index = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    save_path = sys.argv[2] if len(sys.argv) > 2 else None
    
    calibrate_camera_interactive(camera_index, save_path)
