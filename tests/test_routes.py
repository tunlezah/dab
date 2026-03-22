import pytest
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock
from fastapi import FastAPI
from fastapi.testclient import TestClient
from server import routes


def _create_test_app():
    """Create a fresh FastAPI app with just the API router (no lifespan)."""
    test_app = FastAPI()
    test_app.include_router(routes.router)
    return test_app


@pytest.fixture
def client():
    """Set up mock dependencies and create a test client."""
    mock_welle = MagicMock()
    mock_welle.running = True
    mock_welle.current_channel = "9A"
    mock_welle.is_healthy = AsyncMock(return_value=True)
    mock_welle.get_mux_json = AsyncMock(return_value={
        "ensemble": {"label": "ABC NSW DAB"},
        "demodulator": {"snr": 15.2},
        "services": [],
    })
    mock_welle.tune = AsyncMock(return_value=True)
    mock_welle.get_stream_url = AsyncMock(
        return_value="http://localhost:7979/mp3/0x1301"
    )

    mock_scanner = MagicMock()
    mock_scanner.scanning = False
    mock_scanner.progress = {
        "scanning": False,
        "current_channel": None,
        "channels_scanned": 0,
        "channels_total": 0,
        "stations_found": 0,
        "progress_percent": 0.0,
    }
    mock_scanner.scan_all = AsyncMock()
    mock_scanner.scan_popular = AsyncMock()

    mock_registry = MagicMock()
    mock_registry.get_all = AsyncMock(return_value=[])
    mock_registry.get_station = AsyncMock(return_value=None)

    mock_audio = MagicMock()
    mock_audio.current_service_id = None
    mock_audio._current_service_id = None
    mock_audio.output_mode = "browser"
    mock_audio.set_output_mode = AsyncMock()
    mock_audio.play_server = AsyncMock(return_value=True)
    mock_audio.stop_server = AsyncMock()

    routes.setup(mock_welle, mock_scanner, mock_registry, mock_audio)

    test_app = _create_test_app()
    with TestClient(test_app, raise_server_exceptions=False) as tc:
        tc.mock_welle = mock_welle
        tc.mock_scanner = mock_scanner
        tc.mock_registry = mock_registry
        tc.mock_audio = mock_audio
        yield tc


def test_get_status(client):
    response = client.get("/api/status")
    assert response.status_code == 200
    data = response.json()
    assert "sdr_connected" in data
    assert "welle_running" in data
    assert "current_channel" in data
    assert "scanning" in data
    assert "playing" in data
    assert "output_mode" in data
    assert "signal_snr" in data
    assert data["welle_running"] is True
    assert data["current_channel"] == "9A"
    assert data["scanning"] is False


def test_get_stations_empty(client):
    response = client.get("/api/stations")
    assert response.status_code == 200
    data = response.json()
    assert "stations" in data
    assert data["stations"] == []


def test_get_stations_with_data(client):
    stations_data = [
        {"id": "0x1301", "name": "triple j", "channel": "9A"},
        {"id": "0x1302", "name": "Double J", "channel": "9A"},
    ]
    client.mock_registry.get_all = AsyncMock(return_value=stations_data)

    response = client.get("/api/stations")
    assert response.status_code == 200
    data = response.json()
    assert len(data["stations"]) == 2
    names = {s["name"] for s in data["stations"]}
    assert "triple j" in names
    assert "Double J" in names


def test_start_scan(client):
    response = client.post("/api/scan")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "scanning"


def test_start_scan_already_scanning(client):
    client.mock_scanner.scanning = True
    response = client.post("/api/scan")
    assert response.status_code == 409


def test_scan_progress(client):
    client.mock_scanner.progress = {
        "scanning": True,
        "current_channel": "9A",
        "channels_scanned": 3,
        "channels_total": 10,
        "stations_found": 5,
        "progress_percent": 30.0,
    }

    response = client.get("/api/scan/progress")
    assert response.status_code == 200
    data = response.json()
    assert data["scanning"] is True
    assert data["current_channel"] == "9A"
    assert data["channels_scanned"] == 3
    assert data["stations_found"] == 5
    assert data["progress_percent"] == 30.0


def test_play_station_not_found(client):
    client.mock_registry.get_station = AsyncMock(return_value=None)
    response = client.post("/api/play/0xFFFF")
    assert response.status_code == 404


def test_stop_playback(client):
    response = client.delete("/api/play")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "stopped"


def test_get_metadata_nothing_playing(client):
    client.mock_audio.current_service_id = None
    response = client.get("/api/metadata")
    assert response.status_code == 200
    data = response.json()
    assert data["station_name"] is None
    assert data["ensemble"] is None
    assert data["dls"] is None
    assert data["snr"] is None
    assert data["mot_image"] is None
