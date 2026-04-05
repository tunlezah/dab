"""Band III scanning logic for DAB+ stations."""

import asyncio
import logging
from typing import TYPE_CHECKING

from .config import BAND_III_CHANNELS, POPULAR_CHANNELS, SCAN_DWELL_TIME

if TYPE_CHECKING:
    from .activity_log import ActivityLog
    from .station_registry import StationRegistry
    from .welle_manager import WelleManager

logger = logging.getLogger(__name__)


class Scanner:
    """Scans Band III channels to discover DAB+ services."""

    def __init__(
        self,
        welle_manager: "WelleManager",
        station_registry: "StationRegistry",
        activity_log: "ActivityLog | None" = None,
    ) -> None:
        self._welle = welle_manager
        self._registry = station_registry
        self._activity_log = activity_log
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

    async def _log(self, level: str, message: str) -> None:
        """Log to both Python logger and activity log."""
        log_level = "warning" if level == "warn" else level
        getattr(logger, log_level, logger.info)(message)
        if self._activity_log:
            await self._activity_log.add(level, message)

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
        empty_channels: list[str] = []

        await self._log("info", f"Starting scan of {len(channels)} channels")

        try:
            for channel in channels:
                if self._cancelled:
                    await self._log("info", f"Scan cancelled after {self._channels_scanned} channels")
                    break

                self._current_channel = channel
                try:
                    found = await self._scan_channel(channel)
                except Exception as exc:
                    await self._log("error", f"Channel {channel}: scan error — {exc}")
                    found = []
                all_stations.extend(found)
                self._stations_found += len(found)
                self._channels_scanned += 1

                if found:
                    names = ", ".join(s["name"] for s in found)
                    await self._log("info", f"Channel {channel}: found {len(found)} stations ({names})")
                else:
                    empty_channels.append(channel)
                    await self._log("info", f"Channel {channel}: no stations")

            # Retry channels that found nothing — transient interference or
            # slow FIC decode may have caused a miss on the first attempt.
            if empty_channels and not self._cancelled:
                await self._log("info", f"Retrying {len(empty_channels)} empty channels")
                for channel in empty_channels:
                    if self._cancelled:
                        break

                    self._current_channel = channel
                    try:
                        found = await self._scan_channel(channel)
                    except Exception as exc:
                        await self._log("error", f"Channel {channel} retry: scan error — {exc}")
                        found = []

                    if found:
                        all_stations.extend(found)
                        self._stations_found += len(found)
                        names = ", ".join(s["name"] for s in found)
                        await self._log("info", f"Channel {channel} retry: found {len(found)} stations ({names})")
        finally:
            self._scanning = False
            self._current_channel = None

        await self._log(
            "info",
            f"Scan complete: {len(all_stations)} stations found across {self._channels_scanned} channels",
        )

        # Persist stations to disk
        await self._registry.save()

        return all_stations

    async def _scan_channel(self, channel: str) -> list[dict]:
        """Tune to a channel, poll mux.json during dwell, keep best result."""
        tuned = await self._welle.tune(channel)
        if not tuned:
            await self._log("warn", f"Failed to tune to channel {channel}")
            return []

        # Poll mux.json every 2 seconds during the dwell period.
        # FIC decoding is progressive — later polls may find more services.
        poll_interval = min(2.0, SCAN_DWELL_TIME) if SCAN_DWELL_TIME > 0 else 0
        elapsed = 0.0
        best_stations: list[dict] = []

        while elapsed < SCAN_DWELL_TIME or (SCAN_DWELL_TIME == 0 and elapsed == 0):
            if poll_interval > 0:
                await asyncio.sleep(poll_interval)
            elapsed += max(poll_interval, 1)

            mux_data = await self._welle.get_mux_json()
            if mux_data is None:
                continue

            stations = self._parse_services(mux_data, channel)
            if len(stations) > len(best_stations):
                best_stations = stations

        if not best_stations:
            return []

        for station in best_stations:
            await self._registry.add_station(station)

        return best_stations

    @staticmethod
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

    def _parse_services(self, mux_data: dict, channel: str) -> list[dict]:
        """Extract station information from a welle-cli mux.json response."""
        stations: list[dict] = []
        ensemble_label = ""

        ensemble = mux_data.get("ensemble")
        if isinstance(ensemble, dict):
            ensemble_label = self._extract_label(ensemble.get("label", ""))

        services = mux_data.get("services")
        if not isinstance(services, list):
            return stations

        for svc in services:
            if not isinstance(svc, dict):
                continue

            sid = svc.get("sid")
            name = self._extract_label(svc.get("label", ""))
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
