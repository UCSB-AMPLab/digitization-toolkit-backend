# Camera Backend Dependencies

This document lists all system and Python dependencies required for the camera backends.

## System Requirements

### Hardware
- Raspberry Pi 5 (or compatible)
- Camera modules (tested with Arducam IMX519)
- Raspberry Pi OS Bookworm (64-bit)

### Operating System Packages

#### Required for Both Backends
```bash
sudo apt-get install -y \
    python3-dev \
    libcap-dev
```

| Package | Purpose | Required For |
|---------|---------|--------------|
| `python3-dev` | Python development headers | Building Python C extensions |
| `libcap-dev` | libcap development headers | python-prctl dependency |

#### Required for Picamera2 Backend Only
```bash
sudo apt-get install -y \
    python3-libcamera \
    python3-kms++
```

| Package | Purpose | Version Tested |
|---------|---------|----------------|
| `python3-libcamera` | Python bindings for libcamera | 0.5.2+125 |
| `python3-kms++` | KMS++ Python bindings | 0~git20250807 |

#### Required for Subprocess Backend Only
```bash
# Already installed on Raspberry Pi OS with camera support
rpicam-apps  # Provides rpicam-still command
```

## Python Dependencies

### Core Dependencies (requirements.txt)
All listed in `requirements.txt` - installed via pip:
```bash
pip install -r requirements.txt
```

### Camera-Specific Dependencies
```
picamera2>=0.3.33  # For picamera2 backend
```

Dependencies of picamera2 (auto-installed):
- `numpy>=2.4.0` - Array operations
- `pillow>=12.1.0` - Image processing
- `piexif>=1.1.3` - EXIF metadata
- `simplejpeg>=1.9.0` - Fast JPEG encoding
- `python-prctl>=1.8.1` - Process control (requires libcap-dev)
- `PiDNG>=4.0.9` - DNG RAW format support

## Virtual Environment Setup

### Critical: System Package Linking

The picamera2 backend requires access to system-installed `libcamera` Python bindings, which cannot be installed via pip. The virtual environment must be configured to see system packages:

```bash
# Create venv
python3 -m venv .venv

# Link system packages (REQUIRED for picamera2 backend)
echo "/usr/lib/python3/dist-packages" > .venv/lib/python3.11/site-packages/system-packages.pth

# Activate and install
source .venv/bin/activate
pip install -r requirements.txt
```

## Automated Setup

Run the setup script to handle all dependencies:

```bash
./setup_camera_backends.sh
```

This script will:
1. ✓ Check system compatibility
2. ✓ Install system packages
3. ✓ Create/update virtual environment
4. ✓ Link system site-packages
5. ✓ Install Python dependencies
6. ✓ Verify installation

## Verification

Test that all dependencies are correctly installed:

```bash
source .venv/bin/activate

# Test system bindings
python -c "import libcamera; print('libcamera OK')"

# Test backends
python -c "from capture.backends import RpicamBackend, Picamera2Backend; print('Backends OK')"

# Run full test suite
python capture/test_phase2_picamera2.py
```

## Docker Considerations

For containerized deployments, the Dockerfile must:

1. Install system packages:
```dockerfile
RUN apt-get update && apt-get install -y \
    python3-dev \
    libcap-dev \
    python3-libcamera \
    python3-kms++ \
    rpicam-apps
```

2. Grant camera access:
```dockerfile
# Add to docker-compose.yml
devices:
  - /dev/video0:/dev/video0
  - /dev/video1:/dev/video1
privileged: true  # Required for camera access
```

3. Link system packages in container:
```dockerfile
RUN echo "/usr/lib/python3/dist-packages" > \
    /app/.venv/lib/python3.11/site-packages/system-packages.pth
```

## Troubleshooting

### Issue: python-prctl fails to build
```
error: command 'gcc' failed with exit code 1
```
**Solution:** Install build dependencies
```bash
sudo apt-get install -y python3-dev libcap-dev
```

### Issue: ModuleNotFoundError: No module named 'libcamera'
**Solution:** Link system packages to venv
```bash
echo "/usr/lib/python3/dist-packages" > \
    .venv/lib/python3.11/site-packages/system-packages.pth
```

### Issue: Camera access denied
**Solution:** Add user to video group
```bash
sudo usermod -aG video $USER
# Log out and back in
```

## Dependency Update Policy

- **System packages:** Follow Raspberry Pi OS updates
- **Python packages:** Pin versions in requirements.txt
- **libcamera:** Tied to system package version
- **picamera2:** Pin to tested version (currently >=0.3.33)

## References

- [Raspberry Pi Camera Documentation](https://www.raspberrypi.com/documentation/computers/camera_software.html)
- [libcamera Project](https://libcamera.org/)
- [Picamera2 Documentation](https://datasheets.raspberrypi.com/camera/picamera2-manual.pdf)
- [Arducam IMX519 Setup Guide](https://docs.arducam.com/Raspberry-Pi-Camera/Native-camera/16MP-IMX519/)
