import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from server.scanner import Scanner, STATUS_FOUND, STATUS_EMPTY, STATUS_RETRY_SUCCESS
from server.station_registry import StationRegistry


@pytest.fixture
def mock_welle_manager():
    welle = MagicMock()
    welle.tune = AsyncMock(return_value=True)
    welle.get_mux_json = AsyncMock(return_value=None)
    welle.is_healthy = AsyncMock(return_value=True)
    return welle


@pytest.fixture
def registry():
    return StationRegistry()


@pytest.fixture
def scanner(mock_welle_manager, registry):
    return Scanner(mock_welle_manager, registry)


def _patch_dwell(min_dwell=0.0, max_dwell=0.1, poll_interval=0.05):
    """Helper to patch dwell times for fast tests."""
    return [
        patch("server.scanner.MIN_DWELL_TIME", min_dwell),
        patch("server.scanner.MAX_DWELL_TIME", max_dwell),
        patch("server.scanner.DWELL_POLL_INTERVAL", poll_interval),
    ]


@pytest.mark.asyncio
async def test_scan_finds_stations(mock_welle_manager, registry, sample_mux_data):
    mock_welle_manager.get_mux_json = AsyncMock(return_value=sample_mux_data)
    scanner = Scanner(mock_welle_manager, registry)

    with patch("server.scanner.BAND_III_CHANNELS", [("9A", 202.928)]):
        patches = _patch_dwell()
        for p in patches:
            p.start()
        try:
            stations = await scanner.scan_all()
        finally:
            for p in patches:
                p.stop()

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
        patches = _patch_dwell()
        for p in patches:
            p.start()
        try:
            stations = await scanner.scan_all()
        finally:
            for p in patches:
                p.stop()

    assert len(stations) == 0
    assert registry.station_count == 0


@pytest.mark.asyncio
async def test_scan_handles_tune_failure(mock_welle_manager, registry):
    mock_welle_manager.tune = AsyncMock(return_value=False)
    scanner = Scanner(mock_welle_manager, registry)

    with patch("server.scanner.BAND_III_CHANNELS", [("9A", 202.928)]):
        patches = _patch_dwell()
        for p in patches:
            p.start()
        try:
            stations = await scanner.scan_all()
        finally:
            for p in patches:
                p.stop()

    assert len(stations) == 0
    assert registry.station_count == 0
    # Called twice: once on first pass, once on retry
    assert mock_welle_manager.tune.call_count == 2
    mock_welle_manager.tune.assert_called_with("9A")


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
                        "transportmode": "Audio",
                        "ascty": "DAB+",
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
    assert station["type"] == "audio"


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
                "components": [{"transportmode": "Audio", "subchannel": {"bitrate": 128}}],
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
        patches = _patch_dwell(min_dwell=0.0, max_dwell=0.05, poll_interval=0.02)
        for p in patches:
            p.start()
        try:
            scan_task = asyncio.create_task(scanner.scan_all())
            await asyncio.sleep(0.05)
            assert scanner.scanning is True
            await scanner.cancel()
            stations = await scan_task
        finally:
            for p in patches:
                p.stop()

    # Scanner should have stopped before completing all channels
    assert scanner.scanning is False
    assert scanner.progress["channels_scanned"] < len(many_channels)


@pytest.mark.asyncio
async def test_scan_report(mock_welle_manager, registry, sample_mux_data):
    """Test that scan_report tracks per-channel status."""
    mock_welle_manager.get_mux_json = AsyncMock(return_value=sample_mux_data)
    scanner = Scanner(mock_welle_manager, registry)

    with patch("server.scanner.BAND_III_CHANNELS", [("9A", 202.928)]):
        patches = _patch_dwell()
        for p in patches:
            p.start()
        try:
            await scanner.scan_all()
        finally:
            for p in patches:
                p.stop()

    report = scanner.scan_report
    assert "9A" in report
    assert report["9A"]["status"] == STATUS_FOUND
    assert report["9A"]["stations"] == 2
    assert report["9A"]["attempts"] >= 1


@pytest.mark.asyncio
async def test_retry_logic(mock_welle_manager, registry, sample_mux_data):
    """Test that channels with no services on first pass are retried."""
    call_count = 0

    async def mux_with_retry(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        # Return None on first few calls (first pass), data on retry
        if call_count <= 3:
            return {"ensemble": {"label": "Test"}, "services": [], "demodulator": {"snr": 5.0}}
        return sample_mux_data

    mock_welle_manager.get_mux_json = AsyncMock(side_effect=mux_with_retry)
    scanner = Scanner(mock_welle_manager, registry)

    with patch("server.scanner.BAND_III_CHANNELS", [("9A", 202.928)]):
        patches = _patch_dwell()
        for p in patches:
            p.start()
        try:
            stations = await scanner.scan_all()
        finally:
            for p in patches:
                p.stop()

    report = scanner.scan_report
    assert "9A" in report
    assert report["9A"]["attempts"] == 2


def test_deferred_label_resolution(scanner):
    """Test that services with valid SID but no name get a placeholder."""
    mux = {
        "ensemble": {"label": "Test Ensemble"},
        "services": [
            {
                "sid": "0x1301",
                "label": "",
                "components": [{"transportmode": "Audio", "subchannel": {"bitrate": 128}}],
            }
        ],
    }

    stations = scanner._parse_services(mux, "9A")
    assert len(stations) == 1
    assert stations[0]["name"].startswith("[SID:")


def test_data_only_service_filtered(scanner):
    """Test that data-only services are filtered out by default."""
    mux = {
        "ensemble": {"label": "Test Ensemble"},
        "services": [
            {
                "sid": "0x1301",
                "label": "Audio Station",
                "components": [{"transportmode": "Audio", "ascty": "DAB+", "subchannel": {"bitrate": 128}}],
            },
            {
                "sid": "0x1302",
                "label": "Data Service",
                "components": [{"transportmode": "Stream", "subchannel": {"bitrate": 16}}],
            },
        ],
    }

    with patch("server.scanner.INCLUDE_DATA_SERVICES", False):
        stations = scanner._parse_services(mux, "9A")

    assert len(stations) == 1
    assert stations[0]["name"] == "Audio Station"
    assert stations[0]["type"] == "audio"


def test_data_service_included_when_configured(scanner):
    """Test that data services are included when INCLUDE_DATA_SERVICES is True."""
    mux = {
        "ensemble": {"label": "Test Ensemble"},
        "services": [
            {
                "sid": "0x1302",
                "label": "Data Service",
                "components": [{"transportmode": "Stream", "subchannel": {"bitrate": 16}}],
            },
        ],
    }

    with patch("server.scanner.INCLUDE_DATA_SERVICES", True):
        stations = scanner._parse_services(mux, "9A")

    assert len(stations) == 1
    assert stations[0]["type"] == "data"


def test_merge_stations():
    """Test that _merge_stations updates placeholder labels."""
    existing = [
        {"id": "0x1301", "name": "[SID:0x1301]", "channel": "9A", "bitrate": None, "dls": ""},
    ]
    new = [
        {"id": "0x1301", "name": "triple j", "channel": "9A", "bitrate": 128, "dls": "Now Playing"},
        {"id": "0x1302", "name": "Double J", "channel": "9A", "bitrate": 80, "dls": ""},
    ]

    Scanner._merge_stations(existing, new)

    assert len(existing) == 2
    assert existing[0]["name"] == "triple j"
    assert existing[0]["bitrate"] == 128
    assert existing[1]["name"] == "Double J"
