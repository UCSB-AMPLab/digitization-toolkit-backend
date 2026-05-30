#!/usr/bin/env python3
"""
gphoto2 CLI tuning & diagnostics script for DSLR cameras (e.g. Canon EOS 1500D).

Usage examples:
  # Auto-detect cameras and show their settings
  python3 gphoto2_test.py detect

  # Capture from a single camera
  python3 gphoto2_test.py capture --port usb:003,005 --out /tmp/test.jpg

  # Capture from both cameras in parallel (dual-capture)
  python3 gphoto2_test.py dual --left usb:003,005 --right usb:001,005 --outdir /tmp/caps

  # Benchmark: run N dual-captures and report timing stats
  python3 gphoto2_test.py bench --left usb:003,005 --right usb:001,005 --count 5

  # Show all writable config options for a camera
  python3 gphoto2_test.py config --port usb:003,005

  # Apply a known-good speed preset to a camera
  python3 gphoto2_test.py preset --port usb:003,005 [--port usb:001,005 ...]
"""

import argparse
import subprocess
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _gphoto2(*args, port: Optional[str] = None, timeout: int = 30) -> subprocess.CompletedProcess:
    cmd = ["gphoto2"]
    if port:
        cmd += ["--camera", "Canon EOS 1500D", "--port", port]
    cmd += list(args)
    return _run(cmd, timeout=timeout)


def _require_gphoto2():
    r = _run(["which", "gphoto2"])
    if r.returncode != 0:
        sys.exit("gphoto2 not found. Install with: sudo apt install gphoto2")


# ---------------------------------------------------------------------------
# Sub-commands
# ---------------------------------------------------------------------------

def cmd_detect(args):
    """Auto-detect all connected cameras."""
    print("Detecting cameras...\n")
    r = _gphoto2("--auto-detect")
    print(r.stdout)
    if r.returncode != 0:
        print("stderr:", r.stderr, file=sys.stderr)


def cmd_config(args):
    """List all writable config keys and their current values for a camera."""
    port = args.port
    print(f"Reading config for port {port} ...\n")

    # Keys most relevant to capture speed
    speed_keys = [
        "capturetarget",
        "imageformat",
        "imagequality",
        "iso",
        "shutterspeed",
        "aperture",
        "whitebalance",
        "focusmode",
        "continuousaf",
        "reviewtime",        # image review delay after shot
    ]

    for key in speed_keys:
        r = _gphoto2("--get-config", key, port=port, timeout=10)
        if r.returncode == 0:
            # Parse out Label and Current
            info = {}
            for line in r.stdout.splitlines():
                if line.startswith("Label:"):
                    info["label"] = line.split(":", 1)[1].strip()
                elif line.startswith("Current:"):
                    info["current"] = line.split(":", 1)[1].strip()
                elif line.startswith("Choice:"):
                    info.setdefault("choices", []).append(line.split(" ", 2)[-1].strip())
            label   = info.get("label", key)
            current = info.get("current", "?")
            choices = info.get("choices", [])
            if key == "focusmode" and current not in ("Manual", "MF"):
                marker = " ◀ SET LENS TO MF — AF is the main source of latency & PTP Busy errors"
            elif key in ("capturetarget", "reviewtime", "continuousaf"):
                marker = " ◀ speed-relevant"
            else:
                marker = ""
            print(f"  {label:30s}  current={current!r:20s}  choices={choices}{marker}")
        else:
            print(f"  {key:30s}  (not available on this camera)")

    print()
    print("Full config dump (--list-all-config):")
    r = _gphoto2("--list-all-config", port=port, timeout=15)
    print(r.stdout)


def cmd_preset(args):
    """Apply speed-optimised settings to one or more cameras."""
    ports = args.port  # list

    # Settings that minimise capture+download latency:
    #   capturetarget=0  → Internal RAM (skip SD card write)
    #   reviewtime=0     → No on-camera image review delay (if supported)
    speed_settings = [
        ("capturetarget", "0"),   # Internal RAM
        ("reviewtime",    "0"),   # None / Off
    ]

    for port in ports:
        print(f"\nApplying speed preset to {port} ...")
        for key, val in speed_settings:
            r = _gphoto2("--set-config", f"{key}={val}", port=port, timeout=10)
            status = "OK" if r.returncode == 0 else f"SKIP ({r.stderr.strip().splitlines()[-1] if r.stderr else 'unknown'})"
            print(f"  set {key}={val!r}  → {status}")

    print("\nDone. Run 'config' subcommand to verify.")


