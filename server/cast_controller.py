"""Casting controllers for Chromecast and AirPlay devices.

Chromecast: Uses pychromecast to launch the Default Media Receiver and
            send the MP3 stream URL for the device to fetch directly.

AirPlay:    Uses pyatv to connect and stream audio. For AirPlay 2 devices
            that support play_url, sends the HTTP stream URL. For RAOP-only
            devices, pushes transcoded audio data directly.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, asdict
from typing import TYPE_CHECKING

import pychromecast
from pychromecast.controllers.media import MediaController

import pyatv

if TYPE_CHECKING:
    from .device_discovery import DiscoveredDevice
    from .stream_manager import StreamManager

logger = logging.getLogger(__name__)


@dataclass
class CastSession:
    """An active casting session."""
    device_id: str
    device_name: str
    device_type: str  # "chromecast" or "airplay"
    service_id: str
    station_name: str
    stream_session_id: str | None
    status: str  # "connecting", "playing", "stopped", "error"
    started_at: float
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class CastController:
    """Manages casting sessions to Chromecast and AirPlay devices."""

    def __init__(self, stream_manager: "StreamManager", server_host: str, server_port: int) -> None:
        self._stream_manager = stream_manager
        self._server_host = server_host
        self._server_port = server_port
        self._sessions: dict[str, CastSession] = {}
        self._chromecast_objects: dict[str, pychromecast.Chromecast] = {}
        self._airplay_objects: dict[str, object] = {}  # pyatv connections
        self._lock = asyncio.Lock()

    @property
    def active_sessions(self) -> list[dict]:
        """Return all active cast sessions."""
        return [s.to_dict() for s in self._sessions.values()]

    def _build_stream_url(self, service_id: str, fmt: str = "mp3") -> str:
        """Build the public stream URL for a device to fetch."""
        if fmt == "mp3":
            return f"http://{self._server_host}:{self._server_port}/api/cast/stream/{service_id}/mp3"
        return f"http://{self._server_host}:{self._server_port}/api/cast/stream/{service_id}/{fmt}"

    async def cast_to_chromecast(
        self,
        device: "DiscoveredDevice",
        service_id: str,
        station_name: str = "DAB+ Radio",
    ) -> CastSession:
        """Start casting to a Chromecast device.

        The Chromecast device will fetch the MP3 stream directly from our
        HTTP endpoint. No transcoding required — Chromecast natively supports MP3.
        """
        # Stop any existing session on this device
        await self.stop_cast(device.id)

        session = CastSession(
            device_id=device.id,
            device_name=device.name,
            device_type="chromecast",
            service_id=service_id,
            station_name=station_name,
            stream_session_id=None,
            status="connecting",
            started_at=time.monotonic(),
        )
        self._sessions[device.id] = session

        try:
            # Start a stream session for this device (MP3 passthrough)
            stream_session = await self._stream_manager.start_stream(
                service_id=service_id,
                target_format="mp3",
                device_id=device.id,
            )
            session.stream_session_id = stream_session.session_id

            # Connect to Chromecast
            logger.info(
                "Connecting to Chromecast '%s' at %s:%d",
                device.name, device.host, device.port,
            )

            cast = await asyncio.to_thread(
                self._connect_chromecast, device.host, device.port
            )
            self._chromecast_objects[device.id] = cast

            # Build the stream URL the Chromecast will fetch
            stream_url = self._build_stream_url(service_id, "mp3")

            logger.info(
                "Sending stream URL to Chromecast '%s': %s",
                device.name, stream_url,
            )

            # Launch Default Media Receiver and play the stream
            mc = cast.media_controller
            await asyncio.to_thread(
                mc.play_media,
                stream_url,
                "audio/mpeg",
                title=station_name,
                stream_type="LIVE",
            )

            # Wait for media to load
            await asyncio.sleep(1.0)
            await asyncio.to_thread(mc.block_until_active, timeout=10.0)

            session.status = "playing"
            logger.info(
                "Chromecast '%s' now playing '%s'",
                device.name, station_name,
            )
            return session

        except Exception as exc:
            logger.error(
                "Failed to cast to Chromecast '%s': %s",
                device.name, exc, exc_info=True,
            )
            session.status = "error"
            session.error = str(exc)
            # Clean up on failure
            if session.stream_session_id:
                await self._stream_manager.stop_stream(session.stream_session_id)
            self._cleanup_chromecast(device.id)
            raise RuntimeError(f"Chromecast casting failed: {exc}") from exc

    def _connect_chromecast(self, host: str, port: int) -> pychromecast.Chromecast:
        """Connect to a Chromecast device (blocking, run in thread)."""
        chromecasts, browser = pychromecast.get_listed_chromecasts(
            friendly_names=None,
            known_hosts=[host],
        )

        if not chromecasts:
            # Try direct connection by host
            cast = pychromecast.Chromecast(host=host, port=port)
        else:
            cast = chromecasts[0]

        cast.wait()
        browser.stop_discovery()
        return cast

    async def cast_to_airplay(
        self,
        device: "DiscoveredDevice",
        service_id: str,
        station_name: str = "DAB+ Radio",
    ) -> CastSession:
        """Start casting to an AirPlay device.

        Uses pyatv to connect and stream. Attempts play_url first (AirPlay 2),
        falls back to RAOP audio push for older devices.
        """
        # Stop any existing session on this device
        await self.stop_cast(device.id)

        session = CastSession(
            device_id=device.id,
            device_name=device.name,
            device_type="airplay",
            service_id=service_id,
            station_name=station_name,
            stream_session_id=None,
            status="connecting",
            started_at=time.monotonic(),
        )
        self._sessions[device.id] = session

        try:
            # Start a stream session (AAC for AirPlay HTTP streaming)
            stream_session = await self._stream_manager.start_stream(
                service_id=service_id,
                target_format="aac",
                device_id=device.id,
            )
            session.stream_session_id = stream_session.session_id

            # Discover and connect to the specific AirPlay device
            logger.info(
                "Connecting to AirPlay device '%s' at %s",
                device.name, device.host,
            )

            loop = asyncio.get_event_loop()
            atvs = await pyatv.scan(
                loop,
                identifier=device.id,
                hosts=[device.host],
                timeout=5.0,
            )

            if not atvs:
                raise RuntimeError(
                    f"AirPlay device '{device.name}' not found at {device.host}"
                )

            atv_conf = atvs[0]
            atv = await pyatv.connect(atv_conf, loop)
            self._airplay_objects[device.id] = atv

            # Build stream URL for the AirPlay device to fetch
            stream_url = self._build_stream_url(service_id, "aac")

            logger.info(
                "Sending stream URL to AirPlay '%s': %s",
                device.name, stream_url,
            )

            # Try play_url (works with AirPlay 2 devices like Apple TV, HomePod)
            try:
                await atv.stream.play_url(stream_url)
                session.status = "playing"
                logger.info(
                    "AirPlay '%s' now playing '%s' via play_url",
                    device.name, station_name,
                )
                return session
            except (NotImplementedError, AttributeError) as exc:
                logger.info(
                    "play_url not supported on '%s' (%s), trying RAOP stream",
                    device.name, exc,
                )

            # Fallback: RAOP audio push
            # Stop the AAC stream, start a PCM stream instead
            await self._stream_manager.stop_stream(stream_session.session_id)
            stream_session = await self._stream_manager.start_stream(
                service_id=service_id,
                target_format="pcm",
                device_id=device.id,
            )
            session.stream_session_id = stream_session.session_id

            # For RAOP, we need to write a temporary file or use stream_file
            # pyatv's stream_file works best with a file path
            # We'll create a named pipe (FIFO) for real-time streaming
            import tempfile
            import os

            fifo_path = os.path.join(tempfile.gettempdir(), f"dab_airplay_{device.id}.pcm")
            if os.path.exists(fifo_path):
                os.unlink(fifo_path)
            os.mkfifo(fifo_path)

            # Start a background task to feed the FIFO
            async def feed_fifo():
                try:
                    fd = await asyncio.to_thread(os.open, fifo_path, os.O_WRONLY)
                    try:
                        async for chunk in self._stream_manager.read_stream(
                            stream_session.session_id
                        ):
                            await asyncio.to_thread(os.write, fd, chunk)
                    finally:
                        os.close(fd)
                except Exception as e:
                    logger.error("FIFO feed error: %s", e)
                finally:
                    try:
                        os.unlink(fifo_path)
                    except OSError:
                        pass

            feed_task = asyncio.create_task(feed_fifo())

            try:
                await atv.stream.stream_file(fifo_path)
                session.status = "playing"
                logger.info(
                    "AirPlay '%s' now playing '%s' via RAOP",
                    device.name, station_name,
                )
            except Exception as stream_exc:
                feed_task.cancel()
                try:
                    os.unlink(fifo_path)
                except OSError:
                    pass
                raise stream_exc

            return session

        except RuntimeError:
            raise
        except Exception as exc:
            logger.error(
                "Failed to cast to AirPlay '%s': %s",
                device.name, exc, exc_info=True,
            )
            session.status = "error"
            session.error = str(exc)
            # Clean up on failure
            if session.stream_session_id:
                await self._stream_manager.stop_stream(session.stream_session_id)
            await self._cleanup_airplay(device.id)
            raise RuntimeError(f"AirPlay casting failed: {exc}") from exc

    async def stop_cast(self, device_id: str) -> None:
        """Stop casting to a device."""
        session = self._sessions.pop(device_id, None)
        if session is None:
            return

        logger.info("Stopping cast to '%s'", session.device_name)

        # Stop the stream
        if session.stream_session_id:
            await self._stream_manager.stop_stream(session.stream_session_id)

        # Stop device-specific streams
        await self._stream_manager.stop_device_streams(device_id)

        # Disconnect from Chromecast
        if device_id in self._chromecast_objects:
            try:
                cast = self._chromecast_objects[device_id]
                await asyncio.to_thread(cast.quit_app)
            except Exception as exc:
                logger.warning("Error quitting Chromecast app: %s", exc)
            self._cleanup_chromecast(device_id)

        # Disconnect from AirPlay
        if device_id in self._airplay_objects:
            await self._cleanup_airplay(device_id)

        session.status = "stopped"
        logger.info("Cast to '%s' stopped", session.device_name)

    async def stop_all(self) -> None:
        """Stop all active casting sessions."""
        device_ids = list(self._sessions.keys())
        for device_id in device_ids:
            await self.stop_cast(device_id)

    async def get_cast_status(self, device_id: str) -> dict | None:
        """Get the status of a cast session."""
        session = self._sessions.get(device_id)
        if session is None:
            return None

        result = session.to_dict()

        # Try to get live Chromecast status
        if device_id in self._chromecast_objects:
            try:
                cast = self._chromecast_objects[device_id]
                mc = cast.media_controller
                if mc.status:
                    result["player_state"] = str(mc.status.player_state)
                    result["volume"] = cast.status.volume_level if cast.status else None
            except Exception:
                pass

        return result

    async def set_volume(self, device_id: str, volume: float) -> None:
        """Set volume on a casting device (0.0 to 1.0)."""
        volume = max(0.0, min(1.0, volume))

        if device_id in self._chromecast_objects:
            cast = self._chromecast_objects[device_id]
            await asyncio.to_thread(cast.set_volume, volume)
            logger.info("Chromecast '%s' volume set to %.0f%%", device_id, volume * 100)

        elif device_id in self._airplay_objects:
            atv = self._airplay_objects[device_id]
            try:
                await atv.audio.set_volume(volume * 100)
                logger.info("AirPlay '%s' volume set to %.0f%%", device_id, volume * 100)
            except Exception as exc:
                logger.warning("Failed to set AirPlay volume: %s", exc)

    async def pause(self, device_id: str) -> None:
        """Pause playback on a casting device."""
        if device_id in self._chromecast_objects:
            cast = self._chromecast_objects[device_id]
            mc = cast.media_controller
            await asyncio.to_thread(mc.pause)

        elif device_id in self._airplay_objects:
            atv = self._airplay_objects[device_id]
            try:
                await atv.remote_control.pause()
            except Exception as exc:
                logger.warning("Failed to pause AirPlay: %s", exc)

    async def resume(self, device_id: str) -> None:
        """Resume playback on a casting device."""
        if device_id in self._chromecast_objects:
            cast = self._chromecast_objects[device_id]
            mc = cast.media_controller
            await asyncio.to_thread(mc.play)

        elif device_id in self._airplay_objects:
            atv = self._airplay_objects[device_id]
            try:
                await atv.remote_control.play()
            except Exception as exc:
                logger.warning("Failed to resume AirPlay: %s", exc)

    def _cleanup_chromecast(self, device_id: str) -> None:
        """Clean up a Chromecast connection."""
        cast = self._chromecast_objects.pop(device_id, None)
        if cast:
            try:
                cast.disconnect()
            except Exception:
                pass

    async def _cleanup_airplay(self, device_id: str) -> None:
        """Clean up an AirPlay connection."""
        atv = self._airplay_objects.pop(device_id, None)
        if atv:
            try:
                atv.close()
            except Exception:
                pass
