"""FastAPI API route definitions for DAB+ radio web application."""

import asyncio
import logging

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from .config import APP_VERSION, SDR_AGC, SDR_GAIN, SDR_PPM, WELLE_CLI_PORT
from .welle_manager import WelleManager
from .scanner import Scanner
from .station_registry import StationRegistry
from .audio_manager import AudioManager
from .activity_log import ActivityLog

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


def setup(welle: WelleManager, scanner: Scanner, registry: StationRegistry, audio: AudioManager, activity_log: ActivityLog) -> None:
    """Inject dependencies into the routes module."""
    global _welle, _scanner, _registry, _audio, _activity_log
    _welle, _scanner, _registry, _audio, _activity_log = welle, scanner, registry, audio, activity_log


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


@router.get("/scan/report")
async def get_scan_report() -> dict:
    """Return per-channel scan status from the last scan."""
    return {"report": _scanner.scan_report}


@router.get("/sdr/config")
async def get_sdr_config() -> dict:
    """Return current SDR gain/AGC/PPM settings."""
    return {
        "gain": SDR_GAIN,
        "agc": SDR_AGC,
        "ppm": SDR_PPM,
    }


@router.post("/sdr/config")
async def set_sdr_config(body: dict) -> dict:
    """Update SDR settings and restart welle-cli to apply them."""
    import server.config as cfg

    if "gain" in body:
        val = body["gain"]
        cfg.SDR_GAIN = int(val) if val is not None else None
    if "agc" in body:
        cfg.SDR_AGC = bool(body["agc"])
    if "ppm" in body:
        cfg.SDR_PPM = int(body["ppm"])

    # Restart welle-cli so the new flags take effect
    restarted = await _welle.restart()
    return {
        "gain": cfg.SDR_GAIN,
        "agc": cfg.SDR_AGC,
        "ppm": cfg.SDR_PPM,
        "welle_restarted": restarted,
    }


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
