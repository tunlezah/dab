"""FastAPI application entry point for DAB+ radio web application."""

import asyncio
import logging
from pathlib import Path
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from .config import DATA_DIR, WEB_PORT, DEVICE_CACHE_TTL, DEVICE_SCAN_TIMEOUT, MAX_STREAMS
from .welle_manager import WelleManager
from .scanner import Scanner
from .station_registry import StationRegistry
from .audio_manager import AudioManager
from .activity_log import ActivityLog
from .device_discovery import DeviceDiscovery
from .stream_manager import StreamManager
from .cast_controller import CastController
from . import routes

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = PROJECT_ROOT / "web"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application startup and shutdown."""
    # Startup
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    welle = WelleManager()
    registry = StationRegistry(persist_path=DATA_DIR / "stations.json")
    activity_log = ActivityLog()
    scanner = Scanner(welle, registry, activity_log)
    audio = AudioManager(welle)

    # Casting components
    discovery = DeviceDiscovery(
        cache_ttl=DEVICE_CACHE_TTL,
        scan_timeout=DEVICE_SCAN_TIMEOUT,
    )
    stream_mgr = StreamManager(max_streams=MAX_STREAMS)
    cast_ctrl = CastController(
        stream_manager=stream_mgr,
        server_host="",  # Auto-detected per request
        server_port=WEB_PORT,
    )

    # Load previously discovered stations
    loaded = await registry.load()
    if loaded:
        await activity_log.add("info", f"Loaded {loaded} stations from previous scan")

    routes.setup(
        welle, scanner, registry, audio, activity_log,
        discovery=discovery,
        stream_manager=stream_mgr,
        cast_controller=cast_ctrl,
    )

    app.state.welle = welle
    app.state.scanner = scanner
    app.state.registry = registry
    app.state.audio = audio
    app.state.activity_log = activity_log
    app.state.discovery = discovery
    app.state.stream_mgr = stream_mgr
    app.state.cast_ctrl = cast_ctrl

    logger.info("Detecting RTL-SDR device...")
    device = await welle.detect_device_name()
    if device:
        await activity_log.add("info", f"RTL-SDR device detected: {device}")
        logger.info("RTL-SDR device: %s", device)
    else:
        await activity_log.add("warn", "No RTL-SDR device detected")
        logger.warning("No RTL-SDR device detected")

    logger.info("Starting welle-cli on default channel 9A")
    await activity_log.add("info", "Starting welle-cli on channel 9A")
    started = await welle.start(channel="9A")
    if started:
        await activity_log.add("info", "welle-cli started successfully")
    else:
        await activity_log.add("error", "Failed to start welle-cli - check SDR device connection")
    logger.info("DAB+ radio web application started on port %d", WEB_PORT)

    yield

    # Shutdown
    logger.info("Shutting down DAB+ radio web application")
    await cast_ctrl.stop_all()
    await stream_mgr.stop_all()
    await audio.stop_server()
    await welle.stop()
    logger.info("Shutdown complete")


app = FastAPI(title="DAB+ Radio", lifespan=lifespan)

# CORS middleware — allow all origins for single-user local app
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount API routes
app.include_router(routes.router)


# Serve web UI
@app.get("/")
async def serve_index():
    """Serve the main web UI page."""
    index_path = WEB_DIR / "index.html"
    if not index_path.exists():
        return {"error": "Web UI not found. Place index.html in the web/ directory."}
    return FileResponse(index_path)


# Mount static files after the root route to avoid conflicts
if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


if __name__ == "__main__":
    uvicorn.run("server.main:app", host="0.0.0.0", port=WEB_PORT, log_level="info")
