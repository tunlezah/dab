"""Band III scanning logic for DAB+ stations."""

import asyncio
import logging
from typing import TYPE_CHECKING

import httpx

from .config import (
    BAND_III_CHANNELS,
    DWELL_POLL_INTERVAL,
    INCLUDE_DATA_SERVICES,
    MAX_DWELL_TIME,
    MIN_DWELL_TIME,
    POPULAR_CHANNELS,
)

if TYPE_CHECKING:
    from .activity_log import ActivityLog
    from .station_registry import StationRegistry
    from .welle_manager import WelleManager

logger = logging.getLogger(__name__)

# Per-channel scan status values
STATUS_FOUND = "found"
STATUS_EMPTY = "empty"
STATUS_TIMEOUT = "timeout"
STATUS_ERROR = "error"
STATUS_RETRY_SUCCESS = "retry_success"
STATUS_SKIPPED = "skipped"


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
        self._scan_report: dict[str, dict] = {}

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

    @property
    def scan_report(self) -> dict[str, dict]:
        return dict(self._scan_report)

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
        self._scan_report = {}
        all_stations: list[dict] = []
        retry_queue: list[str] = []

        await self._log("info", f"Starting scan of {len(channels)} channels")

        try:
            # First pass
            for channel in channels:
                if self._cancelled:
                    await self._log("info", f"Scan cancelled after {self._channels_scanned} channels")
                    break

                self._current_channel = channel
                found, status = await self._scan_channel_safe(channel)
                self._scan_report[channel] = {"status": status, "stations": len(found), "attempts": 1}

                if status in (STATUS_EMPTY, STATUS_TIMEOUT, STATUS_ERROR, STATUS_SKIPPED):
                    retry_queue.append(channel)

                all_stations.extend(found)
                self._stations_found += len(found)
                self._channels_scanned += 1

                if found:
                    names = ", ".join(s["name"] for s in found)
                    await self._log("info", f"Channel {channel}: found {len(found)} stations ({names})")
                else:
                    await self._log("info", f"Channel {channel}: no stations")

            # Retry pass — one retry for channels that returned zero services
            if retry_queue and not self._cancelled:
                await self._log("info", f"Retrying {len(retry_queue)} channels")
                for channel in retry_queue:
                    if self._cancelled:
                        break

                    self._current_channel = channel
                    found, status = await self._scan_channel_safe(channel)
                    self._scan_report[channel]["attempts"] = 2

                    if found:
                        self._scan_report[channel]["status"] = STATUS_RETRY_SUCCESS
                        self._scan_report[channel]["stations"] = len(found)
                        all_stations.extend(found)
                        self._stations_found += len(found)
                        names = ", ".join(s["name"] for s in found)
                        await self._log("info", f"Channel {channel} retry: found {len(found)} stations ({names})")
                    else:
                        self._scan_report[channel]["status"] = status

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

    async def _scan_channel_safe(self, channel: str) -> tuple[list[dict], str]:
        """Scan a channel with structured error handling. Returns (stations, status)."""
        try:
            # Verify welle-cli is running before tuning
            if not await self._check_welle_health():
                await self._log("warn", f"Channel {channel}: welle-cli not healthy, skipping")
                return [], STATUS_SKIPPED

            found = await self._scan_channel(channel)
            status = STATUS_FOUND if found else STATUS_EMPTY
            return found, status
        except httpx.TimeoutException:
            await self._log("error", f"Channel {channel}: timeout fetching mux.json")
            return [], STATUS_TIMEOUT
        except httpx.ConnectError:
            await self._log("error", f"Channel {channel}: cannot connect to welle-cli")
            return [], STATUS_ERROR
        except (ValueError, Exception) as exc:
            if isinstance(exc, ValueError):
                await self._log("error", f"Channel {channel}: malformed mux.json — {exc}")
            else:
                await self._log("error", f"Channel {channel}: scan error — {exc}")
            return [], STATUS_ERROR

    async def _check_welle_health(self) -> bool:
        """Verify welle-cli is running. Wait up to 5s for restart if down."""
        if await self._welle.is_healthy():
            return True

        # Wait for auto-restart
        for _ in range(5):
            await asyncio.sleep(1.0)
            if await self._welle.is_healthy():
                return True

        return False

    async def _scan_channel(self, channel: str) -> list[dict]:
        """Tune to a channel and use adaptive dwell to extract discovered services."""
        tuned = await self._welle.tune(channel)
        if not tuned:
            await self._log("warn", f"Failed to tune to channel {channel}")
            return []

        # Adaptive dwell: poll mux.json at intervals, exit early when stable
        elapsed = 0.0
        prev_service_count: int | None = None
        best_stations: list[dict] = []
        ensemble_detected = False

        while elapsed < MAX_DWELL_TIME:
            await asyncio.sleep(DWELL_POLL_INTERVAL)
            elapsed += DWELL_POLL_INTERVAL

            mux_data = await self._welle.get_mux_json()
            if mux_data is None:
                continue

            # Check for ensemble without services (possible incomplete FIC decode)
            ensemble = mux_data.get("ensemble")
            if isinstance(ensemble, dict) and self._extract_label(ensemble.get("label", "")):
                ensemble_detected = True

            # Log SNR if available
            demod = mux_data.get("demodulator")
            if demod and isinstance(demod, dict):
                snr = demod.get("snr")
                if snr is not None and elapsed <= DWELL_POLL_INTERVAL:
                    logger.debug("Channel %s SNR: %.1f dB", channel, snr)

            stations = self._parse_services(mux_data, channel)
            current_count = len(stations)

            # Update best stations — merge labels from later polls
            if current_count > 0:
                self._merge_stations(best_stations, stations)

            # Check stabilisation: after min dwell, two consecutive polls
            # with same service count means we're done
            if elapsed >= MIN_DWELL_TIME and current_count > 0:
                if prev_service_count is not None and current_count == prev_service_count:
                    break

            prev_service_count = current_count

        # Log diagnostic for ensemble-detected-but-no-services
        if ensemble_detected and not best_stations:
            await self._log(
                "warn",
                f"Ensemble detected on {channel} but no services enumerated — possible incomplete FIC decode",
            )
        elif not ensemble_detected and not best_stations:
            logger.debug("No ensemble detected on %s", channel)

        for station in best_stations:
            await self._registry.add_station(station)

        return best_stations

    @staticmethod
    def _merge_stations(existing: list[dict], new_stations: list[dict]) -> None:
        """Merge new station data into existing list, updating placeholder labels."""
        existing_by_id = {s["id"]: s for s in existing}
        for station in new_stations:
            sid = station["id"]
            if sid in existing_by_id:
                old = existing_by_id[sid]
                # Update placeholder labels with real ones
                if old["name"].startswith("[SID:") and not station["name"].startswith("[SID:"):
                    old["name"] = station["name"]
                # Update other fields that may have arrived later
                if station.get("bitrate") and not old.get("bitrate"):
                    old["bitrate"] = station["bitrate"]
                if station.get("dls") and not old.get("dls"):
                    old["dls"] = station["dls"]
            else:
                existing.append(dict(station))
                existing_by_id[sid] = existing[-1]

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
            if not sid:
                continue

            name = self._extract_label(svc.get("label", ""))

            # Deferred label resolution: keep services with valid SID but no name
            if not name:
                # Use hex placeholder — will be resolved on subsequent polls
                try:
                    sid_int = int(str(sid), 0)
                    name = f"[SID:0x{sid_int:04X}]"
                except (ValueError, TypeError):
                    name = f"[SID:{sid}]"

            # Determine if service has audio components
            has_audio = False
            bitrate: int | None = None
            components = svc.get("components")
            if isinstance(components, list):
                for comp in components:
                    if not isinstance(comp, dict):
                        continue
                    # Check transport mode — "Audio" indicates audio service
                    transport_mode = comp.get("transportmode", "")
                    ascty = comp.get("ascty", "")
                    if transport_mode == "Audio" or ascty in ("DAB", "DAB+"):
                        has_audio = True
                    subchannel = comp.get("subchannel")
                    if isinstance(subchannel, dict) and bitrate is None:
                        br = subchannel.get("bitrate")
                        if br is not None:
                            bitrate = int(br)

            # If no component info available, assume audio
            if not components or not isinstance(components, list):
                has_audio = True

            service_type = "audio" if has_audio else "data"

            # Filter data-only services unless configured to include them
            if service_type == "data" and not INCLUDE_DATA_SERVICES:
                logger.debug(
                    "Skipping data-only service %s (%s) on %s", name, sid, channel
                )
                continue

            station = {
                "id": str(sid),
                "name": name,
                "ensemble": ensemble_label,
                "channel": channel,
                "bitrate": bitrate,
                "mode": svc.get("mode", ""),
                "dls": svc.get("dls_label", ""),
                "type": service_type,
            }
            stations.append(station)

        return stations
