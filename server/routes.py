"""FastAPI API route definitions for DAB+ radio web application."""

import asyncio
import logging
import socket

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from .config import APP_VERSION, WELLE_CLI_PORT
from .welle_manager import WelleManager
from .scanner import Scanner
from .station_registry import StationRegistry
from .audio_manager import AudioManager
from .activity_log import ActivityLog
from .device_discovery import DeviceDiscovery
from .stream_manager import StreamManager, CONTENT_TYPES
from .cast_controller import CastController

logger = logging.getLogger(__name__)


def _extract_label(value) -> str:
    """Extract a label string from welle-cli data.

    welle-cli may return labels as plain strings or as dicts like:
    {"label": "ABC NSW DAB", "shortlabel": "ABC", ...}
    """
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return (value.get("label") or value.get("fig2label") or "").strip()
    return ""

router = APIRouter(prefix="/api")

# Module-level references set by setup()
_welle: WelleManager = None
_scanner: Scanner = None
_registry: StationRegistry = None
_audio: AudioManager = None
_activity_log: ActivityLog = None
_discovery: DeviceDiscovery = None
_stream_mgr: StreamManager = None
_cast_ctrl: CastController = None


def setup(
    welle: WelleManager,
    scanner: Scanner,
    registry: StationRegistry,
    audio: AudioManager,
    activity_log: ActivityLog,
    discovery: DeviceDiscovery | None = None,
    stream_manager: StreamManager | None = None,
    cast_controller: CastController | None = None,
) -> None:
    """Inject dependencies into the routes module."""
    global _welle, _scanner, _registry, _audio, _activity_log
    global _discovery, _stream_mgr, _cast_ctrl
    _welle, _scanner, _registry, _audio, _activity_log = welle, scanner, registry, audio, activity_log
    _discovery = discovery
    _stream_mgr = stream_manager
    _cast_ctrl = cast_controller


@router.get("/status")
async def get_status() -> dict:
    """Return system status."""
    snr = None
    if _welle.running:
        mux = await _welle.get_mux_json()
        if mux and "demodulator" in mux:
            snr = mux["demodulator"].get("snr")

    return {
        "sdr_connected": await _welle.is_healthy(),
        "welle_running": _welle.running,
        "current_channel": _welle.current_channel,
        "scanning": _scanner.scanning,
        "playing": _audio.current_service_id,
        "output_mode": _audio.output_mode,
        "signal_snr": snr,
        "sdr_device_name": _welle.device_name,
        "version": APP_VERSION,
    }


@router.post("/scan")
async def start_scan(mode: str = Query(default="full")) -> dict:
    """Start a full Band III scan or a quick popular-channels scan."""
    if _scanner.scanning:
        raise HTTPException(status_code=409, detail="Scan already in progress")

    if mode == "popular":
        asyncio.create_task(_scanner.scan_popular())
    else:
        asyncio.create_task(_scanner.scan_all())

    return {"status": "scanning", "message": "Scan started"}


@router.get("/scan/progress")
async def scan_progress() -> dict:
    """Return current scan progress."""
    return _scanner.progress


@router.get("/logs")
async def get_logs(after: int = Query(default=0)) -> dict:
    """Return activity log entries after a given sequence number."""
    entries = await _activity_log.get_since(after)
    return {"entries": entries}


@router.get("/stations")
async def get_stations() -> dict:
    """Return all discovered stations."""
    return {"stations": await _registry.get_all()}


@router.post("/play/{service_id}")
async def play_station(service_id: str, body: dict | None = None) -> dict:
    """Tune to a station and start playback."""
    station = await _registry.get_station(service_id)
    if station is None:
        raise HTTPException(status_code=404, detail="Station not found")

    channel = station.get("channel")
    if channel and channel != _welle.current_channel:
        success = await _welle.tune(channel)
        if not success:
            raise HTTPException(status_code=500, detail="Failed to tune to channel")

    output_mode = "browser"
    if body and "output" in body:
        output_mode = body["output"]

    await _audio.set_output_mode(output_mode)
    _audio._current_service_id = service_id

    if output_mode in ("server", "both"):
        await _audio.play_server(service_id)

    return {"status": "playing", "service_id": service_id}


