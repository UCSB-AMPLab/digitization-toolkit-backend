#!/usr/bin/env python3
"""
Command-line tool for camera calibration.

Usage:
    python calibrate.py                    # Calibrate camera 0
    python calibrate.py 1                  # Calibrate camera 1
    python calibrate.py 0 my_profile.json  # Save to custom path
    python calibrate.py dual               # Calibrate both cameras
"""
import sys
from pathlib import Path
from calibration import calibrate_camera_interactive, CameraCalibration


def calibrate_dual_cameras(save_dir: str = "."):
    """Calibrate both cameras and save profiles."""
    print("\n" + "="*70)
    print("DUAL CAMERA CALIBRATION")
    print("="*70)
    print("This will calibrate both cameras sequentially.\n")
    
    save_dir = Path(save_dir)
    save_dir.mkdir(parents=True, exist_ok=True)
    
    # Calibrate camera 0
    print("\n>>> CAMERA 0 <<<")
    cal0 = calibrate_camera_interactive(0, str(save_dir / "calibration_camera0.json"))
    
    # Calibrate camera 1
    print("\n>>> CAMERA 1 <<<")
    cal1 = calibrate_camera_interactive(1, str(save_dir / "calibration_camera1.json"))
    
    print("\n" + "="*70)
    print("DUAL CALIBRATION COMPLETE")
    print("="*70)
    print(f"Camera 0 profile: {save_dir / 'calibration_camera0.json'}")
    print(f"Camera 1 profile: {save_dir / 'calibration_camera1.json'}")
    
    return cal0, cal1


def show_usage():
    """Show usage information."""
    print(__doc__)
    print("\nExamples:")
    print("  python calibrate.py              # Calibrate camera 0")
    print("  python calibrate.py 1            # Calibrate camera 1")
    print("  python calibrate.py dual         # Calibrate both cameras")
    print("  python calibrate.py 0 custom.json  # Custom save path")


if __name__ == "__main__":
    if len(sys.argv) == 1:
        # Default: calibrate camera 0
        calibrate_camera_interactive(0)
    
    elif sys.argv[1] in ["-h", "--help", "help"]:
        show_usage()
    
    elif sys.argv[1] == "dual":
        # Calibrate both cameras
        save_dir = sys.argv[2] if len(sys.argv) > 2 else "."
        calibrate_dual_cameras(save_dir)
    
    else:
        # Calibrate specific camera
        try:
            camera_index = int(sys.argv[1])
            save_path = sys.argv[2] if len(sys.argv) > 2 else None
            calibrate_camera_interactive(camera_index, save_path)
        except ValueError:
            print(f"❌ Error: Invalid camera index '{sys.argv[1]}'")
            print("\nUsage: python calibrate.py [camera_index|dual] [save_path]")
            sys.exit(1)
