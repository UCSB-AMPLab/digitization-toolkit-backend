# digitization-toolkit-backend

## Quick Start

### Prerequisites

**For Raspberry Pi with camera support:**

1. Run the automated setup script:
```bash
cd /home/pi/dtk/backend
./scripts/setup_camera_backends.sh
```

This will install:
- System dependencies (libcap-dev, python3-dev)
- Camera system packages (python3-libcamera, python3-kms++)
- Python virtual environment with all requirements
- Proper linking to system packages

**Manual setup (alternative):**

```bash
# Install system dependencies
sudo apt-get update
sudo apt-get install -y python3-dev libcap-dev python3-libcamera python3-kms++

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Link system packages for libcamera access
echo "/usr/lib/python3/dist-packages" > .venv/lib/python3.11/site-packages/system-packages.pth

# Install Python dependencies
pip install -r requirements.txt
```

### Running the Server

```bash
source .venv/bin/activate
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Test: Open your browser and navigate to `http://localhost:8000` to see the health check response -> `{"status": "ok"}`

## Camera Backend Configuration

The capture service has four camera drivers:

- `picamera2`: Pi cameras, through the `picamera2` library. Streaming, live preview and settings that change while it runs.
- `subprocess`: Pi cameras, through the `rpicam-still` command. A fallback for a unit where `picamera2` does not load.
- `gphoto2`: Canon DSLRs.
- `chdk`: Canon PowerShot A2500 bodies running CHDK.

**Which one a unit uses.** At start-up the backend settles the kind in this order: the choice an administrator stored with `PUT /system/camera-kind`; `CAMERA_BACKEND` in the environment or `.env`; the cameras that are connected; the last kind it used; `picamera2`. A unit with its cameras connected needs no setting.

To set it by hand, add the line to `.env` and restart the service:
```
CAMERA_BACKEND=gphoto2
```

`GET /system/camera-kind` reports the kind in effect, why it was chosen, what is connected and which drivers the unit has.

See [capture/BACKEND_SWITCHING.md](capture/BACKEND_SWITCHING.md) for detailed comparison.

## Testing

Run camera backend tests:
```bash
source .venv/bin/activate

# Needs the cameras connected
python -m pytest tests/test_cameras.py -m camera
```

## Troubleshooting

**Issue: `ModuleNotFoundError: No module named 'libcamera'`**
- Solution: Run `./scripts/setup_camera_backends.sh` or manually link system packages

**Issue: Camera not detected**
- Verify hardware connection: `rpicam-hello --list-cameras`
- Check device setup: see the [Pi 5 and IMX519 guide](https://captua.org/docs/deployment/pi5-imx519/)

**Issue: Picamera2 backend fails**
- Fall back to subprocess: `export CAMERA_BACKEND=subprocess`
- Check system packages: `dpkg -l | grep libcamera`

## License

Copyright (c) 2025 UCSB - Archives, Memory & Preservation Lab.

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU Affero General Public License as published by the Free
Software Foundation, either version 3 of the License, or (at your option) any
later version. See [LICENSE](LICENSE) for the full text.