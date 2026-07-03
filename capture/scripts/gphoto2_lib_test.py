#!/usr/bin/env python3
"""
python-gphoto2 library test for DSLR capture (Canon EOS 1500D).

Unlike the subprocess approach (gphoto2_test.py), this holds a persistent
PTP session across captures, which eliminates the Device Busy error caused
by re-connecting on every shot and yields ~1.3s per capture.

Key findings from testing:
  - Lens barrel MUST be set to MF (manual focus). AF causes ~12s hangs.
  - capturetarget=Internal RAM and reviewtime=None are set automatically.
  - Stable external continuous lighting eliminates exposure-hunt errors.
    Do NOT use the built-in popup flash — UV/visible flash causes
    photochemical degradation of archival paper and ink.
  - settle=3s between shots is the reliable minimum for the 1500D.

Usage:
  cd backend
  pixi run python capture/scripts/gphoto2_lib_test.py detect
  pixi run python capture/scripts/gphoto2_lib_test.py capture --port usb:003,002 --out /tmp/test.jpg
  pixi run python capture/scripts/gphoto2_lib_test.py dual   --left usb:003,002 --right usb:001,002 --outdir /tmp/caps
  pixi run python capture/scripts/gphoto2_lib_test.py bench  --left usb:003,002 --right usb:001,002 --count 6 --settle 3
"""

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    import gphoto2 as gp
except ImportError:
    sys.exit("python-gphoto2 not found. Run from pixi env: cd backend && pixi run python ...")


# ---------------------------------------------------------------------------
# Camera context manager — holds the PTP session open between captures
# ---------------------------------------------------------------------------

class DSLRCamera:
    """Persistent gphoto2 camera session. Use as a context manager.

    Opens the PTP session once on enter, applies speed settings, and keeps
    the connection alive for multiple captures — no reconnect overhead or
    Device Busy errors between shots.
    """

    def __init__(self, port: str, model: str | None = None):
        self.port = port
        self.model = model  # pass in to avoid calling autodetect() from threads
        self._cam: Optional[gp.Camera] = None

    def __enter__(self):
        # gphoto2 requires BOTH port info AND camera abilities (model driver).
        # Setting only the port gives -105 Unknown model on init().
        # autodetect() is NOT thread-safe — callers should pass model= explicitly
        # when opening cameras from worker threads.
        model = self.model
        if model is None:
            cameras = gp.Camera.autodetect()
            for i in range(len(cameras)):
                name, port = cameras[i]
                if port == self.port:
                    model = name
                    break
            if model is None:
                raise RuntimeError(f"No camera found on port {self.port}")

        abilities_list = gp.CameraAbilitiesList()
        abilities_list.load()
        self._cam = gp.Camera()
        abilities_idx = abilities_list.lookup_model(model)
        self._cam.set_abilities(abilities_list[abilities_idx])

        port_info_list = gp.PortInfoList()
        port_info_list.load()
        port_idx = port_info_list.lookup_path(self.port)
        self._cam.set_port_info(port_info_list[port_idx])

        self._cam.init()
        self._apply_speed_preset()
        return self

    def __exit__(self, *_):
        if self._cam:
            try:
                self._cam.exit()
            except Exception:
                pass
            self._cam = None

    def _set_config(self, key: str, value) -> bool:
        try:
            cfg = self._cam.get_config()
            widget = cfg.get_child_by_name(key)
            widget.set_value(value)
            self._cam.set_config(cfg)
            return True
        except gp.GPhoto2Error:
            return False

    def _get_config(self, key: str):
        try:
            cfg = self._cam.get_config()
            return cfg.get_child_by_name(key).get_value()
        except gp.GPhoto2Error:
            return None

    def _apply_speed_preset(self):
        """Set capturetarget=RAM and reviewtime=None once at session start."""
        self._set_config("capturetarget", "Internal RAM")
        self._set_config("reviewtime", "None")

    def get_summary(self) -> dict:
        keys = ["capturetarget", "reviewtime", "focusmode", "imageformat"]
        return {k: self._get_config(k) for k in keys}

    def capture(self, outpath: Path, retry: int = 3, retry_delay: float = 3.0) -> float:
        """Capture and download one image. Returns elapsed seconds.

        Retries within the same persistent session on transient I/O-busy
        errors — no reconnect needed, just a short wait for the camera buffer.
        Fatal errors (e.g. -1 Unspecified) are raised immediately.
        """
        outpath.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(1, retry + 1):
            try:
                t0 = time.perf_counter()
                file_path = self._cam.capture(gp.GP_CAPTURE_IMAGE)
                camera_file = self._cam.file_get(
                    file_path.folder, file_path.name, gp.GP_FILE_TYPE_NORMAL
                )
                camera_file.save(str(outpath))
                self._cam.file_delete(file_path.folder, file_path.name)
                return time.perf_counter() - t0
            except gp.GPhoto2Error as e:
                err = str(e)
                retriable = "-110" in err or "I/O in progress" in err or "busy" in err.lower()
                if attempt < retry and retriable:
                    print(f"  [{self.port}] I/O busy on attempt {attempt}/{retry}, retrying in {retry_delay}s ...")
                    time.sleep(retry_delay)
                    continue
                raise


