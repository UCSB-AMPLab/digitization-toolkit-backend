#!/bin/bash
# Setup script for DTK capture service dependencies
# This installs system packages required for camera backends

set -e  # Exit on error

echo "=================================================="
echo "DTK Capture Service - Dependency Setup"
echo "=================================================="
echo ""

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Check if running on Raspberry Pi
if ! grep -q "Raspberry Pi" /proc/cpuinfo 2>/dev/null; then
    echo -e "${YELLOW}Warning: This script is designed for Raspberry Pi.${NC}"
    echo "Some packages may not be available on other systems."
    read -p "Continue anyway? (y/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

# Function to check if package is installed
check_installed() {
    dpkg -l "$1" &> /dev/null
    return $?
}

echo "Step 1: Updating package lists..."
sudo apt-get update -qq

echo ""
echo "Step 2: Installing system dependencies..."
echo "----------------------------------------"

# Required for both backends
COMMON_PACKAGES=(
    "python3-dev"          # Python development headers
    "libcap-dev"           # libcap development headers (for picamera2)
)

# Required for picamera2 backend
PICAMERA2_PACKAGES=(
    "python3-libcamera"    # Python bindings for libcamera
    "python3-kms++"        # KMS++ Python bindings (for display)
)

# Check and install common packages
for pkg in "${COMMON_PACKAGES[@]}"; do
    if check_installed "$pkg"; then
        echo -e "${GREEN}✓${NC} $pkg (already installed)"
    else
        echo -e "${YELLOW}→${NC} Installing $pkg..."
        sudo apt-get install -y "$pkg" || echo -e "${RED}✗ Failed to install $pkg${NC}"
    fi
done

# Check and install picamera2 packages
echo ""
echo "Installing Picamera2 system dependencies..."
for pkg in "${PICAMERA2_PACKAGES[@]}"; do
    if check_installed "$pkg"; then
        echo -e "${GREEN}✓${NC} $pkg (already installed)"
    else
        echo -e "${YELLOW}→${NC} Installing $pkg..."
        sudo apt-get install -y "$pkg" || echo -e "${RED}✗ Failed to install $pkg${NC}"
    fi
done

echo ""
echo "Step 3: Setting up Python virtual environment..."
echo "-----------------------------------------------"

BACKEND_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$BACKEND_DIR/.venv"

if [ -d "$VENV_DIR" ]; then
    echo -e "${GREEN}✓${NC} Virtual environment exists: $VENV_DIR"
else
    echo -e "${YELLOW}→${NC} Creating virtual environment..."
    python3 -m venv "$VENV_DIR"
fi

# Add system site-packages to venv (required for libcamera)
SITE_PACKAGES="$VENV_DIR/lib/python3.11/site-packages"
PTH_FILE="$SITE_PACKAGES/system-packages.pth"

if [ -f "$PTH_FILE" ]; then
    echo -e "${GREEN}✓${NC} System packages already linked to venv"
else
    echo -e "${YELLOW}→${NC} Linking system site-packages to venv..."
    echo "/usr/lib/python3/dist-packages" > "$PTH_FILE"
    echo -e "${GREEN}✓${NC} System packages linked (libcamera accessible in venv)"
fi

echo ""
echo "Step 4: Installing Python dependencies..."
echo "----------------------------------------"

source "$VENV_DIR/bin/activate"

# Upgrade pip
echo -e "${YELLOW}→${NC} Upgrading pip..."
pip install --upgrade pip -q

# Install requirements
if [ -f "$BACKEND_DIR/requirements.txt" ]; then
    echo -e "${YELLOW}→${NC} Installing from requirements.txt..."
    pip install -r "$BACKEND_DIR/requirements.txt"
    echo -e "${GREEN}✓${NC} Python dependencies installed"
else
    echo -e "${RED}✗ requirements.txt not found${NC}"
    exit 1
fi

echo ""
echo "Step 5: Verifying installation..."
echo "--------------------------------"

# Test imports
echo -e "${YELLOW}→${NC} Testing camera backend imports..."

if python3 -c "from capture.backends import RpicamBackend; print('✓ RpicamBackend')" 2>/dev/null; then
    echo -e "${GREEN}✓${NC} Subprocess backend available"
else
    echo -e "${RED}✗ Subprocess backend failed to import${NC}"
fi

if python3 -c "from capture.backends import Picamera2Backend; print('✓ Picamera2Backend')" 2>/dev/null; then
    echo -e "${GREEN}✓${NC} Picamera2 backend available"
else
    echo -e "${RED}✗ Picamera2 backend failed to import${NC}"
    echo -e "${YELLOW}  This may be due to missing system packages${NC}"
fi

# Test libcamera
if python3 -c "import libcamera; print('✓ libcamera')" 2>/dev/null; then
    echo -e "${GREEN}✓${NC} libcamera Python bindings accessible"
else
    echo -e "${YELLOW}⚠${NC} libcamera not accessible (picamera2 backend will not work)"
fi

echo ""
echo "=================================================="
echo -e "${GREEN}Setup Complete!${NC}"
echo "=================================================="
echo ""
echo "Next steps:"
echo "  1. Activate virtual environment:"
echo "     source $VENV_DIR/bin/activate"
echo ""
echo "  2. Choose camera backend (optional):"
echo "     export CAMERA_BACKEND=subprocess  # (default)"
echo "     export CAMERA_BACKEND=picamera2   # (recommended for better performance)"
echo ""
echo "  3. Run tests:"
echo "     python capture/test_phase2_picamera2.py"
echo ""
echo "  4. Start the service:"
echo "     python -m uvicorn app.main:app --reload"
echo ""
