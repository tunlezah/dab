"""In-memory station storage for discovered DAB+ services."""

import asyncio
import logging

logger = logging.getLogger(__name__)


class StationRegistry:
    """Thread-safe in-memory store of discovered DAB+ stations."""

    def __init__(self) -> None:
        self._stations: dict[str, dict] = {}
        self._lock: asyncio.Lock = asyncio.Lock()

    async def add_station(self, station: dict) -> None:
        """Add or update a station entry.

        Expected station dict keys:
            id, name, ensemble, channel, bitrate, mode, dls
        """
        service_id = station.get("id")
        if not service_id:
            logger.warning("Attempted to add station with no id: %s", station)
            return

        async with self._lock:
            if service_id in self._stations:
                self._stations[service_id].update(station)
                logger.debug("Updated station %s (%s)", service_id, station.get("name"))
            else:
                self._stations[service_id] = dict(station)
                logger.info("Added station %s: %s", service_id, station.get("name"))

    async def get_all(self) -> list[dict]:
        """Return all stations as a list of dicts."""
        async with self._lock:
            return list(self._stations.values())

    async def get_station(self, service_id: str) -> dict | None:
        """Return a single station by service ID, or None if not found."""
        async with self._lock:
            return self._stations.get(service_id)

    async def update_dls(self, service_id: str, dls: str) -> None:
        """Update the DLS (Dynamic Label Segment) text for a station."""
        async with self._lock:
            if service_id in self._stations:
                self._stations[service_id]["dls"] = dls
            else:
                logger.debug(
                    "Cannot update DLS for unknown station %s", service_id
                )

    async def clear(self) -> None:
        """Remove all stations from the registry."""
        async with self._lock:
            count = len(self._stations)
            self._stations.clear()
            logger.info("Cleared %d stations from registry", count)

    @property
    def station_count(self) -> int:
        """Return the number of stored stations."""
        return len(self._stations)
