"""Band III scanning logic for DAB+ stations."""

import asyncio
import logging
from typing import TYPE_CHECKING

from .config import BAND_III_CHANNELS, POPULAR_CHANNELS, SCAN_DWELL_TIME

if TYPE_CHECKING:
    from .station_registry import StationRegistry
    from .welle_manager import WelleManager

logger = logging.getLogger(__name__)


class Scanner:
    """Scans Band III channels to discover DAB+ services."""

    def __init__(
        self,
        welle_manager: "WelleManager",
        station_registry: "StationRegistry",
    ) -> None:
        self._welle = welle_manager
        self._registry = station_registry
        self._scanning: bool = False
        self._cancelled: bool = False
        self._current_channel: str | None = None
        self._channels_scanned: int = 0
        self._channels_total: int = 0
        self._stations_found: int = 0

    @property
    def scanning(self) -> bool:
        return self._scanning

    @property
    def progress(self) -> dict:
        total = self._channels_total or 1
        return {
            "scanning": self._scanning,
            "current_channel": self._current_channel,
            "channels_scanned": self._channels_scanned,
            "channels_total": self._channels_total,
            "stations_found": self._stations_found,
            "progress_percent": round(self._channels_scanned / total * 100, 1),
        }

    async def scan_all(self) -> list:
        """Full Band III sweep across all channels."""
        channels = [label for label, _ in BAND_III_CHANNELS]
        return await self._run_scan(channels)

    async def scan_popular(self) -> list:
        """Scan only the popular/quick channels."""
        return await self._run_scan(POPULAR_CHANNELS)

    async def cancel(self) -> None:
        """Cancel an in-progress scan."""
        if self._scanning:
            logger.info("Scan cancellation requested")
            self._cancelled = True

    async def _run_scan(self, channels: list[str]) -> list:
        """Execute a scan over the given list of channel labels."""
        if self._scanning:
            logger.warning("Scan already in progress")
            return []

        self._scanning = True
        self._cancelled = False
        self._channels_scanned = 0
        self._channels_total = len(channels)
        self._stations_found = 0
        all_stations: list[dict] = []

        logger.info("Starting scan of %d channels", len(channels))

        try:
            for channel in channels:
                if self._cancelled:
                    logger.info("Scan cancelled after %d channels", self._channels_scanned)
                    break

                self._current_channel = channel
                found = await self._scan_channel(channel)
                all_stations.extend(found)
                self._stations_found += len(found)
                self._channels_scanned += 1
                logger.info(
                    "Channel %s: found %d stations (%d/%d)",
                    channel,
                    len(found),
                    self._channels_scanned,
                    self._channels_total,
                )
        finally:
            self._scanning = False
            self._current_channel = None

        logger.info(
            "Scan complete: %d stations found across %d channels",
            len(all_stations),
            self._channels_scanned,
        )
        return all_stations

    async def _scan_channel(self, channel: str) -> list[dict]:
        """Tune to a channel, wait for lock, then extract discovered services."""
        tuned = await self._welle.tune(channel)
        if not tuned:
            logger.warning("Failed to tune to channel %s", channel)
            return []

        await asyncio.sleep(SCAN_DWELL_TIME)

        mux_data = await self._welle.get_mux_json()
        if mux_data is None:
            logger.debug("No mux data for channel %s", channel)
            return []

        stations = self._parse_services(mux_data, channel)
        for station in stations:
            await self._registry.add_station(station)

        return stations

    def _parse_services(self, mux_data: dict, channel: str) -> list[dict]:
        """Extract station information from a welle-cli mux.json response."""
        stations: list[dict] = []
        ensemble_label = ""

        ensemble = mux_data.get("ensemble")
        if isinstance(ensemble, dict):
            ensemble_label = ensemble.get("label", "").strip()

        services = mux_data.get("services")
        if not isinstance(services, list):
            return stations

        for svc in services:
            if not isinstance(svc, dict):
                continue

            sid = svc.get("sid")
            name = svc.get("label", "").strip()
            if not sid or not name:
                continue

            # Extract bitrate from the first component's subchannel if available
            bitrate: int | None = None
            components = svc.get("components")
            if isinstance(components, list):
                for comp in components:
                    if not isinstance(comp, dict):
                        continue
                    subchannel = comp.get("subchannel")
                    if isinstance(subchannel, dict):
                        br = subchannel.get("bitrate")
                        if br is not None:
                            bitrate = int(br)
                            break

            station = {
                "id": str(sid),
                "name": name,
                "ensemble": ensemble_label,
                "channel": channel,
                "bitrate": bitrate,
                "mode": svc.get("mode", ""),
                "dls": svc.get("dls_label", ""),
            }
            stations.append(station)

        return stations
