"""
python-gphoto2 backend for DSLR cameras (Canon EOS, etc.).

Uses persistent PTP sessions for ~1.3s capture times.

Key design decisions:
  - Sessions opened once and kept alive between captures — no reconnect overhead.
  - capturetarget=Internal RAM, reviewtime=None, autopoweroff=0 applied at init.
  - Port map built lazily from gp.Camera.autodetect(); rebuilt automatically on
    session failure (handles USB re-enumeration after camera power-cycle).
  - Flash guard: disables camera flash (flashmode=Off) at session open and before
    every capture. Flash (UV/visible) causes photochemical degradation of archival
    paper and ink — use external continuous lighting instead.
  - Focus mode detection: warns if lens is not in MF (AF causes ~12s PTP hangs).

Tested with: Canon EOS 1500D × 2, USB 2.0, Raspberry Pi.

Future hooks (wire up when DSLRCameraConfig is introduced):
  - ISO control via `iso` PTP widget
  - Shutter speed control via `shutterspeed` PTP widget
  - Aperture control via `aperture` PTP widget
"""

import threading
import time
from pathlib import Path

try:
    import rawpy
    _RAWPY_AVAILABLE = True
except ImportError:
    rawpy = None  # type: ignore[assignment]
    _RAWPY_AVAILABLE = False
try:
    import gphoto2 as gp
    _GP_AVAILABLE = True
except ImportError:
    gp = None  # type: ignore[assignment]
    _GP_AVAILABLE = False

from .base import CameraBackend

# Image format mapping: CameraConfig.image_format → PTP imageformat widget value
_IMAGE_FORMAT_MAP = {
    "JPEG":     "L",
    "RAW":      "RAW",
    "RAW+JPEG": "RAW + L",
}
# Default when image_format is None
_IMAGE_FORMAT_DEFAULT = "L"
# Reverse mapping: PTP widget value → user-facing label
_IMAGE_FORMAT_REVERSE_MAP = {v: k for k, v in _IMAGE_FORMAT_MAP.items()}

# Minimum settle time used as retry_delay in capture (seconds).
# Canon EOS 1500D needs ~3s between shots for reliable PTP operation.
_DEFAULT_SETTLE = 3.0
_DEFAULT_RETRY = 3


