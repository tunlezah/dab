import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from server.scanner import Scanner
from server.station_registry import StationRegistry


@pytest.fixture
def mock_welle_manager():
    welle = MagicMock()
    welle.tune = AsyncMock(return_value=True)
    welle.get_mux_json = AsyncMock(return_value=None)
    return welle


@pytest.fixture
def registry():
    return StationRegistry()


@pytest.fixture
def scanner(mock_welle_manager, registry):
    return Scanner(mock_welle_manager, registry)


@pytest.mark.asyncio
async def test_scan_finds_stations(mock_welle_manager, registry, sample_mux_data):
    mock_welle_manager.get_mux_json = AsyncMock(return_value=sample_mux_data)
    scanner = Scanner(mock_welle_manager, registry)

    with patch("server.scanner.BAND_III_CHANNELS", [("9A", 202.928)]):
        with patch("server.scanner.SCAN_DWELL_TIME", 0):
            stations = await scanner.scan_all()

    assert len(stations) == 2
    names = {s["name"] for s in stations}
    assert "triple j" in names
    assert "Double J" in names
    assert registry.station_count == 2


@pytest.mark.asyncio
async def test_scan_progress(scanner):
    progress = scanner.progress
    assert progress["scanning"] is False
    assert progress["current_channel"] is None
    assert progress["channels_scanned"] == 0
    assert progress["channels_total"] == 0
    assert progress["stations_found"] == 0
    assert "progress_percent" in progress


@pytest.mark.asyncio
async def test_scan_empty_channel(mock_welle_manager, registry):
    empty_mux = {
        "ensemble": {"label": "Empty Mux", "id": "0x0000"},
        "services": [],
        "demodulator": {"snr": 5.0},
    }
    mock_welle_manager.get_mux_json = AsyncMock(return_value=empty_mux)
    scanner = Scanner(mock_welle_manager, registry)

    with patch("server.scanner.BAND_III_CHANNELS", [("5A", 174.928)]):
        with patch("server.scanner.SCAN_DWELL_TIME", 0):
            stations = await scanner.scan_all()

    assert len(stations) == 0
    assert registry.station_count == 0


@pytest.mark.asyncio
async def test_scan_handles_tune_failure(mock_welle_manager, registry):
    mock_welle_manager.tune = AsyncMock(return_value=False)
    scanner = Scanner(mock_welle_manager, registry)

    with patch("server.scanner.BAND_III_CHANNELS", [("9A", 202.928)]):
        with patch("server.scanner.SCAN_DWELL_TIME", 0):
            stations = await scanner.scan_all()

    assert len(stations) == 0
    assert registry.station_count == 0
    # tune was called but get_mux_json should not have been called
    mock_welle_manager.tune.assert_called_once_with("9A")
    mock_welle_manager.get_mux_json.assert_not_called()


def test_parse_services(scanner):
    sample_mux = {
        "ensemble": {"label": "ABC NSW DAB", "id": "0x1001"},
        "services": [
            {
                "sid": "0x1301",
                "label": "triple j",
                "dls_label": "Now Playing: Song",
                "mode": "DAB+",
                "components": [
                    {
                        "subchannel": {"bitrate": 128},
                    }
                ],
            }
        ],
        "demodulator": {"snr": 15.2},
    }

    stations = scanner._parse_services(sample_mux, "9A")

    assert len(stations) == 1
    station = stations[0]
    assert station["id"] == "0x1301"
    assert station["name"] == "triple j"
    assert station["ensemble"] == "ABC NSW DAB"
    assert station["channel"] == "9A"
    assert station["bitrate"] == 128
    assert station["mode"] == "DAB+"
    assert station["dls"] == "Now Playing: Song"


def test_parse_services_dict_labels(scanner):
    """Test parsing when welle-cli returns labels as nested dicts (real format)."""
    mux = {
        "ensemble": {
            "label": {
                "label": "ABC NSW DAB",
                "shortlabel": "ABC",
                "fig2label": "",
                "fig2charset": "Undefined",
                "fig2rfu": False,
            },
            "id": "0x1001",
            "ecc": "0x00",
        },
        "services": [
            {
                "sid": "0x1301",
                "label": {
                    "label": "triple j",
                    "shortlabel": "trplj",
                    "fig2label": "",
                    "fig2charset": "Undefined",
                    "fig2rfu": False,
                },
                "dls_label": "Now Playing: Song",
                "mode": "DAB+",
                "components": [{"subchannel": {"bitrate": 128}}],
            }
        ],
        "demodulator": {"snr": 15.2},
    }

    stations = scanner._parse_services(mux, "9A")

    assert len(stations) == 1
    assert stations[0]["name"] == "triple j"
    assert stations[0]["ensemble"] == "ABC NSW DAB"
    assert stations[0]["bitrate"] == 128


@pytest.mark.asyncio
async def test_cancel_scan(mock_welle_manager, registry, sample_mux_data):
    call_count = 0

    async def slow_tune(channel):
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.1)
        return True

    mock_welle_manager.tune = slow_tune
    mock_welle_manager.get_mux_json = AsyncMock(return_value=sample_mux_data)
    scanner = Scanner(mock_welle_manager, registry)

    many_channels = [
        ("5A", 174.928), ("5B", 176.640), ("5C", 178.352), ("5D", 180.064),
        ("6A", 181.936), ("6B", 183.648), ("6C", 185.360), ("6D", 187.072),
    ]

    with patch("server.scanner.BAND_III_CHANNELS", many_channels):
        with patch("server.scanner.SCAN_DWELL_TIME", 0):
            scan_task = asyncio.create_task(scanner.scan_all())
            await asyncio.sleep(0.05)
            assert scanner.scanning is True
            await scanner.cancel()
            stations = await scan_task

    # Scanner should have stopped before completing all channels
    assert scanner.scanning is False
    assert scanner.progress["channels_scanned"] < len(many_channels)