# ---------------------------------------------------------------------------
# Sub-commands
# ---------------------------------------------------------------------------

def cmd_detect(_args):
    """List all cameras visible to libgphoto2."""
    print("Detecting cameras ...\n")
    cameras = gp.Camera.autodetect()
    if not cameras:
        print("  No cameras found.")
        return
    print(f"  {'Model':40s}  Port")
    print(f"  {'-'*40}  {'-'*20}")
    for i in range(len(cameras)):
        name, port = cameras[i]
        print(f"  {name:40s}  {port}")


def cmd_capture(args):
    """Single capture with a persistent session."""
    outpath = Path(args.out)
    print(f"Opening session on {args.port} ...")
    with DSLRCamera(args.port) as cam:
        summary = cam.get_summary()
        print(f"  focusmode={summary['focusmode']}  "
              f"capturetarget={summary['capturetarget']}  "
              f"reviewtime={summary['reviewtime']}")
        if summary.get("focusmode") not in ("Manual", "MF"):
            print("  WARNING: Lens not in MF -- AF causes delays. Flip lens barrel switch to MF.")
        print(f"Capturing -> {outpath} ...")
        elapsed = cam.capture(outpath)
    size = outpath.stat().st_size / 1_048_576
    print(f"  Done in {elapsed:.2f}s  ({size:.1f} MB)")


@dataclass
class BenchResult:
    port: str
    elapsed: float
    success: bool
    error: str = ""


def _bench_worker(port: str, model: str, outpath: Path, count: int, settle: float) -> list[BenchResult]:
    """Open one persistent session and run `count` captures."""
    results = []
    try:
        with DSLRCamera(port, model) as cam:
            for i in range(count):
                if i > 0:
                    time.sleep(settle)
                try:
                    elapsed = cam.capture(
                        outpath.parent / f"{outpath.stem}_run{i+1:02d}.jpg",
                        retry_delay=settle,
                    )
                    results.append(BenchResult(port=port, elapsed=elapsed, success=True))
                except gp.GPhoto2Error as e:
                    results.append(BenchResult(port=port, elapsed=0, success=False, error=str(e)))
    except gp.GPhoto2Error as e:
        results.append(BenchResult(port=port, elapsed=0, success=False, error=f"session init: {e}"))
    return results


def _single_capture(port: str, model: str, outpath: Path) -> BenchResult:
    try:
        with DSLRCamera(port, model) as cam:
            elapsed = cam.capture(outpath)
        return BenchResult(port=port, elapsed=elapsed, success=True)
    except gp.GPhoto2Error as e:
        return BenchResult(port=port, elapsed=0, success=False, error=str(e))


