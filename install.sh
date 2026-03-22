#!/usr/bin/env bash
set -euo pipefail

# DAB+ Radio Web Application Installer
# Safe to run multiple times (idempotent)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
# 1. Check prerequisites
# ---------------------------------------------------------------------------
if [[ $EUID -ne 0 ]]; then
    echo "ERROR: This script must be run as root (or with sudo)."
    exit 1
fi

if [[ -f /etc/os-release ]]; then
    . /etc/os-release
    if [[ "${VERSION_ID:-}" != "24.04" ]]; then
        echo "WARNING: This installer targets Ubuntu 24.04. Detected: ${PRETTY_NAME:-unknown}."
        echo "         Continuing anyway — some packages may differ."
    fi
else
    echo "WARNING: Unable to determine OS version. Continuing anyway."
fi

# ---------------------------------------------------------------------------
# 2. Install system packages
# ---------------------------------------------------------------------------
echo ">>> Installing system packages ..."
apt-get update
apt-get install -y \
    rtl-sdr librtlsdr-dev \
    build-essential cmake git \
    libfaad-dev libmpg123-dev libfftw3-dev \
    libusb-1.0-0-dev \
    libmp3lame-dev \
    libflac++-dev \
    python3 python3-pip python3-venv \
    alsa-utils \
    mpg123 \
    xxd

# ---------------------------------------------------------------------------
# 3. Build welle.io from source (if not already installed)
# ---------------------------------------------------------------------------
if command -v welle-cli &>/dev/null; then
    echo ">>> welle-cli already installed, skipping"
else
    echo ">>> Building welle-cli from source ..."
    BUILD_DIR="/tmp/welle-io-build"
    rm -rf "$BUILD_DIR"
    git clone https://github.com/AlbrechtL/welle.io.git "$BUILD_DIR"
    mkdir -p "$BUILD_DIR/build"
    pushd "$BUILD_DIR/build" >/dev/null
    cmake ../ -DBUILD_WELLE_IO=OFF -Wno-dev
    make -j"$(nproc)"
    make install
    popd >/dev/null
    rm -rf "$BUILD_DIR"
    echo ">>> welle-cli installed to $(command -v welle-cli || echo /usr/local/bin/welle-cli)"
fi

# ---------------------------------------------------------------------------
# 4. Configure RTL-SDR
# ---------------------------------------------------------------------------
echo ">>> Configuring RTL-SDR kernel module blacklist and udev rules ..."

cat > /etc/modprobe.d/blacklist-rtlsdr.conf <<'EOF'
blacklist dvb_usb_rtl28xxu
blacklist rtl2832
blacklist rtl2830
EOF

cat > /etc/udev/rules.d/20-rtlsdr.rules <<'EOF'
SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2832", MODE="0666"
SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2838", MODE="0666"
EOF

udevadm control --reload-rules && udevadm trigger

if lsmod | grep -q dvb_usb_rtl28xxu; then
    echo "NOTE: The dvb_usb_rtl28xxu kernel module is currently loaded."
    echo "      A reboot is required for the RTL-SDR blacklist to take effect."
fi

# ---------------------------------------------------------------------------
# 5. Set up Python environment
# ---------------------------------------------------------------------------
echo ">>> Setting up application in /opt/dab-radio ..."
mkdir -p /opt/dab-radio

# Copy project files (server/, web/, requirements.txt) from the repo checkout
for item in server web requirements.txt; do
    if [[ -e "$SCRIPT_DIR/$item" ]]; then
        cp -a "$SCRIPT_DIR/$item" /opt/dab-radio/
    else
        echo "WARNING: Expected project path $SCRIPT_DIR/$item not found — skipping."
    fi
done

if [[ ! -d /opt/dab-radio/venv ]]; then
    python3 -m venv /opt/dab-radio/venv
fi

/opt/dab-radio/venv/bin/pip install --upgrade pip
/opt/dab-radio/venv/bin/pip install -r /opt/dab-radio/requirements.txt

# ---------------------------------------------------------------------------
# 6. Create systemd service
# ---------------------------------------------------------------------------
echo ">>> Installing systemd service ..."
cp "$SCRIPT_DIR/dab-radio.service" /etc/systemd/system/dab-radio.service
systemctl daemon-reload
systemctl enable dab-radio.service

# ---------------------------------------------------------------------------
# 7. Port configuration
# ---------------------------------------------------------------------------
DEFAULT_PORT=8080
PORT="$DEFAULT_PORT"

# If an .env already exists with a chosen port, honour it unless that port is
# now occupied by something else.
if [[ -f /opt/dab-radio/.env ]]; then
    EXISTING_PORT="$(grep -oP '^WEB_PORT=\K[0-9]+' /opt/dab-radio/.env 2>/dev/null || true)"
    if [[ -n "$EXISTING_PORT" ]]; then
        PORT="$EXISTING_PORT"
    fi
fi

# Walk through candidate ports until we find a free one.
port_in_use() {
    # A port is considered "in use" if something other than our own service
    # is listening on it.
    ss -tlnp 2>/dev/null | grep -q ":${1} " && return 0
    return 1
}

if port_in_use "$PORT"; then
    # The current/desired port is taken — scan for an alternative.
    FOUND=false
    for CANDIDATE in $(seq "$DEFAULT_PORT" 8090); do
        if ! port_in_use "$CANDIDATE"; then
            PORT="$CANDIDATE"
            FOUND=true
            break
        fi
    done
    if [[ "$FOUND" != true ]]; then
        echo "WARNING: All candidate ports 8080-8090 are in use. Defaulting to $DEFAULT_PORT."
        PORT="$DEFAULT_PORT"
    fi
fi

echo "WEB_PORT=$PORT" > /opt/dab-radio/.env
echo ">>> Using port $PORT"

# ---------------------------------------------------------------------------
# 8. Start service
# ---------------------------------------------------------------------------
echo ">>> Starting dab-radio service ..."
systemctl start dab-radio.service
echo ">>> DAB+ Radio is running at http://localhost:$PORT"

# ---------------------------------------------------------------------------
# 9. Print summary
# ---------------------------------------------------------------------------
cat <<EOF

============================================
  DAB+ Radio Installation Complete!
============================================
  Web UI: http://localhost:$PORT

  Manage with:
    sudo systemctl status dab-radio
    sudo systemctl restart dab-radio
    sudo journalctl -u dab-radio -f

  NOTE: If this is a fresh RTL-SDR setup,
  you may need to reboot for driver changes.
============================================
EOF
