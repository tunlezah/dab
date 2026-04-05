#!/usr/bin/env bash
set -euo pipefail

# DAB+ Radio Web Application Installer
# Safe to run multiple times (idempotent)
# Usage:
#   sudo ./install.sh              Install or upgrade
#   sudo ./install.sh --uninstall  Remove everything

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION="2.0.0"
INSTALL_DIR="/opt/dab-radio"
SERVICE_NAME="dab-radio"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

# ---------------------------------------------------------------------------
# Uninstall function
# ---------------------------------------------------------------------------
do_uninstall() {
    echo "============================================"
    echo "  DAB+ Radio — Uninstalling"
    echo "============================================"

    # Stop and disable the service
    if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
        echo ">>> Stopping $SERVICE_NAME service ..."
        systemctl stop "$SERVICE_NAME"
    fi
    if systemctl is-enabled --quiet "$SERVICE_NAME" 2>/dev/null; then
        echo ">>> Disabling $SERVICE_NAME service ..."
        systemctl disable "$SERVICE_NAME"
    fi

    # Kill any orphaned welle-cli processes
    if pgrep -x welle-cli >/dev/null 2>&1; then
        echo ">>> Killing orphaned welle-cli process(es) ..."
        pkill -9 -x welle-cli || true
        sleep 1
    fi

    # Remove service file
    if [[ -f "$SERVICE_FILE" ]]; then
        echo ">>> Removing systemd service file ..."
        rm -f "$SERVICE_FILE"
        systemctl daemon-reload
    fi

    # Remove application directory
    if [[ -d "$INSTALL_DIR" ]]; then
        echo ">>> Removing $INSTALL_DIR ..."
        rm -rf "$INSTALL_DIR"
    fi

    # Remove udev rules
    if [[ -f /etc/udev/rules.d/20-rtlsdr.rules ]]; then
        echo ">>> Removing RTL-SDR udev rules ..."
        rm -f /etc/udev/rules.d/20-rtlsdr.rules
        udevadm control --reload-rules 2>/dev/null || true
    fi

    # Remove kernel module blacklist
    if [[ -f /etc/modprobe.d/blacklist-rtlsdr.conf ]]; then
        echo ">>> Removing RTL-SDR kernel module blacklist ..."
        rm -f /etc/modprobe.d/blacklist-rtlsdr.conf
    fi

    # Remove welle-cli if it was installed to /usr/local
    if [[ -f /usr/local/bin/welle-cli ]]; then
        echo ">>> Removing welle-cli from /usr/local/bin ..."
        rm -f /usr/local/bin/welle-cli
    fi

    echo ""
    echo "============================================"
    echo "  DAB+ Radio has been uninstalled."
    echo ""
    echo "  System packages (rtl-sdr, mpg123, etc.)"
    echo "  were left in place. Remove them manually"
    echo "  with apt if no longer needed."
    echo "============================================"
    exit 0
}

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------
if [[ "${1:-}" == "--uninstall" ]]; then
    if [[ $EUID -ne 0 ]]; then
        echo "ERROR: This script must be run as root (or with sudo)."
        exit 1
    fi
    do_uninstall
fi

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
# 2. Detect and clean previous installation
# ---------------------------------------------------------------------------
if [[ -d "$INSTALL_DIR" ]] || [[ -f "$SERVICE_FILE" ]]; then
    echo ">>> Previous installation detected — upgrading ..."

    # Stop the running service before we overwrite files
    if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
        echo ">>> Stopping existing $SERVICE_NAME service ..."
        systemctl stop "$SERVICE_NAME"
    fi

    # Kill any orphaned welle-cli processes from a previous run.
    # These can hold the USB device or port 7979, preventing the new
    # instance from starting (resulting in permanent "Offline" status).
    if pgrep -x welle-cli >/dev/null 2>&1; then
        echo ">>> Killing orphaned welle-cli process(es) ..."
        pkill -x welle-cli || true
        sleep 1
        # Force-kill if still alive
        if pgrep -x welle-cli >/dev/null 2>&1; then
            pkill -9 -x welle-cli || true
            sleep 1
        fi
    fi

    # Release the RTL-SDR USB device if anything else grabbed it
    if pgrep -x rtl_test >/dev/null 2>&1; then
        pkill -x rtl_test || true
    fi

    # Remove old application files (keep .env for port config)
    if [[ -d "$INSTALL_DIR" ]]; then
        echo ">>> Cleaning old application files ..."
        # Preserve .env (user port config) and venv (avoid full rebuild)
        find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 \
            ! -name '.env' ! -name 'venv' -exec rm -rf {} +
    fi
fi

# ---------------------------------------------------------------------------
# 3. Install system packages
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
# 4. Build welle.io from source (if not already installed)
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
# 5. Configure RTL-SDR
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
# 6. Set up Python environment
# ---------------------------------------------------------------------------
echo ">>> Setting up application in $INSTALL_DIR ..."
mkdir -p "$INSTALL_DIR"

# Copy project files (server/, web/, requirements.txt) from the repo checkout
for item in server web requirements.txt; do
    if [[ -e "$SCRIPT_DIR/$item" ]]; then
        cp -a "$SCRIPT_DIR/$item" "$INSTALL_DIR/"
    else
        echo "WARNING: Expected project path $SCRIPT_DIR/$item not found — skipping."
    fi
done

if [[ ! -d "$INSTALL_DIR/venv" ]]; then
    python3 -m venv "$INSTALL_DIR/venv"
fi

"$INSTALL_DIR/venv/bin/pip" install --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"

# ---------------------------------------------------------------------------
# 7. Create systemd service
# ---------------------------------------------------------------------------
echo ">>> Installing systemd service ..."
cp "$SCRIPT_DIR/dab-radio.service" "$SERVICE_FILE"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"

# ---------------------------------------------------------------------------
# 8. Port configuration
# ---------------------------------------------------------------------------
DEFAULT_PORT=8080
PORT="$DEFAULT_PORT"

# If an .env already exists with a chosen port, honour it unless that port is
# now occupied by something else.
if [[ -f "$INSTALL_DIR/.env" ]]; then
    EXISTING_PORT="$(grep -oP '^WEB_PORT=\K[0-9]+' "$INSTALL_DIR/.env" 2>/dev/null || true)"
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

echo "WEB_PORT=$PORT" > "$INSTALL_DIR/.env"
echo ">>> Using port $PORT"

# ---------------------------------------------------------------------------
# 9. Start service
# ---------------------------------------------------------------------------
# Final check: ensure welle-cli's port is free before starting
if ss -tlnp 2>/dev/null | grep -q ":7979 "; then
    echo "WARNING: Port 7979 still in use — killing the process holding it ..."
    fuser -k 7979/tcp 2>/dev/null || true
    sleep 1
fi

echo ">>> Starting $SERVICE_NAME service ..."
systemctl start "$SERVICE_NAME"
echo ">>> DAB+ Radio v$VERSION is running at http://localhost:$PORT"

# ---------------------------------------------------------------------------
# 10. Print summary
# ---------------------------------------------------------------------------
cat <<EOF

============================================
  DAB+ Radio v$VERSION — Installation Complete!
============================================
  Web UI: http://localhost:$PORT

  Manage with:
    sudo systemctl status $SERVICE_NAME
    sudo systemctl restart $SERVICE_NAME
    sudo journalctl -u $SERVICE_NAME -f

  Uninstall with:
    sudo ./install.sh --uninstall

  NOTE: If this is a fresh RTL-SDR setup,
  you may need to reboot for driver changes.
============================================
EOF
