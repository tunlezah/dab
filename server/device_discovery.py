"""Device discovery service for Chromecast and AirPlay devices.

Discovers devices on the local network via mDNS/Zeroconf.
Discovery is triggered on-demand (not continuous) and results are cached.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field, asdict
from enum import Enum

import pychromecast
import pyatv

logger = logging.getLogger(__name__)


class DeviceType(str, Enum):
    CHROMECAST = "chromecast"
    AIRPLAY = "airplay"


@dataclass
class DiscoveredDevice:
    """A discovered casting device."""
    id: str
    name: str
    device_type: str  # "chromecast" or "airplay"
    host: str
    port: int
    model: str = ""
    manufacturer: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class DeviceDiscovery:
    """On-demand device discovery with caching.

    Devices are only discovered when scan() is called.
    Results are cached for a configurable duration.
    """

    def __init__(self, cache_ttl: float = 60.0, scan_timeout: float = 8.0) -> None:
        self._cache_ttl = cache_ttl
        self._scan_timeout = scan_timeout
        self._devices: list[DiscoveredDevice] = []
        self._last_scan: float = 0.0
        self._scanning = False
        self._lock = asyncio.Lock()

    @property
    def devices(self) -> list[DiscoveredDevice]:
        """Return the last discovered devices."""
        return list(self._devices)

    @property
    def scanning(self) -> bool:
        return self._scanning

    @property
    def cache_valid(self) -> bool:
        """Check if the cached device list is still valid."""
        if not self._devices:
            return False
        return (time.monotonic() - self._last_scan) < self._cache_ttl

    async def scan(self) -> list[DiscoveredDevice]:
        """Scan for all casting devices (Chromecast + AirPlay).

        Returns cached results if cache is still valid.
        """
        async with self._lock:
            if self.cache_valid:
                logger.info("Returning %d cached devices", len(self._devices))
                return list(self._devices)

            self._scanning = True

        try:
            # Run both scans concurrently
            chromecast_task = asyncio.create_task(self._scan_chromecasts())
            airplay_task = asyncio.create_task(self._scan_airplay())

            chromecasts, airplays = await asyncio.gather(
                chromecast_task, airplay_task, return_exceptions=True
            )

            devices = []
            if isinstance(chromecasts, list):
                devices.extend(chromecasts)
            else:
                logger.error("Chromecast scan failed: %s", chromecasts)

            if isinstance(airplays, list):
                devices.extend(airplays)
            else:
                logger.error("AirPlay scan failed: %s", airplays)

            async with self._lock:
                self._devices = devices
                self._last_scan = time.monotonic()

            logger.info(
                "Device scan complete: %d Chromecast, %d AirPlay",
                sum(1 for d in devices if d.device_type == DeviceType.CHROMECAST),
                sum(1 for d in devices if d.device_type == DeviceType.AIRPLAY),
            )
            return list(devices)

        finally:
            self._scanning = False

    async def _scan_chromecasts(self) -> list[DiscoveredDevice]:
        """Scan for Chromecast devices using pychromecast/zeroconf."""
        devices = []
        browser = None
        zconf = None

        try:
            from pychromecast.discovery import CastBrowser, SimpleCastListener

            from zeroconf import Zeroconf

            zconf = Zeroconf()
            found_uuids = set()
            cast_infos = {}
            discovery_complete = asyncio.Event()

            class Listener(SimpleCastListener):
                def add_cast(self, uuid, service):
                    found_uuids.add(uuid)
                    cast_infos[uuid] = service

                def update_cast(self, uuid, service):
                    cast_infos[uuid] = service

                def remove_cast(self, uuid, service, reason):
                    found_uuids.discard(uuid)

            listener = Listener()
            browser = CastBrowser(listener, zconf)
            browser.start_discovery()

            # Wait for discovery timeout
            await asyncio.sleep(self._scan_timeout)

            # Collect discovered devices
            for uuid in found_uuids:
                info = cast_infos.get(uuid)
                if info is None:
                    continue

                device_id = str(uuid)
                name = info.friendly_name or f"Chromecast-{device_id[:8]}"
                host = str(info.host) if info.host else ""
                port = info.port or 8009
                model = info.model_name or "Chromecast"
                manufacturer = info.manufacturer or "Google"

                devices.append(DiscoveredDevice(
                    id=device_id,
                    name=name,
                    device_type=DeviceType.CHROMECAST,
                    host=host,
                    port=port,
                    model=model,
                    manufacturer=manufacturer,
                ))

            logger.info("Found %d Chromecast device(s)", len(devices))

        except Exception as exc:
            logger.error("Chromecast discovery error: %s", exc, exc_info=True)

        finally:
            if browser:
                try:
                    browser.stop_discovery()
                except Exception:
                    pass
            if zconf:
                try:
                    zconf.close()
                except Exception:
                    pass

        return devices

    async def _scan_airplay(self) -> list[DiscoveredDevice]:
        """Scan for AirPlay devices using pyatv."""
        devices = []

        try:
            loop = asyncio.get_event_loop()
            atvs = await pyatv.scan(
                loop,
                timeout=self._scan_timeout,
                protocol=pyatv.Protocol.AirPlay,
            )

            for atv_conf in atvs:
                # Use the AirPlay service address
                address = str(atv_conf.address)
                port = 7000  # default AirPlay port

                for service in atv_conf.services:
                    if service.protocol == pyatv.Protocol.AirPlay:
                        port = service.port
                        break

                device_id = atv_conf.identifier or str(atv_conf.address)
                name = atv_conf.name or f"AirPlay-{device_id[:8]}"
                model = atv_conf.device_info.model_str if atv_conf.device_info else ""
                manufacturer = "Apple"

                devices.append(DiscoveredDevice(
                    id=device_id,
                    name=name,
                    device_type=DeviceType.AIRPLAY,
                    host=address,
                    port=port,
                    model=model,
                    manufacturer=manufacturer,
                ))

            logger.info("Found %d AirPlay device(s)", len(devices))

        except Exception as exc:
            logger.error("AirPlay discovery error: %s", exc, exc_info=True)

        return devices

    def get_device(self, device_id: str) -> DiscoveredDevice | None:
        """Look up a cached device by ID."""
        for device in self._devices:
            if device.id == device_id:
                return device
        return None

    def clear_cache(self) -> None:
        """Force-clear the device cache."""
        self._devices = []
        self._last_scan = 0.0
