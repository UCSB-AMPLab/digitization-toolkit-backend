#!/usr/bin/env python3
"""
Speed optimization test - identify bottlenecks and reach 800 pph target.

Target: 4.5s per dual capture (800 pages/hour)
Current: 7.0s per dual capture (513 pages/hour)
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from capture.camera import CameraConfig
from capture.service import dual_capture_image
import time

def benchmark_config(name, cam1, cam2, runs=3):
    """Quick benchmark of a configuration."""
    print(f"\n{name}")
    times = []
    for i in range(runs):
        start = time.time()
        dual_capture_image(f'speed_test_{name.replace(" ", "_").lower()}', cam1, cam2)
        elapsed = time.time() - start
        times.append(elapsed)
        if i < runs - 1:
            time.sleep(0.3)
    
    avg = sum(times) / len(times)
    pph = 3600 / avg
    print(f"  Avg: {avg:.2f}s | {pph:.0f} pph | Min: {min(times):.2f}s | Max: {max(times):.2f}s")
    return avg

print("="*70)
print("SPEED OPTIMIZATION TEST - Target: 800 pph (4.5s per capture)")
print("="*70)

base = {"lens_position": None}  # Let's try without manual focus first
cam1_idx = {"camera_index": 0, **base}
cam2_idx = {"camera_index": 1, **base}

# Baseline - current production config
print("\n" + "-"*70)
print("BASELINE (Current Production)")
print("-"*70)
baseline = benchmark_config(
    "Full res + denoise=10",
    CameraConfig(**cam1_idx, img_size=(4624, 3472), denoise_frames=10, raw=False, quality=93),
    CameraConfig(**cam2_idx, img_size=(4624, 3472), denoise_frames=10, raw=False, quality=93)
)

# Test 1: Remove denoise warmup
print("\n" + "-"*70)
print("OPTIMIZATION 1: Remove temporal denoise warmup")
print("-"*70)
opt1 = benchmark_config(
    "Full res + denoise=0",
    CameraConfig(**cam1_idx, img_size=(4624, 3472), denoise_frames=0, raw=False, quality=93),
    CameraConfig(**cam2_idx, img_size=(4624, 3472), denoise_frames=0, raw=False, quality=93)
)
print(f"  Improvement: {baseline - opt1:.2f}s faster ({(baseline - opt1) / baseline * 100:.0f}%)")

# Test 2: Lower JPEG quality
print("\n" + "-"*70)
print("OPTIMIZATION 2: Lower JPEG quality (93 → 85)")
print("-"*70)
opt2 = benchmark_config(
    "Full res + quality=85",
    CameraConfig(**cam1_idx, img_size=(4624, 3472), denoise_frames=0, raw=False, quality=85),
    CameraConfig(**cam2_idx, img_size=(4624, 3472), denoise_frames=0, raw=False, quality=85)
)
print(f"  Improvement vs baseline: {baseline - opt2:.2f}s faster")

# Test 3: Manual focus (skip autofocus)
print("\n" + "-"*70)
print("OPTIMIZATION 3: Manual focus (skip autofocus)")
print("-"*70)
opt3 = benchmark_config(
    "Full res + manual focus",
    CameraConfig(camera_index=0, lens_position=3.92, img_size=(4624, 3472), denoise_frames=0, raw=False, quality=93),
    CameraConfig(camera_index=1, lens_position=4.19, img_size=(4624, 3472), denoise_frames=0, raw=False, quality=93)
)
print(f"  Improvement vs baseline: {baseline - opt3:.2f}s faster")

# Test 4: Reduce timeout
print("\n" + "-"*70)
print("OPTIMIZATION 4: Reduce AE timeout (50ms → 10ms)")
print("-"*70)
opt4 = benchmark_config(
    "Full res + timeout=10",
    CameraConfig(camera_index=0, lens_position=3.92, img_size=(4624, 3472), denoise_frames=0, raw=False, quality=93, timeout=10),
    CameraConfig(camera_index=1, lens_position=4.19, img_size=(4624, 3472), denoise_frames=0, raw=False, quality=93, timeout=10)
)
print(f"  Improvement vs baseline: {baseline - opt4:.2f}s faster")

# Test 5: All optimizations combined
print("\n" + "-"*70)
print("OPTIMIZATION 5: ALL COMBINED (best shot at 800 pph)")
print("-"*70)
opt5 = benchmark_config(
    "OPTIMIZED",
    CameraConfig(camera_index=0, lens_position=3.92, img_size=(4624, 3472), 
                 denoise_frames=0, raw=False, quality=85, timeout=10),
    CameraConfig(camera_index=1, lens_position=4.19, img_size=(4624, 3472), 
                 denoise_frames=0, raw=False, quality=85, timeout=10)
)
target = 4.5
if opt5 <= target:
    print(f"  ✓ SUCCESS! Met 800 pph target ({opt5:.2f}s ≤ {target:.2f}s)")
else:
    print(f"  ⚠ Still {opt5 - target:.2f}s away from target")
print(f"  Total improvement: {baseline - opt5:.2f}s faster ({(baseline - opt5) / baseline * 100:.0f}%)")

print("\n" + "="*70)
print("SUMMARY")
print("="*70)
results = [
    ("Baseline (current)", baseline, 3600/baseline),
    ("No denoise warmup", opt1, 3600/opt1),
    ("Quality 85", opt2, 3600/opt2),
    ("Manual focus", opt3, 3600/opt3),
    ("Timeout 10ms", opt4, 3600/opt4),
    ("ALL OPTIMIZATIONS", opt5, 3600/opt5),
]

for name, time_val, pph in results:
    target_mark = " ← TARGET!" if pph >= 800 else ""
    print(f"{name:25s} {time_val:5.2f}s  {pph:4.0f} pph{target_mark}")

print()
if opt5 <= 4.5:
    print("✓ RECOMMENDED: Use optimized config for production")
    print("  - Manual focus at calibrated positions")
    print("  - No denoise warmup (quality still good)")
    print("  - JPEG quality 85 (smaller files, adequate quality)")
    print("  - Minimal AE timeout (10ms)")
else:
    print("⚠ Further investigation needed:")
    print("  - Check for additional overhead sources")
    print("  - Consider resolution trade-offs")
    print("  - Profile capture pipeline")
