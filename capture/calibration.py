"""
Camera calibration module for finding optimal settings.

This module provides calibration functions to determine optimal camera settings
for specific setups. Calibration profiles can be saved and loaded for reuse.
"""
import json
import time
from pathlib import Path
from datetime import datetime, timezone
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
            print("⚠️  Ensure object is at normal working distance!")
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
        self.calibration_data["calibrated_at"] = datetime.now(timezone.utc).isoformat()
        
        return result
    
    def calibrate_white_balance(
        self, 
        img_size=(4624, 3472), 
        stabilization_frames: int = 30,
        verbose=True
    ) -> Dict[str, Any]:
        """
        Calibrate white balance for current lighting conditions.
        
        This captures frames with AWB enabled, waits for stabilization,
        then reads the converged AWB gains to use as fixed values.
        
        For best results:
        - Use a neutral gray card or white paper in the frame
        - Ensure consistent lighting
        - Allow camera to warm up
        
        Args:
            img_size: Image resolution tuple (width, height)
            stabilization_frames: Number of frames to wait for AWB convergence
            verbose: Print progress messages
            
        Returns:
            Dictionary with white balance calibration results:
            {
                "success": bool,
                "awb_gains": (red_gain, blue_gain),
                "colour_temperature": int (estimated Kelvin),
                "awb_mode_used": str,
                "frames_for_convergence": int
            }
        """
        if verbose:
            print(f"\n{'='*70}")
            print(f"WHITE BALANCE CALIBRATION - Camera {self.camera_index}")
            print(f"{'='*70}")
            print("⚠️  Place a neutral gray card or white paper in frame!")
            print(f"Resolution: {img_size[0]}x{img_size[1]}")
            print(f"Stabilization frames: {stabilization_frames}")
        
        result = {
            "success": False,
            "awb_gains": None,
            "colour_temperature": None,
            "awb_mode_used": None,
            "frames_for_convergence": stabilization_frames
        }
        
        try:
            picam2 = Picamera2(self.camera_index)
            
            # Use preview config for faster frame capture during calibration
            config = picam2.create_preview_configuration(
                main={"size": img_size}
            )
            picam2.configure(config)
            picam2.start()
            
            if verbose:
                print("\n🔄 Running AWB convergence...")
            
            # Enable auto white balance and let it converge
            picam2.set_controls({"AwbEnable": True, "AwbMode": 0})  # 0 = Auto
            
            # Capture frames to allow AWB to stabilize
            awb_gains_history = []
            for i in range(stabilization_frames):
                metadata = picam2.capture_metadata()
                gains = metadata.get("ColourGains")
                if gains:
                    awb_gains_history.append(gains)
                
                if verbose and (i + 1) % 10 == 0:
                    if gains:
                        print(f"  Frame {i+1}/{stabilization_frames}: R={gains[0]:.3f}, B={gains[1]:.3f}")
            
            # Get final converged values
            final_metadata = picam2.capture_metadata()
            final_gains = final_metadata.get("ColourGains")
            colour_temp = final_metadata.get("ColourTemperature")
            
            picam2.stop()
            picam2.close()
            
            if final_gains:
                result["success"] = True
                result["awb_gains"] = (float(final_gains[0]), float(final_gains[1]))
                result["colour_temperature"] = int(colour_temp) if colour_temp else None
                result["awb_mode_used"] = "auto"
                
                # Check convergence stability (variance of last 10 frames)
                if len(awb_gains_history) >= 10:
                    recent_r = [g[0] for g in awb_gains_history[-10:]]
                    recent_b = [g[1] for g in awb_gains_history[-10:]]
                    variance_r = max(recent_r) - min(recent_r)
                    variance_b = max(recent_b) - min(recent_b)
                    result["convergence_variance"] = {
                        "red": float(variance_r),
                        "blue": float(variance_b)
                    }
                    result["converged"] = variance_r < 0.05 and variance_b < 0.05
                
                if verbose:
                    print(f"\n✅ White balance calibration complete")
                    print(f"🎨 AWB Gains: Red={final_gains[0]:.3f}, Blue={final_gains[1]:.3f}")
                    if colour_temp:
                        print(f"🌡️  Colour temperature: ~{colour_temp}K")
                    if result.get("converged"):
                        print(f"✅ AWB converged (stable)")
                    else:
                        print(f"⚠️  AWB may not be fully converged, consider more frames")
            else:
                if verbose:
                    print(f"\n❌ Failed to get AWB gains from camera")
                    
        except Exception as e:
            result["error"] = str(e)
            if verbose:
                print(f"\n❌ White balance calibration failed: {e}")
        
        # Store in calibration data
        self.calibration_data["white_balance"] = result
        self.calibration_data["calibrated_at"] = datetime.now(timezone.utc).isoformat()
        
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
        
        # White balance recommendations
        if self.calibration_data["white_balance"].get("success"):
            awb_gains = self.calibration_data["white_balance"]["awb_gains"]
            recommendations["awb"] = "custom"
            recommendations["awb_gains"] = awb_gains
            colour_temp = self.calibration_data["white_balance"].get("colour_temperature")
            if colour_temp:
                recommendations["_wb_note"] = f"Custom WB gains (R={awb_gains[0]:.3f}, B={awb_gains[1]:.3f}) @ ~{colour_temp}K"
            else:
                recommendations["_wb_note"] = f"Custom WB gains (R={awb_gains[0]:.3f}, B={awb_gains[1]:.3f})"
        
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