@router.delete("/play")
async def stop_playback() -> dict:
    """Stop current playback."""
    await _audio.stop_server()
    return {"status": "stopped"}


@router.get("/stream/{service_id}")
async def stream_audio(service_id: str) -> StreamingResponse:
    """Proxy the MP3 stream from welle-cli."""
    url = f"http://localhost:{WELLE_CLI_PORT}/mp3/{service_id}"

    async def generate():
        async with httpx.AsyncClient(timeout=None) as client:
            try:
                async with client.stream("GET", url) as response:
                    async for chunk in response.aiter_bytes(chunk_size=4096):
                        yield chunk
            except httpx.HTTPError as exc:
                logger.error("Stream proxy error for service %s: %s", service_id, exc)

    return StreamingResponse(
        generate(),
        media_type="audio/mpeg",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


@router.get("/slide/{service_id}")
async def get_slide(service_id: str) -> StreamingResponse:
    """Proxy the slideshow image from welle-cli."""
    url = f"http://localhost:{WELLE_CLI_PORT}/slide/{service_id}"

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.error("Slide fetch error for service %s: %s", service_id, exc)
        raise HTTPException(status_code=502, detail="Failed to fetch slide from welle-cli")

    content_type = response.headers.get("content-type", "image/png")

    return StreamingResponse(
        iter([response.content]),
        media_type=content_type,
    )


@router.get("/metadata")
async def get_metadata() -> dict:
    """Return current playing station's live metadata."""
    service_id = _audio.current_service_id
    if service_id is None:
        return {
            "station_name": None,
            "ensemble": None,
            "channel": None,
            "bitrate": None,
            "dls": None,
            "snr": None,
            "mot_image": None,
        }

    mux = await _welle.get_mux_json()
    if mux is None:
        return {
            "station_name": None,
            "ensemble": None,
            "channel": _welle.current_channel,
            "bitrate": None,
            "dls": None,
            "snr": None,
            "mot_image": None,
        }

    # Extract ensemble name
    ensemble_name = None
    ensemble = mux.get("ensemble")
    if isinstance(ensemble, dict):
        ensemble_name = _extract_label(ensemble.get("label", "")) or None

    # Extract SNR from demodulator
    snr = None
    demodulator = mux.get("demodulator")
    if demodulator:
        snr = demodulator.get("snr")

    # Find the currently playing service in the mux data
    station_name = None
    bitrate = None
    dls = None
    mode = None
    has_slide = False

    services = mux.get("services", [])
    for service in services:
        sid = service.get("sid", "")
        if sid == service_id:
            station_name = _extract_label(service.get("label", "")) or None
            bitrate = service.get("bitrate")
            dls = service.get("dls_label", service.get("dls", ""))
            if isinstance(dls, dict):
                dls = _extract_label(dls)
            mode = service.get("mode", "")
            has_slide = bool(service.get("mot", {}).get("data") if isinstance(service.get("mot"), dict) else False)
            if not bitrate:
                components = service.get("components", [])
                for comp in components:
                    if isinstance(comp, dict):
                        sc = comp.get("subchannel", {})
                        if isinstance(sc, dict) and sc.get("bitrate"):
                            bitrate = sc["bitrate"]
                            break
            break

    return {
        "station_name": station_name,
        "ensemble": ensemble_name,
        "channel": _welle.current_channel,
        "bitrate": bitrate,
        "mode": mode,
        "dls": dls,
        "snr": snr,
        "mot_image": f"/api/slide/{service_id}" if has_slide else None,
    }


# ---- Casting / Streaming Endpoints ----


def _get_server_host(request: Request) -> str:
    """Determine the server host address for cast stream URLs.

    The casting device needs to reach our server, so we need an address
    that's accessible from the local network.
    """
    from .config import CAST_SERVER_HOST
    if CAST_SERVER_HOST:
        return CAST_SERVER_HOST

    # Try to get the host from the request
    host = request.headers.get("host", "")
    if host:
        # Strip port if present
        return host.split(":")[0]

    # Fallback: detect local IP
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


@router.post("/devices/scan")
async def scan_devices() -> dict:
    """Scan for Chromecast and AirPlay devices on the local network.

    Discovery runs for ~8 seconds (configurable) and results are cached.
    """
    if _discovery is None:
        raise HTTPException(status_code=503, detail="Device discovery not available")

    if _discovery.scanning:
        raise HTTPException(status_code=409, detail="Device scan already in progress")

    await _activity_log.add("info", "Scanning for casting devices...")

    try:
        devices = await _discovery.scan()
    except Exception as exc:
        logger.error("Device scan failed: %s", exc)
        await _activity_log.add("error", f"Device scan failed: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))

    chromecast_count = sum(1 for d in devices if d.device_type == "chromecast")
    airplay_count = sum(1 for d in devices if d.device_type == "airplay")
    await _activity_log.add(
        "info",
        f"Found {chromecast_count} Chromecast and {airplay_count} AirPlay device(s)",
    )

    return {
        "devices": [d.to_dict() for d in devices],
        "count": len(devices),
    }


@router.get("/devices")
async def get_devices() -> dict:
    """Return the cached list of discovered devices."""
    if _discovery is None:
        return {"devices": [], "count": 0, "cache_valid": False}

    devices = _discovery.devices
    return {
        "devices": [d.to_dict() for d in devices],
        "count": len(devices),
        "cache_valid": _discovery.cache_valid,
    }


@router.post("/cast/chromecast")
async def cast_chromecast(body: dict, request: Request) -> dict:
    """Start casting to a Chromecast device.

    Body: {"device_id": "...", "service_id": "..."}
    """
    if _cast_ctrl is None:
        raise HTTPException(status_code=503, detail="Casting not available")

    device_id = body.get("device_id")
    service_id = body.get("service_id")
    if not device_id or not service_id:
        raise HTTPException(status_code=400, detail="device_id and service_id required")

    device = _discovery.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found. Run a device scan first.")

    if device.device_type != "chromecast":
        raise HTTPException(status_code=400, detail="Device is not a Chromecast")

    station = await _registry.get_station(service_id)
    if station is None:
        raise HTTPException(status_code=404, detail="Station not found")

    station_name = station.get("name", "DAB+ Radio")

    # Ensure welle-cli is tuned to the right channel
    channel = station.get("channel")
    if channel and channel != _welle.current_channel:
        success = await _welle.tune(channel)
        if not success:
            raise HTTPException(status_code=500, detail="Failed to tune to channel")

    # Update cast controller's server host
    host = _get_server_host(request)
    _cast_ctrl._server_host = host

    await _activity_log.add("info", f"Casting '{station_name}' to Chromecast '{device.name}'")

    try:
        session = await _cast_ctrl.cast_to_chromecast(device, service_id, station_name)
        return {"status": "playing", "session": session.to_dict()}
    except RuntimeError as exc:
        await _activity_log.add("error", f"Chromecast cast failed: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/cast/airplay")
async def cast_airplay(body: dict, request: Request) -> dict:
    """Start casting to an AirPlay device.

    Body: {"device_id": "...", "service_id": "..."}
    """
    if _cast_ctrl is None:
        raise HTTPException(status_code=503, detail="Casting not available")

    device_id = body.get("device_id")
    service_id = body.get("service_id")
    if not device_id or not service_id:
        raise HTTPException(status_code=400, detail="device_id and service_id required")

    device = _discovery.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found. Run a device scan first.")

    if device.device_type != "airplay":
        raise HTTPException(status_code=400, detail="Device is not an AirPlay device")

    station = await _registry.get_station(service_id)
    if station is None:
        raise HTTPException(status_code=404, detail="Station not found")

    station_name = station.get("name", "DAB+ Radio")

    # Ensure welle-cli is tuned to the right channel
    channel = station.get("channel")
    if channel and channel != _welle.current_channel:
        success = await _welle.tune(channel)
        if not success:
            raise HTTPException(status_code=500, detail="Failed to tune to channel")

    # Update cast controller's server host
    host = _get_server_host(request)
    _cast_ctrl._server_host = host

    await _activity_log.add("info", f"Casting '{station_name}' to AirPlay '{device.name}'")

    try:
        session = await _cast_ctrl.cast_to_airplay(device, service_id, station_name)
        return {"status": "playing", "session": session.to_dict()}
    except RuntimeError as exc:
        await _activity_log.add("error", f"AirPlay cast failed: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/cast/stop")
async def stop_cast(body: dict | None = None) -> dict:
    """Stop casting to a specific device or all devices.

    Body: {"device_id": "..."} — stops a specific device.
    No body or empty body — stops all casting sessions.
    """
    if _cast_ctrl is None:
        raise HTTPException(status_code=503, detail="Casting not available")

    device_id = body.get("device_id") if body else None

    if device_id:
        await _cast_ctrl.stop_cast(device_id)
        await _activity_log.add("info", f"Stopped casting to device {device_id}")
    else:
        await _cast_ctrl.stop_all()
        await _activity_log.add("info", "Stopped all casting sessions")

    return {"status": "stopped"}


@router.get("/cast/status")
async def get_cast_status() -> dict:
    """Return status of all active casting sessions."""
    if _cast_ctrl is None:
        return {"sessions": []}

    return {"sessions": _cast_ctrl.active_sessions}


@router.post("/cast/volume")
async def set_cast_volume(body: dict) -> dict:
    """Set volume on a casting device.

    Body: {"device_id": "...", "volume": 0.0-1.0}
    """
    if _cast_ctrl is None:
        raise HTTPException(status_code=503, detail="Casting not available")

    device_id = body.get("device_id")
    volume = body.get("volume")
    if not device_id or volume is None:
        raise HTTPException(status_code=400, detail="device_id and volume required")

    try:
        await _cast_ctrl.set_volume(device_id, float(volume))
        return {"status": "ok", "volume": float(volume)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/cast/pause")
async def pause_cast(body: dict) -> dict:
    """Pause playback on a casting device.

    Body: {"device_id": "..."}
    """
    if _cast_ctrl is None:
        raise HTTPException(status_code=503, detail="Casting not available")

    device_id = body.get("device_id")
    if not device_id:
        raise HTTPException(status_code=400, detail="device_id required")

    await _cast_ctrl.pause(device_id)
    return {"status": "paused"}


@router.post("/cast/resume")
async def resume_cast(body: dict) -> dict:
    """Resume playback on a casting device.

    Body: {"device_id": "..."}
    """
    if _cast_ctrl is None:
        raise HTTPException(status_code=503, detail="Casting not available")

    device_id = body.get("device_id")
    if not device_id:
        raise HTTPException(status_code=400, detail="device_id required")

    await _cast_ctrl.resume(device_id)
    return {"status": "playing"}


@router.get("/cast/stream/{service_id}/{fmt}")
async def cast_stream(service_id: str, fmt: str) -> StreamingResponse:
    """Serve a transcoded audio stream for casting devices.

    This endpoint is called by the casting device (Chromecast/AirPlay),
    NOT by the browser. The device fetches audio directly from here.

    Supported formats:
      - mp3: Direct MP3 passthrough from welle-cli
      - aac: AAC-LC in ADTS container (FFmpeg transcoded)
      - mpegts: AAC in MPEG-TS container (FFmpeg transcoded)
    """
    if _stream_mgr is None:
        raise HTTPException(status_code=503, detail="Streaming not available")

    if fmt not in CONTENT_TYPES:
        raise HTTPException(status_code=400, detail=f"Unsupported format: {fmt}")

    content_type = CONTENT_TYPES[fmt]

    if fmt == "mp3":
        # Direct proxy from welle-cli (same as existing /api/stream endpoint)
        url = f"http://localhost:{WELLE_CLI_PORT}/mp3/{service_id}"

        async def generate_mp3():
            async with httpx.AsyncClient(timeout=None) as client:
                try:
                    async with client.stream("GET", url) as response:
                        async for chunk in response.aiter_bytes(chunk_size=4096):
                            yield chunk
                except httpx.HTTPError as exc:
                    logger.error("Cast stream proxy error: %s", exc)

        return StreamingResponse(
            generate_mp3(),
            media_type=content_type,
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    # For transcoded formats, start an on-demand FFmpeg session
    try:
        session = await _stream_mgr.start_stream(
            service_id=service_id,
            target_format=fmt,
            device_id=None,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    async def generate_transcoded():
        try:
            async for chunk in _stream_mgr.read_stream(session.session_id):
                yield chunk
        finally:
            await _stream_mgr.stop_stream(session.session_id)

    return StreamingResponse(
        generate_transcoded(),
        media_type=content_type,
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )
