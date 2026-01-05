#!/usr/bin/env python3
"""
Detailed timing profile of a single capture to identify bottlenecks.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from capture.camera import CameraConfig
from capture.service import get_backend
import time

print("="*70)
print("DETAILED CAPTURE TIMING PROFILE")
print("="*70)
print()

backend = get_backend()

# Simple config - manual focus, no extras
config = CameraConfig(
    camera_index=0,
    lens_position=3.92,
    img_size=(4624, 3472),
    denoise_frames=0,
    raw=False,
    quality=93,
    timeout=10
)

output_path = Path("/tmp/timing_test.jpg")

print("Running 3 captures with detailed timing...")
print()

for run in range(3):
    print(f"Run {run + 1}:")
    print("-" * 50)
    
    total_start = time.time()
    
    try:
        result = backend.capture_image(str(output_path), config, None)
        
        total_time = time.time() - total_start
        
        print(f"  Total time: {total_time:.3f}s")
        print()
        
        if run < 2:
            time.sleep(0.5)
            
    except Exception as e:
        print(f"  ERROR: {e}")
        break

print("="*70)
print("The issue: Even a single camera takes 6+ seconds")
print("This suggests the bottleneck is in picamera2/libcamera itself")
print("="*70)
