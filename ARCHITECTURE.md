# DAB+ Radio Web Application — Architecture Plan

## Planner Agent Output

### Executive Summary

Build a web-based DAB+ radio application for Australian Band III using RTL-SDR hardware.
The system uses **welle-cli's built-in web server** as the DAB+ engine, with a **Python
(FastAPI) backend** as orchestrator/proxy, and a **single-page web UI**.

---

## 1. KEY ARCHITECTURAL DISCOVERY

welle-cli (welle.io's CLI) includes a built-in HTTP server (`-w <port>`) that exposes:

| Endpoint | Method | Description |
|---|---|---|
| `/mux.json` | GET | Full ensemble data: services, labels, bitrate, DLS, components, SNR, etc. |
| `/mp3/{serviceId}` | GET | MP3 audio stream for a service (continuous HTTP stream) |
| `/flac/{serviceId}` | GET | FLAC audio stream for a service |
| `/stream/{serviceId}` | GET | Raw audio stream |
| `/slide/{serviceId}` | GET | MOT slideshow images |
| `/channel` | GET | Current tuned channel |
| `/channel` | POST | Retune to a new channel (body = channel name e.g. "9A") |
| `/mux.m3u` | GET | M3U playlist of available services |
| `/spectrum` | GET | Spectrum data |
| `/impulseresponse` | GET | Impulse response data |

**This means we do NOT need to parse welle-cli's stdout or manage PCM audio directly.**
welle-cli handles all DAB+ decoding, metadata extraction, and audio encoding internally.

---

## 2. SYSTEM ARCHITECTURE

```
┌─────────────────────────────────────────────────────────────┐
│                        Web Browser                          │
│  ┌───────────────────────────────────────────────────────┐  │
│  │                   Web UI (HTML/JS/CSS)                 │  │
│  │  - Scan button → POST /api/scan                       │  │
│  │  - Station list → GET /api/stations                   │  │
│  │  - Play button → GET /api/play/{id}                   │  │
│  │  - Metadata poll → GET /api/metadata                  │  │
│  │  - <audio src="/api/stream/{id}"> for browser play    │  │
│  └───────────────────────────────────────────────────────┘  │
└──────────────────────────┬──────────────────────────────────┘
                           │ HTTP
┌──────────────────────────▼──────────────────────────────────┐
│               Python Backend (FastAPI) :8080                │
│                                                             │
│  /api/scan        → Iterate Band III channels via welle-cli │
│  /api/stations    → Return discovered station registry      │
│  /api/play/{id}   → Start server-side playback (aplay)      │
│  /api/stop        → Stop current playback                   │
│  /api/metadata    → Return current station metadata/DLS     │
│  /api/stream/{id} → Proxy MP3 stream from welle-cli        │
│  /api/status      → System status (SDR, signal, errors)     │
│  /                → Serve web UI                            │
│                                                             │
│  Manages:                                                   │
│  - welle-cli subprocess lifecycle                           │
│  - Channel scanning state machine                           │
│  - Station registry (in-memory)                             │
│  - Server-side audio playback (aplay subprocess)            │
│  - Error handling (USB disconnect, no signal, etc.)         │
└──────────────────────────┬──────────────────────────────────┘
                           │ HTTP (localhost)
┌──────────────────────────▼──────────────────────────────────┐
│              welle-cli -w <internal_port> -F rtl_sdr        │
│                                                             │
│  - DAB+ demodulation & decoding                             │
│  - Ensemble/service parsing                                 │
│  - DLS (Dynamic Label Segment) extraction                   │
│  - HE-AAC v2 → MP3 transcoding                             │
│  - HTTP API on internal port                                │
│  - Controlled by Python backend                             │
└──────────────────────────┬──────────────────────────────────┘
                           │ USB
┌──────────────────────────▼──────────────────────────────────┐
│                  RTL-SDR USB Dongle (RTL2832U)              │
└─────────────────────────────────────────────────────────────┘
```

---

## 3. SCANNING STRATEGY

Australian DAB+ uses Band III. Channels to scan:

| Channel | Freq (MHz) | Channel | Freq (MHz) |
|---------|-----------|---------|-----------|
| 5A | 174.928 | 9A | 202.928 |
| 5B | 176.640 | 9B | 204.640 |
| 5C | 178.352 | 9C | 206.352 |
| 5D | 180.064 | 9D | 208.064 |
| 6A | 181.936 | 10A | 209.936 |
| 6B | 183.648 | 10B | 211.648 |
| 6C | 185.360 | 10C | 213.360 |
| 6D | 187.072 | 10D | 215.072 |
| 7A | 188.928 | 11A | 216.928 |
| 7B | 190.640 | 11B | 218.640 |
| 7C | 192.352 | 11C | 220.352 |
| 7D | 194.064 | 11D | 222.064 |
| 8A | 195.936 | 12A | 223.936 |
| 8B | 197.648 | 12B | 225.648 |
| 8C | 199.360 | 12C | 227.360 |
| 8D | 201.072 | 12D | 229.072 |
| | | 13A | 230.784 |
| | | 13B | 232.496 |
| | | 13C | 234.208 |
| | | 13D | 235.776 |
| | | 13E | 237.488 |
| | | 13F | 239.200 |

**Scan Algorithm:**
1. For each channel in Band III (5A → 13F):
   a. POST `/channel` to welle-cli to tune to the channel
   b. Wait 3-4 seconds for ensemble lock
   c. GET `/mux.json` to check if services were found
   d. If services found: extract ensemble name, all services with metadata
   e. Store in station registry with channel association
   f. Report progress to UI via SSE or polling
2. After full sweep, return complete station list

---

## 4. METADATA EXTRACTION

From welle-cli's `/mux.json` response, we extract:

```json
{
  "ensemble": {
    "label": "ABC NSW DAB",        // Ensemble name
    "id": "0x1001"
  },
  "services": [
    {
      "sid": "0x1301",
      "label": "triple j",         // Station name
      "bitrate": 128,              // Bitrate (kbps)
      "dls": "Now Playing: ...",   // Dynamic Label Segment
      "mode": "DAB+",
      "protection": "EEP 3-A",
      "components": [...]
    }
  ],
  "demodulator": {
    "snr": 15.2,                   // Signal-to-noise ratio
    "frequencyCorrection": 1234
  }
}
```

**DLS (Now Playing) is provided directly by welle-cli** via the `/mux.json` endpoint.
We poll this endpoint periodically (every 2 seconds) when a station is active.

---

## 5. AUDIO ROUTING

### Browser Playback
- Frontend sets `<audio src="/api/stream/{serviceId}">`
- Python backend proxies the MP3 stream from welle-cli's `/mp3/{serviceId}`
- MP3 format works in both Safari and Chrome
- welle-cli's default output codec is MP3 (via LAME)

### Server-side Playback
- Python backend fetches MP3 stream from welle-cli
- Pipes it through `ffmpeg` or `mpg123` to ALSA for local speaker output
- Command: `mpg123 -q http://localhost:<welle_port>/mp3/<serviceId>`
- Or: pipe the stream bytes to `ffmpeg -f mp3 -i pipe:0 -f alsa default`

### Output Switching
- UI provides toggle: "Browser" / "Server" / "Both"
- "Browser": audio element plays, no server subprocess
- "Server": Python starts mpg123/ffmpeg subprocess, audio element paused
- "Both": both active simultaneously (welle-cli serves multiple clients)

---

## 6. FILE STRUCTURE

```
dab/
├── install.sh                    # Main installer script
├── dab-radio.service             # systemd service file
├── requirements.txt              # Python dependencies
├── server/
│   ├── __init__.py
│   ├── main.py                   # FastAPI application entry point
│   ├── config.py                 # Configuration management
│   ├── welle_manager.py          # welle-cli process management
│   ├── scanner.py                # Band III scanning logic
│   ├── station_registry.py       # In-memory station storage
│   ├── audio_manager.py          # Server-side audio playback
│   └── routes.py                 # API route definitions
├── web/
│   ├── index.html                # Single-page web UI
│   ├── app.js                    # Frontend JavaScript
│   └── style.css                 # Styles
└── tests/
    ├── test_scanner.py           # Scanner tests
    ├── test_welle_manager.py     # Process management tests
    └── test_routes.py            # API endpoint tests
```

---

## 7. INSTALLER SCRIPT (install.sh)

The installer must:

1. **Check prerequisites**: Ubuntu 24.04, root/sudo access
2. **Install system packages**:
   - `rtl-sdr librtlsdr-dev` — RTL-SDR drivers
   - `build-essential cmake git` — Build tools
   - `libfaad-dev libmpg123-dev libfftw3-dev` — Audio libs
   - `libusb-1.0-0-dev` — USB support
   - `liblame-dev` — MP3 encoding (for welle-cli)
   - `python3 python3-pip python3-venv` — Python runtime
   - `alsa-utils` — Server audio playback
   - `mpg123` — MP3 player for server output
3. **Build welle.io from source**:
   - Clone https://github.com/AlbrechtL/welle.io
   - cmake with `-DRTLSDR=1 -DBUILD_WELLE_CLI=ON`
   - Build and install welle-cli binary
4. **Configure RTL-SDR**:
   - Blacklist `dvb_usb_rtl28xxu` and `rtl2832` kernel modules
   - Create udev rule: `SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2838", MODE="0666"`
   - Reload udev rules
5. **Set up Python environment**:
   - Create venv in /opt/dab-radio
   - Install FastAPI, uvicorn, httpx, etc.
6. **Port selection**:
   - Default to 8080 for web UI
   - Prompt user, auto-detect conflicts with `ss -tlnp`
   - welle-cli uses an internal port (7979) not exposed externally
7. **Install systemd service**:
   - Service file runs the Python backend
   - Backend manages welle-cli subprocess internally
8. **Start and enable service**

---

## 8. ERROR HANDLING

| Scenario | Detection | Response |
|---|---|---|
| USB disconnected | welle-cli process exits / stderr | Show "SDR disconnected" in UI, attempt reconnect every 5s |
| No signal on channel | Empty services in /mux.json | Skip channel during scan, show "No signal" if tuned |
| Port conflict | `ss -tlnp` check, bind error | Auto-select next free port |
| welle-cli crash | Process monitor in Python | Restart with backoff |
| No audio device | aplay fails | Disable server playback option, browser-only mode |
| Network timeout | httpx timeout to welle-cli | Retry with backoff, surface error to UI |

---

## 9. TECHNOLOGY CHOICES

| Component | Technology | Justification |
|---|---|---|
| DAB+ Engine | welle-cli | Proven DAB+ support, built-in web server, MP3 output, DLS |
| Backend | Python + FastAPI | Async-native, StreamingResponse, lightweight |
| HTTP Client | httpx | Async HTTP client for proxying welle-cli streams |
| Frontend | Vanilla HTML/JS/CSS | No build step, no dependencies, single-user app |
| Server Audio | mpg123 or aplay+ffmpeg | Lightweight MP3-to-ALSA playback |
| Process Mgmt | subprocess + asyncio | Native Python, no extra deps |
| Installer | Bash script | Universal on Ubuntu, no bootstrap deps needed |

---

## 10. API SPECIFICATION

### GET /api/status
Returns system status.
```json
{
  "sdr_connected": true,
  "welle_running": true,
  "current_channel": "9C",
  "scanning": false,
  "playing": null,
  "output_mode": "browser",
  "signal_snr": 15.2
}
```

### POST /api/scan
Starts a full Band III scan. Returns immediately, progress via polling.
```json
{"status": "scanning", "message": "Scan started"}
```

### GET /api/scan/progress
Returns scan progress.
```json
{
  "scanning": true,
  "current_channel": "8A",
  "channels_scanned": 12,
  "channels_total": 38,
  "stations_found": 24,
  "progress_percent": 31
}
```

### GET /api/stations
Returns all discovered stations.
```json
{
  "stations": [
    {
      "id": "0x1301",
      "name": "triple j",
      "ensemble": "ABC NSW DAB",
      "channel": "9A",
      "bitrate": 128,
      "mode": "DAB+",
      "dls": "Now Playing: Song Name - Artist"
    }
  ]
}
```

### POST /api/play/{service_id}
Tune and play a station. Body: `{"output": "browser"|"server"|"both"}`

### DELETE /api/play
Stop current playback.

### GET /api/stream/{service_id}
Proxied MP3 audio stream. Content-Type: audio/mpeg.

### GET /api/metadata
Current playing station's live metadata.
```json
{
  "station_name": "triple j",
  "ensemble": "ABC NSW DAB",
  "channel": "9A",
  "bitrate": 128,
  "dls": "Now Playing: Song Name - Artist",
  "snr": 15.2,
  "mot_image": "/api/slide/0x1301"
}
```

---

## 11. RISKS & MITIGATIONS

| Risk | Mitigation |
|---|---|
| welle-cli build fails on Ubuntu 24.04 | Pin to known-good commit/tag; include build patches if needed |
| welle-cli /mux.json format varies | Defensive parsing with fallbacks for missing fields |
| MP3 stream latency too high | Use `-flush_packets 1` equivalent, tune welle-cli buffer |
| DLS not available for some stations | Show "No information available" gracefully |
| RTL-SDR driver conflicts | Installer blacklists DVB-T modules, provides troubleshooting |
| Single welle-cli instance limitation | Design scanning to stop/restart welle-cli per channel |

---

## 12. IMPLEMENTATION ORDER

1. **Installer script** — Get welle-cli building and running
2. **WelleManager** — Python class to manage welle-cli subprocess
3. **Scanner** — Band III sweep using WelleManager
4. **StationRegistry** — In-memory store for discovered stations
5. **AudioManager** — Server-side playback via mpg123
6. **FastAPI routes** — All API endpoints
7. **Web UI** — HTML/JS/CSS frontend
8. **systemd service** — Service file and integration
9. **Error handling** — All failure scenarios
10. **Testing** — Functional and failure tests
