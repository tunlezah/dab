import pytest
import asyncio
from server.station_registry import StationRegistry


@pytest.fixture
def registry():
    return StationRegistry()


@pytest.mark.asyncio
async def test_add_and_get_station(registry):
    station = {
        "id": "0x1301",
        "name": "triple j",
        "ensemble": "ABC NSW DAB",
        "channel": "9A",
        "bitrate": 128,
        "mode": "DAB+",
        "dls": "Now Playing: Song",
    }
    await registry.add_station(station)
    result = await registry.get_station("0x1301")
    assert result is not None
    assert result["id"] == "0x1301"
    assert result["name"] == "triple j"
    assert result["channel"] == "9A"
    assert result["bitrate"] == 128


@pytest.mark.asyncio
async def test_get_all_stations(registry):
    station1 = {"id": "0x1301", "name": "triple j", "channel": "9A"}
    station2 = {"id": "0x1302", "name": "Double J", "channel": "9A"}
    station3 = {"id": "0x1401", "name": "SBS Chill", "channel": "9B"}

    await registry.add_station(station1)
    await registry.add_station(station2)
    await registry.add_station(station3)

    all_stations = await registry.get_all()
    assert len(all_stations) == 3

    names = {s["name"] for s in all_stations}
    assert names == {"triple j", "Double J", "SBS Chill"}


@pytest.mark.asyncio
async def test_update_existing_station(registry):
    station_v1 = {"id": "0x1301", "name": "triple j", "dls": "Old DLS"}
    station_v2 = {"id": "0x1301", "name": "triple j", "dls": "New DLS"}

    await registry.add_station(station_v1)
    await registry.add_station(station_v2)

    result = await registry.get_station("0x1301")
    assert result["dls"] == "New DLS"
    assert registry.station_count == 1


@pytest.mark.asyncio
async def test_update_dls(registry):
    station = {"id": "0x1301", "name": "triple j", "dls": "Old DLS"}
    await registry.add_station(station)

    await registry.update_dls("0x1301", "New Song - New Artist")

    result = await registry.get_station("0x1301")
    assert result["dls"] == "New Song - New Artist"


@pytest.mark.asyncio
async def test_get_nonexistent_station(registry):
    result = await registry.get_station("0xFFFF")
    assert result is None


@pytest.mark.asyncio
async def test_clear(registry):
    await registry.add_station({"id": "0x1301", "name": "triple j"})
    await registry.add_station({"id": "0x1302", "name": "Double J"})
    assert registry.station_count == 2

    await registry.clear()
    assert registry.station_count == 0

    all_stations = await registry.get_all()
    assert all_stations == []


@pytest.mark.asyncio
async def test_station_count(registry):
    assert registry.station_count == 0

    await registry.add_station({"id": "0x1301", "name": "triple j"})
    assert registry.station_count == 1

    await registry.add_station({"id": "0x1302", "name": "Double J"})
    assert registry.station_count == 2

    # Adding same station again should not increase count
    await registry.add_station({"id": "0x1301", "name": "triple j"})
    assert registry.station_count == 2
