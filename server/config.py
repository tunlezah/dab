"""Configuration management for DAB+ radio web application."""

import os

# App version — display only, bump manually on release
APP_VERSION = "1.0.0"

# welle-cli internal HTTP port
WELLE_CLI_PORT: int = int(os.environ.get("WELLE_CLI_PORT", "7979"))

# External web UI port
WEB_PORT: int = int(os.environ.get("WEB_PORT", "8080"))

# Path to the welle-cli binary
WELLE_CLI_PATH: str = os.environ.get("WELLE_CLI_PATH", "welle-cli")

# Seconds to dwell on each channel during a scan
SCAN_DWELL_TIME: float = float(os.environ.get("SCAN_DWELL_TIME", "4.0"))

# Seconds between DLS metadata polls
METADATA_POLL_INTERVAL: float = float(
    os.environ.get("METADATA_POLL_INTERVAL", "2.0")
)

# Seconds to wait after starting welle-cli before considering it ready
WELLE_CLI_STARTUP_DELAY: float = float(
    os.environ.get("WELLE_CLI_STARTUP_DELAY", "2.0")
)

# Australian Band III channel list: (label, frequency_MHz)
BAND_III_CHANNELS: list[tuple[str, float]] = [
    ("5A", 174.928),
    ("5B", 176.640),
    ("5C", 178.352),
    ("5D", 180.064),
    ("6A", 181.936),
    ("6B", 183.648),
    ("6C", 185.360),
    ("6D", 187.072),
    ("7A", 188.928),
    ("7B", 190.640),
    ("7C", 192.352),
    ("7D", 194.064),
    ("8A", 195.936),
    ("8B", 197.648),
    ("8C", 199.360),
    ("8D", 201.072),
    ("9A", 202.928),
    ("9B", 204.640),
    ("9C", 206.352),
    ("9D", 208.064),
    ("10A", 209.936),
    ("10B", 211.648),
    ("10C", 213.360),
    ("10D", 215.072),
    ("10N", 210.096),
    ("11A", 216.928),
    ("11B", 218.640),
    ("11C", 220.352),
    ("11D", 222.064),
    ("11N", 217.088),
    ("12A", 223.936),
    ("12B", 225.648),
    ("12C", 227.360),
    ("12D", 229.072),
    ("12N", 224.096),
    ("13A", 230.784),
    ("13B", 232.496),
    ("13C", 234.208),
    ("13D", 235.776),
    ("13E", 237.488),
    ("13F", 239.200),
]

# Channels to scan in quick/popular mode (Sydney metro defaults)
_popular_env = os.environ.get("POPULAR_CHANNELS", "")
POPULAR_CHANNELS: list[str] = (
    [ch.strip() for ch in _popular_env.split(",") if ch.strip()]
    if _popular_env
    else ["9A", "9B", "9C"]
)