def cmd_dual(args):
    """Parallel dual capture from two cameras."""
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    left_path  = outdir / f"{stamp}_left.jpg"
    right_path = outdir / f"{stamp}_right.jpg"

    _detected = gp.Camera.autodetect()
    cam_map = {_detected[i][1]: _detected[i][0] for i in range(len(_detected))}
    print(f"Dual capture: {args.left} + {args.right}")
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=2) as ex:
        fl = ex.submit(_single_capture, args.left,  cam_map[args.left],  left_path)
        fr = ex.submit(_single_capture, args.right, cam_map[args.right], right_path)
        rl, rr = fl.result(), fr.result()
    wall = time.perf_counter() - t0

    for r, path in ((rl, left_path), (rr, right_path)):
        if r.success:
            size = path.stat().st_size / 1_048_576
            print(f"  [{r.port}]  OK  {r.elapsed:.2f}s  {path.name}  ({size:.1f} MB)")
        else:
            print(f"  [{r.port}]  FAIL  {r.error}", file=sys.stderr)
    print(f"\nTotal wall time: {wall:.2f}s")
    if not rl.success or not rr.success:
        sys.exit(1)


def cmd_bench(args):
    """Benchmark N captures per camera using persistent sessions.

    Each camera gets its own persistent session (PTP init paid once).
    Left and right cameras run in parallel threads.
    """
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    n      = args.count
    settle = args.settle

    _detected = gp.Camera.autodetect()
    cam_map = {_detected[i][1]: _detected[i][0] for i in range(len(_detected))}
    print(f"Benchmarking {n} captures per camera  (settle={settle}s between shots)")
    print(f"Persistent session: PTP init cost paid ONCE per camera\n")

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=2) as ex:
        fl = ex.submit(_bench_worker, args.left,  cam_map[args.left],  outdir / "left",  n, settle)
        fr = ex.submit(_bench_worker, args.right, cam_map[args.right], outdir / "right", n, settle)
        left_results, right_results = fl.result(), fr.result()
    total_wall = time.perf_counter() - t0

    def _summarise(label: str, results: list[BenchResult]):
        ok = [r for r in results if r.success]
        timings = [r.elapsed for r in ok]
        print(f"  {label}  ({len(ok)}/{len(results)} ok)")
        if timings:
            print(f"    per-shot:  min={min(timings):.2f}s  avg={sum(timings)/len(timings):.2f}s  max={max(timings):.2f}s")
            print(f"    runs: {[f'{t:.2f}s' for t in timings]}")
        for r in results:
            if not r.success:
                print(f"    FAIL: {r.error}")

    for label, results in (
        (f"left  [{args.left}]",  left_results),
        (f"right [{args.right}]", right_results),
    ):
        _summarise(label, results)

    print(f"\nTotal wall time for all {n} pairs: {total_wall:.2f}s")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="python-gphoto2 library test (persistent PTP session)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("detect", help="List connected cameras")

    p_cap = sub.add_parser("capture", help="Single capture from one camera")
    p_cap.add_argument("--port", required=True)
    p_cap.add_argument("--out", default="/tmp/gp_lib_test.jpg")

    p_dual = sub.add_parser("dual", help="Parallel dual capture")
    p_dual.add_argument("--left",   required=True)
    p_dual.add_argument("--right",  required=True)
    p_dual.add_argument("--outdir", default="/tmp/gp_lib_dual")

    p_bench = sub.add_parser("bench", help="Benchmark N captures per camera")
    p_bench.add_argument("--left",   required=True)
    p_bench.add_argument("--right",  required=True)
    p_bench.add_argument("--count",  type=int,   default=5)
    p_bench.add_argument("--settle", type=float, default=3.0,
                         help="Seconds between shots (default 3 -- minimum reliable for 1500D)")
    p_bench.add_argument("--outdir", default="/tmp/gp_lib_bench")

    args = parser.parse_args()
    {
        "detect":  cmd_detect,
        "capture": cmd_capture,
        "dual":    cmd_dual,
        "bench":   cmd_bench,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
