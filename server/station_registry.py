"""In-memory station storage for discovered DAB+ services, with JSON persistence."""

import asyncio
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class StationRegistry:
    """Thread-safe in-memory store of discovered DAB+ stations.

    Optionally backed by a JSON file so stations survive restarts.
    """

    def __init__(self, persist_path: Path | None = None) -> None:
        self._stations: dict[str, dict] = {}
        self._lock: asyncio.Lock = asyncio.Lock()
        self._persist_path = persist_path

    async def load(self) -> int:
        """Load stations from the JSON file. Returns count loaded."""
        if not self._persist_path or not self._persist_path.exists():
            return 0

        try:
            raw = self._persist_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if not isinstance(data, list):
                logger.warning("stations.json has unexpected format, ignoring")
                return 0

            async with self._lock:
                for station in data:
                    sid = station.get("id")
                    if sid:
                        self._stations[sid] = station

            count = len(self._stations)
            logger.info("Loaded %d stations from %s", count, self._persist_path)
            return count
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load stations file: %s", exc)
            return 0

    async def save(self) -> None:
        """Save current stations to the JSON file."""
        if not self._persist_path:
            return

        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            async with self._lock:
                data = list(self._stations.values())
            self._persist_path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            logger.debug("Saved %d stations to %s", len(data), self._persist_path)
        except OSError as exc:
            logger.warning("Failed to save stations file: %s", exc)

    async def add_station(self, station: dict) -> None:
        """Add or update a station entry.

        Expected station dict keys:
            id, name, ensemble, channel, bitrate, mode, dls

        Duplicate SID handling: if the same service ID is found on a
        different channel, the first occurrence is kept (it was scanned
        successfully) and the new channel is recorded as an alternate.
        """
        service_id = station.get("id")
        if not service_id:
            logger.warning("Attempted to add station with no id: %s", station)
            return

        async with self._lock:
            if service_id in self._stations:
                existing = self._stations[service_id]
                existing_channel = existing.get("channel")
                new_channel = station.get("channel")

                if existing_channel and new_channel and existing_channel != new_channel:
                    # Duplicate SID on a different channel — record alternate
                    alternates = existing.setdefault("alternate_channels", [])
                    if new_channel not in alternates:
                        alternates.append(new_channel)
                    logger.info(
                        "Service %s (SID %s) found on %s, already registered from %s",
                        station.get("name"), service_id, new_channel, existing_channel,
                    )
                else:
                    # Same channel — update in place (e.g. label resolved)
                    existing.update(station)
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