@dataclass
class CaptureResult:
    port: str
    success: bool
    path: Optional[Path]
    elapsed: float
    error: str = ""


def _capture_one(port: str, outpath: Path, retry: int = 3, retry_delay: float = 3.0) -> CaptureResult:
    """Capture a single image, retrying on PTP Device Busy.

    Note: 'PTP Device Busy / Canon EOS Full-Press failed' is almost always
    caused by autofocus being active (AI Focus / AI Servo mode). The fix is
    to switch the lens barrel switch to MF (manual focus) — AF cannot be
    disabled via software on these bodies. With MF the first attempt succeeds
    immediately and no retries are needed.
    """
    outpath.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, retry + 1):
        t0 = time.perf_counter()
        r = _gphoto2(
            "--quiet",
            "--capture-image-and-download",
            "--filename", str(outpath),
            "--force-overwrite",
            port=port,
            timeout=45,
        )
        elapsed = time.perf_counter() - t0

        if r.returncode == 0:
            return CaptureResult(port=port, success=True, path=outpath, elapsed=elapsed)

        if "Device Busy" in r.stderr or "I/O in progress" in r.stderr:
            if attempt < retry:
                print(f"  [{port}] PTP Device Busy, retrying in {retry_delay}s (attempt {attempt}/{retry}) ...")
                time.sleep(retry_delay)
                continue

        return CaptureResult(port=port, success=False, path=None, elapsed=elapsed,
                             error=r.stderr.strip().splitlines()[-1] if r.stderr else "unknown error")

    return CaptureResult(port=port, success=False, path=None, elapsed=0, error="max retries exceeded")


def cmd_capture(args):
    """Capture a single image from one camera."""
    outpath = Path(args.out)
    print(f"Capturing from {args.port} → {outpath} ...")
    result = _capture_one(args.port, outpath)
    if result.success:
        size = outpath.stat().st_size / 1_048_576
        print(f"  Done in {result.elapsed:.2f}s  ({size:.1f} MB)")
    else:
        print(f"  FAILED in {result.elapsed:.2f}s: {result.error}", file=sys.stderr)
        sys.exit(1)


def cmd_dual(args):
    """Capture from two cameras in parallel."""
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    left_path  = outdir / f"{stamp}_left.jpg"
    right_path = outdir / f"{stamp}_right.jpg"

    print(f"Dual capture: {args.left} + {args.right}")
    t0 = time.perf_counter()

    with ThreadPoolExecutor(max_workers=2) as ex:
        fut_left  = ex.submit(_capture_one, args.left,  left_path)
        fut_right = ex.submit(_capture_one, args.right, right_path)
        results   = [f.result() for f in (fut_left, fut_right)]

    total = time.perf_counter() - t0

    for r in results:
        if r.success:
            size = r.path.stat().st_size / 1_048_576
            print(f"  [{r.port}]  OK  {r.elapsed:.2f}s  {r.path.name}  ({size:.1f} MB)")
        else:
            print(f"  [{r.port}]  FAIL  {r.elapsed:.2f}s  {r.error}", file=sys.stderr)

    print(f"\nTotal wall time: {total:.2f}s")
    failed = [r for r in results if not r.success]
    if failed:
        sys.exit(1)


def cmd_warmup(args):
    """Fire a throwaway dual capture to wake cameras from idle (eliminates PTP Busy on first real shot)."""
    import tempfile, os
    print(f"Warming up cameras {args.left} + {args.right} ...")
    with tempfile.TemporaryDirectory() as tmp:
        left_p  = Path(tmp) / "warmup_left.jpg"
        right_p = Path(tmp) / "warmup_right.jpg"
        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=2) as ex:
            fl = ex.submit(_capture_one, args.left,  left_p)
            fr = ex.submit(_capture_one, args.right, right_p)
            rl, rr = fl.result(), fr.result()
        wall = time.perf_counter() - t0
    ok_l = f"OK ({rl.elapsed:.2f}s)" if rl.success else f"FAIL: {rl.error}"
    ok_r = f"OK ({rr.elapsed:.2f}s)" if rr.success else f"FAIL: {rr.error}"
    print(f"  left  [{args.left}]  {ok_l}")
    print(f"  right [{args.right}]  {ok_r}")
    print(f"  Cameras ready. ({wall:.2f}s total)")
    if not (rl.success and rr.success):
        sys.exit(1)