class _PTPSession:
    """
    An open PTP/USB session to one DSLR camera.

    Created once per camera; held alive between captures. Call close() when done.
    Not thread-safe — callers must hold the per-camera lock.
    """

    def __init__(self, port: str, model: str, logger):
        self.port = port
        self.model = model
        self._logger = logger
        self._cam = None
        self._open()

    def _open(self):
        abilities_list = gp.CameraAbilitiesList()
        abilities_list.load()
        self._cam = gp.Camera()
        abilities_idx = abilities_list.lookup_model(self.model)
        self._cam.set_abilities(abilities_list[abilities_idx])

        port_info_list = gp.PortInfoList()
        port_info_list.load()
        port_idx = port_info_list.lookup_path(self.port)
        self._cam.set_port_info(port_info_list[port_idx])

        self._cam.init()
        self._logger.info(f"[gphoto2] Opened session: {self.model} on {self.port}")
        # Run post-init config; on any error release the USB claim so the
        # next attempt isn't blocked by a half-open session.
        try:
            self._apply_speed_preset()
            self._disable_autopoweroff()
            self._warn_if_af()
            self._enforce_flash_off()
        except Exception as exc:
            self._logger.warning(
                f"[gphoto2] {self.port}: error during session init ({exc}), releasing device"
            )
            try:
                self._cam.exit()
            except Exception:
                pass
            raise

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------

    def _set_config(self, key: str, value) -> bool:
        try:
            cfg = self._cam.get_config()
            widget = cfg.get_child_by_name(key)
            widget.set_value(value)
            self._cam.set_config(cfg)
            return True
        except (gp.GPhoto2Error, TypeError, ValueError):
            return False

    def _get_config(self, key: str):
        try:
            cfg = self._cam.get_config()
            return cfg.get_child_by_name(key).get_value()
        except gp.GPhoto2Error:
            return None

    # ------------------------------------------------------------------
    # Session init helpers
    # ------------------------------------------------------------------

    def _apply_speed_preset(self):
        """Set capturetarget=RAM and reviewtime=None once at session start."""
        self._set_config("capturetarget", "Internal RAM")
        self._set_config("reviewtime", "None")

    def apply_dslr_config(self, camera_config) -> None:
        """Apply DSLR-specific CameraConfig fields to the camera via PTP.

        Called before each capture so settings reflect the current config.
        Fields that are None are left at their current camera value.
        Flash is always enforced off here as an archival safety guard.
        """
        # ── Flash guard: must be first, before shutter opens ──────────
        self._enforce_flash_off()

        fmt = getattr(camera_config, "image_format", None)
        if fmt is not None:
            ptp_fmt = _IMAGE_FORMAT_MAP.get(fmt, _IMAGE_FORMAT_DEFAULT)
            self._set_config("imageformat", ptp_fmt)
        # If fmt is None, leave the camera's current imageformat unchanged

        iso = getattr(camera_config, "iso", None)
        if iso is not None:
            self._set_config("iso", str(iso))

        shutter = getattr(camera_config, "shutter_speed", None)
        if shutter is not None:
            self._set_config("shutterspeed", shutter)

        aperture = getattr(camera_config, "aperture", None)
        if aperture is not None:
            self._set_config("aperture", aperture)

    def _disable_autopoweroff(self):
        """Disable camera auto-power-off to prevent sleep mid-session."""
        # Canon EOS PTP widget expects a string, not an integer.
        # Try "0" first (some bodies), fall back to "Off" (others).
        if not self._set_config("autopoweroff", "0"):
            if not self._set_config("autopoweroff", "Off"):
                self._logger.debug(
                    f"[gphoto2] {self.port}: autopoweroff widget not available on {self.model}"
                )

    def _warn_if_af(self):
        """Log a warning if the lens is not in manual focus mode."""
        focusmode = self._get_config("focusmode")
        if focusmode and focusmode not in ("Manual", "MF"):
            self._logger.warning(
                f"[gphoto2] {self.port}: focusmode={focusmode!r} — "
                "AF causes ~12s PTP hangs. Flip lens barrel switch to MF."
            )

    def _enforce_flash_off(self) -> bool:
        """Actively disable camera flash before every capture.

        Tries to set the flashmode PTP widget to 'Off'.  If the widget is
        read-only or unavailable (some bodies don't expose it), falls back to
        reading the current value and logging a warning if flash is still
        active.  Never blocks the capture — returns False when flash cannot
        be confirmed off so callers can decide.

        Flash (UV/visible) causes photochemical degradation of archival paper
        and ink.  Use stable external continuous lighting instead.
        """
        if self._set_config("flashmode", "Off"):
            self._logger.debug(
                f"[gphoto2] {self.port}: flash disabled (flashmode=Off)"
            )
            return True
        # Widget is read-only or not present — check current value
        flashmode = self._get_config("flashmode")
        if flashmode is None:
            # Camera doesn't support the widget; assume no flash
            return True
        if "off" not in str(flashmode).lower():
            self._logger.warning(
                f"[gphoto2] {self.port}: flashmode={flashmode!r} and cannot be "
                "set to Off automatically. "
                "Flash is harmful to archival materials — "
                "manually disable the flash before capturing."
            )
            return False
        return True

    # ------------------------------------------------------------------
    # Capture
    # ------------------------------------------------------------------

    def capture(
        self,
        outpath: Path,
        retry: int = _DEFAULT_RETRY,
        retry_delay: float = _DEFAULT_SETTLE,
    ) -> tuple[float, Path]:
        """Capture one image to outpath. Returns (elapsed_seconds, actual_path).

        actual_path may differ from outpath when the camera is in RAW mode
        (extension becomes .cr2 instead of .jpg). Callers should use
        actual_path to reference the saved file.

        When imageformat is RAW, the camera produces a .cr2 file. The embedded
        full-resolution JPEG (6000×4000, 11 ms to extract) is saved alongside
        the CR2 as ``{stem}_preview.jpg`` so the existing thumbnail/review
        pipeline has a JPEG to work with.

        Retries on transient I/O-busy errors within the same open session.
        Fatal errors (e.g. -1 Unspecified) are raised immediately.
        """
        outpath.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(1, retry + 1):
            try:
                t0 = time.perf_counter()
                file_path = self._cam.capture(gp.GP_CAPTURE_IMAGE)

                # The captured filename tells us the actual format (.cr2 vs .jpg)
                is_raw = file_path.name.lower().endswith(".cr2")
                if is_raw:
                    # Save CR2 with .cr2 extension regardless of outpath stem
                    actual_outpath = outpath.with_suffix(".cr2")
                else:
                    actual_outpath = outpath

                camera_file = self._cam.file_get(
                    file_path.folder, file_path.name, gp.GP_FILE_TYPE_NORMAL
                )
                camera_file.save(str(actual_outpath))
                self._cam.file_delete(file_path.folder, file_path.name)
                elapsed = time.perf_counter() - t0

                # Extract embedded JPEG from CR2 for thumbnail/review pipeline
                if is_raw and _RAWPY_AVAILABLE:
                    try:
                        preview_path = actual_outpath.with_name(
                            actual_outpath.stem + "_preview.jpg"
                        )
                        with rawpy.imread(str(actual_outpath)) as raw:
                            thumb = raw.extract_thumb()
                        if hasattr(thumb, "data"):
                            preview_path.write_bytes(thumb.data)
                        self._logger.info(
                            f"[gphoto2] {self.port}: embedded JPEG saved to "
                            f"{preview_path.name}"
                        )
                    except Exception as exc:
                        self._logger.warning(
                            f"[gphoto2] {self.port}: failed to extract embedded JPEG: {exc}"
                        )

                return elapsed, actual_outpath
            except gp.GPhoto2Error as exc:
                err = str(exc)
                retriable = (
                    "-110" in err
                    or "I/O in progress" in err
                    or "busy" in err.lower()
                )
                if attempt < retry and retriable:
                    self._logger.warning(
                        f"[gphoto2] {self.port}: I/O busy (attempt {attempt}/{retry}), "
                        f"retrying in {retry_delay}s ..."
                    )
                    time.sleep(retry_delay)
                    continue
                raise

    def get_info(self) -> dict:
        """Return current camera settings as a dict (for logging / future API)."""
        keys = [
            "focusmode", "capturetarget", "reviewtime",
            "iso", "shutterspeed", "aperture", "imageformat", "flashmode",
        ]
        return {k: self._get_config(k) for k in keys}

    def close(self):
        if self._cam is not None:
            try:
                self._cam.exit()
            except Exception:
                pass
            self._cam = None
            self._logger.info(f"[gphoto2] Closed session: {self.model} on {self.port}")


