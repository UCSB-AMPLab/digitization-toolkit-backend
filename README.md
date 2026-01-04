# digitization-toolkit-backend

## Quick Start

### Prerequisites

**For Raspberry Pi with camera support:**

1. Run the automated setup script:
```bash
cd /home/pi/dtk/backend
./setup_camera_backends.sh
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

Test: Open your browser and navigate to `http://localhost:8000` to see the health check response → `{"status": "ok"}`

## Camera Backend Configuration

The capture service supports two camera backends:

### Picamera2 Backend (Default)
- Uses official `picamera2` Python library
- **91.5% faster** than subprocess backend
- Supports streaming and live preview
- Enables dynamic settings adjustment

### Subprocess Backend (Fallback)
- Uses `rpicam-still` command-line tool
- Stable and process-isolated
- Works out of the box

**Switch backends:**
```bash
export CAMERA_BACKEND=picamera2  # Use picamera2 (default)
export CAMERA_BACKEND=subprocess # Use subprocess (fallback)
```

Or add to `.env` file:
```
CAMERA_BACKEND=picamera2
```

See [capture/BACKEND_SWITCHING.md](capture/BACKEND_SWITCHING.md) for detailed comparison.

## Testing

Run camera backend tests:
```bash
source .venv/bin/activate

# Test both backends
python capture/test_phase2_picamera2.py

# Test service integration
python capture/test_phase1_integration.py
```

## Troubleshooting

**Issue: `ModuleNotFoundError: No module named 'libcamera'`**
- Solution: Run `./setup_camera_backends.sh` or manually link system packages

**Issue: Camera not detected**
- Verify hardware connection: `rpicam-hello --list-cameras`
- Check device setup: See [docs/developers/device_setup_pi5_imx519.qmd](../docs/developers/device_setup_pi5_imx519.qmd)

**Issue: Picamera2 backend fails**
- Fall back to subprocess: `export CAMERA_BACKEND=subprocess`
- Check system packages: `dpkg -l | grep libcamera`