def cmd_bench(args):
    """Run N dual-captures and report timing statistics."""
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    n = args.count
    settle = args.settle

    if args.warmup:
        print("Warming up cameras before benchmark ...")
        cmd_warmup(args)
        print(f"Settling {settle}s ...\n")
        time.sleep(settle)

    print(f"Benchmarking {n} dual-captures (settle={settle}s between runs)\n")
    timings: list[float] = []

    for i in range(1, n + 1):
        if i > 1:
            print(f"  Settling {settle}s ...")
            time.sleep(settle)

        stamp  = time.strftime("%Y%m%d_%H%M%S")
        left_p  = outdir / f"{stamp}_run{i:02d}_left.jpg"
        right_p = outdir / f"{stamp}_run{i:02d}_right.jpg"

        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=2) as ex:
            fl = ex.submit(_capture_one, args.left,  left_p,  retry=2, retry_delay=settle)
            fr = ex.submit(_capture_one, args.right, right_p, retry=2, retry_delay=settle)
            rl, rr = fl.result(), fr.result()
        wall = time.perf_counter() - t0

        ok_l = f"{rl.elapsed:.2f}s" if rl.success else f"FAIL({rl.error[:30]})"
        ok_r = f"{rr.elapsed:.2f}s" if rr.success else f"FAIL({rr.error[:30]})"
        print(f"  Run {i:2d}/{n}  left={ok_l}  right={ok_r}  wall={wall:.2f}s")

        if rl.success and rr.success:
            timings.append(wall)

    if timings:
        avg = sum(timings) / len(timings)
        mn  = min(timings)
        mx  = max(timings)
        print(f"\nResults over {len(timings)}/{n} successful runs:")
        print(f"  min={mn:.2f}s  avg={avg:.2f}s  max={mx:.2f}s")
    else:
        print("\nNo successful captures.", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    _require_gphoto2()

    parser = argparse.ArgumentParser(
        description="gphoto2 tuning & diagnostics for DSLR cameras",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # detect
    sub.add_parser("detect", help="Auto-detect connected cameras")

    # config
    p_cfg = sub.add_parser("config", help="Show speed-relevant config for a camera")
    p_cfg.add_argument("--port", required=True, help="USB port, e.g. usb:003,005")

    # preset
    p_pre = sub.add_parser("preset", help="Apply speed-optimised settings")
    p_pre.add_argument("--port", required=True, nargs="+", help="One or more USB ports")

    # capture
    p_cap = sub.add_parser("capture", help="Single-camera capture with timing")
    p_cap.add_argument("--port",  required=True, help="USB port")
    p_cap.add_argument("--out",   default="/tmp/gphoto2_test.jpg", help="Output file path")

    # dual
    p_dual = sub.add_parser("dual", help="Parallel dual-camera capture")
    p_dual.add_argument("--left",   required=True, help="Left camera USB port")
    p_dual.add_argument("--right",  required=True, help="Right camera USB port")
    p_dual.add_argument("--outdir", default="/tmp/gphoto2_dual", help="Output directory")

    # bench
    p_bench = sub.add_parser("bench", help="Benchmark N dual-captures")
    p_bench.add_argument("--left",   required=True, help="Left camera USB port")
    p_bench.add_argument("--right",  required=True, help="Right camera USB port")
    p_bench.add_argument("--count",  type=int, default=5, help="Number of captures (default 5)")
    p_bench.add_argument("--settle", type=float, default=5.0, help="Seconds between runs (default 5)")
    p_bench.add_argument("--outdir",  default="/tmp/gphoto2_bench", help="Output directory")
    p_bench.add_argument("--warmup", action="store_true", help="Fire a throwaway dual capture first to wake cameras")

    # warmup
    p_warm = sub.add_parser("warmup", help="Wake cameras from idle before a capture session")
    p_warm.add_argument("--left",  required=True, help="Left camera USB port")
    p_warm.add_argument("--right", required=True, help="Right camera USB port")

    args = parser.parse_args()
    dispatch = {
        "detect":  cmd_detect,
        "config":  cmd_config,
        "preset":  cmd_preset,
        "capture": cmd_capture,
        "dual":    cmd_dual,
        "warmup":  cmd_warmup,
        "bench":   cmd_bench,
    }
    dispatch[args.cmd](args)


if __name__ == "__main__":
    main()