class GPhoto2Backend(CameraBackend):
    """
    DSLR camera backend using python-gphoto2 with persistent PTP sessions.

    Activate by setting CAMERA_BACKEND=gphoto2 in the environment / .env.

    Thread safety:
      - One threading.Lock per camera index serialises concurrent capture calls.
      - The port map is protected by a separate map_lock.
    """

    def __init__(self, logger):
        if not _GP_AVAILABLE:
            raise RuntimeError(
                "python-gphoto2 is not installed. "
                "Add it to pixi.toml with: pixi add python-gphoto2"
            )
        super().__init__(logger)
        # camera_index → (model_name, usb_port)
        self._port_map: dict[int, tuple[str, str]] = {}
        # camera_index → open _PTPSession
        self._sessions: dict[int, _PTPSession] = {}
        # per-camera lock for capture serialisation
        self._session_locks: dict[int, threading.Lock] = {}
        self._map_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Port map management
    # ------------------------------------------------------------------

    def _build_port_map(self) -> dict[int, tuple[str, str]]:
        detected = gp.Camera.autodetect()
        port_map: dict[int, tuple[str, str]] = {}
        for i in range(len(detected)):
            model, port = detected[i][0], detected[i][1]
            port_map[i] = (model, port)
            self.logger.info(f"[gphoto2] Detected camera {i}: {model} on {port}")
        if not port_map:
            self.logger.warning("[gphoto2] No cameras detected by autodetect().")
        return port_map

    def _refresh_port_map(self):
        with self._map_lock:
            self._port_map = self._build_port_map()

    def _get_port_map(self) -> dict[int, tuple[str, str]]:
        with self._map_lock:
            if not self._port_map:
                self._port_map = self._build_port_map()
            return dict(self._port_map)

    def _get_camera_lock(self, camera_index: int) -> threading.Lock:
        with self._map_lock:
            if camera_index not in self._session_locks:
                self._session_locks[camera_index] = threading.Lock()
            return self._session_locks[camera_index]

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def _get_or_open_session(self, camera_index: int) -> _PTPSession:
        """Return an existing open session, or open a new one."""
        existing = self._sessions.get(camera_index)
        if existing is not None and existing._cam is not None:
            return existing

        port_map = self._get_port_map()
        if camera_index not in port_map:
            raise RuntimeError(
                f"Camera index {camera_index} not found. "
                f"Detected indices: {sorted(port_map.keys())}. "
                "Ensure the camera is on and USB is connected, then try again."
            )

        model, port = port_map[camera_index]
        try:
            session = _PTPSession(port, model, self.logger)
            self._sessions[camera_index] = session
            return session
        except gp.GPhoto2Error as exc:
            # -105 Unknown model usually means ports changed (USB re-enumeration
            # after a power-cycle). Refresh the port map once and retry.
            if "-105" in str(exc) or "Unknown model" in str(exc):
                self.logger.warning(
                    f"[gphoto2] Camera {camera_index}: session init failed ({exc}). "
                    "Refreshing port map and retrying ..."
                )
                self._refresh_port_map()
                port_map = self._get_port_map()
                if camera_index not in port_map:
                    raise RuntimeError(
                        f"Camera index {camera_index} not found after port map refresh."
                    ) from exc
                model, port = port_map[camera_index]
                session = _PTPSession(port, model, self.logger)
                self._sessions[camera_index] = session
                return session
            raise

    def _close_session(self, camera_index: int):
        session = self._sessions.pop(camera_index, None)
        if session is not None:
            session.close()

    # ------------------------------------------------------------------
    # CameraBackend interface
    # ------------------------------------------------------------------

    def is_camera_connected(self, camera_index: int = 0) -> bool:
        try:
            # Always do a fresh detection so this method reflects reality even
            # if cameras have been power-cycled since last call.
            self._refresh_port_map()
            return camera_index in self._get_port_map()
        except Exception as exc:
            self.logger.error(f"[gphoto2] is_camera_connected({camera_index}): {exc}")
            return False

    def capture_image(
        self,
        output_path: Path,
        camera_config,
        capture_output: bool = False,
    ) -> str:
        """Capture one image to output_path. Returns path as str.

        Most CameraConfig fields (awb, vflip, lens_position, etc.) are
        picamera2-specific and are silently ignored here. DSLR-specific
        controls (ISO, shutter speed, aperture) will be wired up once a
        dedicated DSLRCameraConfig is introduced.

        Args:
            output_path: Destination path for the captured JPEG.
            camera_config: CameraConfig (camera_index used for routing).
            capture_output: Unused — kept for interface compatibility.

        Returns:
            Absolute path string to the saved image file.

        Raises:
            RuntimeError: If the capture fails.
        """
        camera_index = getattr(camera_config, "camera_index", 0)
        lock = self._get_camera_lock(camera_index)

        with lock:
            session = self._get_or_open_session(camera_index)
            try:
                session.apply_dslr_config(camera_config)
                elapsed, actual_path = session.capture(output_path)
                self.logger.info(
                    f"[gphoto2] Camera {camera_index}: captured {actual_path.name} "
                    f"in {elapsed:.2f}s"
                )
                return str(actual_path), None
            except gp.GPhoto2Error as exc:
                self.logger.error(
                    f"[gphoto2] Camera {camera_index}: capture failed: {exc}"
                )
                # Close the failed session so the next call re-opens it cleanly.
                self._close_session(camera_index)
                raise RuntimeError(f"DSLR capture failed: {exc}") from exc

    def list_devices(self) -> list:
        """Enumerate all connected DSLR cameras and return device metadata.

        Uses the persistent port map (refreshed if empty). For each camera,
        re-uses an already-open PTP session to read the serial number; if no
        session is open yet, opens a brief one just for the read and closes it.
        """
        import re
        port_map = self._get_port_map()
        if not port_map:
            return []

        result = []
        for idx, (model_raw, port) in sorted(port_map.items()):
            serial = ""
            # Re-use existing open session if available
            session = self._sessions.get(idx)
            if session is not None and session._cam is not None:
                try:
                    cfg = session._cam.get_config()
                    serial = cfg.get_child_by_name("serialnumber").get_value().strip()
                except Exception:
                    pass
            else:
                # Brief PTP connection solely to read the serial number
                try:
                    al = gp.CameraAbilitiesList()
                    al.load()
                    cam = gp.Camera()
                    cam.set_abilities(al[al.lookup_model(model_raw)])
                    pil = gp.PortInfoList()
                    pil.load()
                    cam.set_port_info(pil[pil.lookup_path(port)])
                    cam.init()
                    cfg = cam.get_config()
                    serial = cfg.get_child_by_name("serialnumber").get_value().strip()
                    cam.exit()
                except Exception:
                    pass

            model_slug = re.sub(r"[^a-z0-9]", "", model_raw.lower())
            hw_id = (
                f"{model_slug}_{serial}" if serial
                else f"{model_slug}_idx{idx}"
            )
            result.append({
                "index": idx,
                "model": model_raw,
                "hardware_id": hw_id,
                "serial": serial or None,
                "location": f"USB {port}",
                "has_aperture_control": True,   # DSLRs always expose aperture via PTP
                "supports_zoom": False,          # No digital zoom for DSLRs
            })

        return result

    def capture_preview(self, camera_index: int) -> bytes:
        """Return a live-preview JPEG frame from the camera.

        Uses the same persistent PTP session as full captures so there is no
        reconnect overhead. The first frame takes ~1.3s (camera warms up video
        subsystem); subsequent frames are ~30ms at ~33 fps.

        The per-camera lock serialises preview and full-capture calls so they
        never interleave on the same session.
        """
        lock = self._get_camera_lock(camera_index)
        with lock:
            session = self._get_or_open_session(camera_index)
            try:
                camera_file = session._cam.capture_preview()
                data = camera_file.get_data_and_size()
                return bytes(data)
            except gp.GPhoto2Error as exc:
                self.logger.error(
                    f"[gphoto2] Camera {camera_index}: preview capture failed: {exc}"
                )
                self._close_session(camera_index)
                raise RuntimeError(
                    f"DSLR preview capture failed: {exc}"
                ) from exc

    def get_dslr_settings(self, camera_index: int) -> dict:
        """Read current DSLR settings from the open PTP session.

        Returns a dict with keys:
            iso (str | None), shutter_speed (str | None), aperture (str | None),
            image_format (str | None), focus_mode (str | None), flash_mode (str | None)
        """
        lock = self._get_camera_lock(camera_index)
        with lock:
            session = self._get_or_open_session(camera_index)
            try:
                raw = session.get_info()
                return {
                    "iso": raw.get("iso"),
                    "shutter_speed": raw.get("shutterspeed"),
                    "aperture": raw.get("aperture"),
                    "image_format": _IMAGE_FORMAT_REVERSE_MAP.get(
                        raw.get("imageformat", ""), raw.get("imageformat")
                    ),
                    "focus_mode": raw.get("focusmode"),
                    "flash_mode": raw.get("flashmode"),
                }
            except gp.GPhoto2Error as exc:
                self.logger.error(
                    f"[gphoto2] Camera {camera_index}: get_dslr_settings failed: {exc}"
                )
                raise RuntimeError(f"Failed to read DSLR settings: {exc}") from exc

    def apply_dslr_settings(self, camera_index: int, settings: dict) -> dict:
        """Apply a partial dict of DSLR settings via PTP and return updated values.

        Accepted keys (all optional):
            iso (str): PTP iso value e.g. "400"
            shutter_speed (str): PTP shutterspeed e.g. "1/125"
            aperture (str): PTP aperture e.g. "5.6"
            image_format (str): "JPEG", "RAW", or "RAW+JPEG"

        Returns the full settings dict (same shape as get_dslr_settings) after
        applying the requested changes.
        """
        lock = self._get_camera_lock(camera_index)
        with lock:
            session = self._get_or_open_session(camera_index)
            try:
                if settings.get("iso") is not None:
                    session._set_config("iso", str(settings["iso"]))
                if settings.get("shutter_speed") is not None:
                    session._set_config("shutterspeed", settings["shutter_speed"])
                if settings.get("aperture") is not None:
                    session._set_config("aperture", settings["aperture"])
                if settings.get("image_format") is not None:
                    ptp_fmt = _IMAGE_FORMAT_MAP.get(
                        settings["image_format"], _IMAGE_FORMAT_DEFAULT
                    )
                    session._set_config("imageformat", ptp_fmt)
                # Return updated state
                raw = session.get_info()
                return {
                    "iso": raw.get("iso"),
                    "shutter_speed": raw.get("shutterspeed"),
                    "aperture": raw.get("aperture"),
                    "image_format": _IMAGE_FORMAT_REVERSE_MAP.get(
                        raw.get("imageformat", ""), raw.get("imageformat")
                    ),
                    "focus_mode": raw.get("focusmode"),
                    "flash_mode": raw.get("flashmode"),
                }
            except gp.GPhoto2Error as exc:
                self.logger.error(
                    f"[gphoto2] Camera {camera_index}: apply_dslr_settings failed: {exc}"
                )
                raise RuntimeError(f"Failed to apply DSLR settings: {exc}") from exc

    def supports_streaming(self) -> bool:
        return False

    def supports_live_adjustment(self) -> bool:
        return False

    def get_capabilities(self) -> dict:
        return {
            "live_preview": True,
            "focus_control": False,
            "live_controls": False,
            "zoom": False,
            "autofocus_calibration": False,
            "dslr_settings": True,
        }

    def get_backend_name(self) -> str:
        return "gphoto2"

    def cleanup(self):
        """Close all open PTP sessions."""
        for idx in list(self._sessions.keys()):
            try:
                self._close_session(idx)
            except Exception as exc:
                self.logger.warning(
                    f"[gphoto2] cleanup: error closing session {idx}: {exc}"
                )
        self.logger.info("[gphoto2] All sessions closed.")
