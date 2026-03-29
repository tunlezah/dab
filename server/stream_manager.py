"""Stream manager for transcoding DAB+ audio for casting devices.

Manages FFmpeg transcoding pipelines that convert the MP3 stream from
welle-cli into formats suitable for Chromecast and AirPlay devices.

Chromecast: MP3 passthrough (natively supported) or AAC in ADTS
AirPlay: AAC-LC in MPEG-TS container (widely compatible)
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .config import WELLE_CLI_PORT

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# FFmpeg pipeline configurations per target format
FFMPEG_PIPELINES = {
    # MP3 passthrough - just re-stream the MP3 from welle-cli
    # Chromecast natively supports MP3 over HTTP, no transcoding needed
    "mp3": None,  # No FFmpeg, direct proxy

    # AAC-LC in ADTS container - universal Chromecast/AirPlay compatibility
    # Input: MP3 from welle-cli | Output: AAC-LC 128kbps 48kHz stereo ADTS
    "aac": [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
        "-i", "{input_url}",
        "-vn",
        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "48000",
        "-ac", "2",
        "-f", "adts",
        "-flush_packets", "1",
        "pipe:1",
    ],

    # WAV/PCM - for AirPlay RAOP streaming (raw audio push)
    # Input: MP3 from welle-cli | Output: PCM s16le 44100Hz stereo
    "pcm": [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
        "-i", "{input_url}",
        "-vn",
        "-c:a", "pcm_s16le",
        "-ar", "44100",
        "-ac", "2",
        "-f", "s16le",
        "-flush_packets", "1",
        "pipe:1",
    ],

    # MPEG-TS with AAC - for HTTP Live Streaming compatible devices
    # Good for network streaming as MPEG-TS handles packet loss gracefully
    "mpegts": [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
        "-i", "{input_url}",
        "-vn",
        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "48000",
        "-ac", "2",
        "-f", "mpegts",
        "-flush_packets", "1",
        "pipe:1",
    ],
}

# Content types for each format
CONTENT_TYPES = {
    "mp3": "audio/mpeg",
    "aac": "audio/aac",
    "pcm": "audio/L16;rate=44100;channels=2",
    "mpegts": "video/MP2T",
}


@dataclass
class StreamSession:
    """An active transcoding stream session."""
    session_id: str
    service_id: str
    target_format: str
    device_id: str | None
    process: asyncio.subprocess.Process | None
    created_at: float
    active: bool = True

    @property
    def pid(self) -> int | None:
        if self.process and self.process.returncode is None:
            return self.process.pid
        return None


class StreamManager:
    """Manages transcoding streams for casting devices.

    Each cast session gets its own FFmpeg process (or direct proxy for MP3).
    Handles lifecycle: start, monitor, restart on crash, stop.
    """

    def __init__(self, max_streams: int = 4, restart_delay: float = 2.0) -> None:
        self._sessions: dict[str, StreamSession] = {}
        self._max_streams = max_streams
        self._restart_delay = restart_delay
        self._lock = asyncio.Lock()
        self._monitor_tasks: dict[str, asyncio.Task] = {}
        self._session_counter = 0

    @property
    def active_sessions(self) -> list[dict]:
        """Return info about active stream sessions."""
        return [
            {
                "session_id": s.session_id,
                "service_id": s.service_id,
                "format": s.target_format,
                "device_id": s.device_id,
                "pid": s.pid,
                "active": s.active,
            }
            for s in self._sessions.values()
            if s.active
        ]

    def _next_session_id(self) -> str:
        self._session_counter += 1
        return f"stream-{self._session_counter}"

    def _build_input_url(self, service_id: str) -> str:
        """Build the welle-cli MP3 stream URL for a service."""
        return f"http://localhost:{WELLE_CLI_PORT}/mp3/{service_id}"

    async def start_stream(
        self,
        service_id: str,
        target_format: str = "mp3",
        device_id: str | None = None,
    ) -> StreamSession:
        """Start a new transcoding stream session.

        Args:
            service_id: The DAB+ station service ID.
            target_format: Output format (mp3, aac, pcm, mpegts).
            device_id: Optional device ID this stream is for.

        Returns:
            The created StreamSession.
        """
        if target_format not in FFMPEG_PIPELINES:
            raise ValueError(f"Unsupported format: {target_format}")

        async with self._lock:
            active_count = sum(1 for s in self._sessions.values() if s.active)
            if active_count >= self._max_streams:
                raise RuntimeError(
                    f"Maximum concurrent streams ({self._max_streams}) reached"
                )

            session_id = self._next_session_id()

        pipeline = FFMPEG_PIPELINES[target_format]
        process = None

        if pipeline is not None:
            # Build FFmpeg command with actual input URL
            input_url = self._build_input_url(service_id)
            cmd = [
                arg.replace("{input_url}", input_url) for arg in pipeline
            ]

            logger.info(
                "Starting FFmpeg stream %s: %s → %s (device=%s)",
                session_id, service_id, target_format, device_id,
            )

            try:
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except FileNotFoundError:
                raise RuntimeError(
                    "FFmpeg not found. Install FFmpeg for transcoding support."
                )
            except OSError as exc:
                raise RuntimeError(f"Failed to start FFmpeg: {exc}")

            # Brief check that it didn't exit immediately
            await asyncio.sleep(0.3)
            if process.returncode is not None:
                stderr = ""
                if process.stderr:
                    stderr_bytes = await process.stderr.read()
                    stderr = stderr_bytes.decode(errors="replace")[:500]
                raise RuntimeError(
                    f"FFmpeg exited immediately (code {process.returncode}): {stderr}"
                )

        session = StreamSession(
            session_id=session_id,
            service_id=service_id,
            target_format=target_format,
            device_id=device_id,
            process=process,
            created_at=time.monotonic(),
        )

        async with self._lock:
            self._sessions[session_id] = session

        # Start monitoring task for FFmpeg process health
        if process is not None:
            task = asyncio.create_task(
                self._monitor_process(session_id),
                name=f"monitor-{session_id}",
            )
            self._monitor_tasks[session_id] = task

        logger.info("Stream session %s started (pid=%s)", session_id, session.pid)
        return session

    async def stop_stream(self, session_id: str) -> None:
        """Stop a stream session and kill its FFmpeg process."""
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return
            session.active = False

        # Cancel monitor task
        monitor = self._monitor_tasks.pop(session_id, None)
        if monitor:
            monitor.cancel()
            try:
                await monitor
            except asyncio.CancelledError:
                pass

        # Kill FFmpeg process
        if session.process and session.process.returncode is None:
            pid = session.process.pid
            logger.info("Stopping FFmpeg process %d for session %s", pid, session_id)
            try:
                session.process.terminate()
                await asyncio.wait_for(session.process.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                logger.warning("FFmpeg %d did not exit after terminate; killing", pid)
                try:
                    session.process.kill()
                    await session.process.wait()
                except ProcessLookupError:
                    pass
            except ProcessLookupError:
                pass

        async with self._lock:
            self._sessions.pop(session_id, None)

        logger.info("Stream session %s stopped", session_id)

    async def stop_device_streams(self, device_id: str) -> None:
        """Stop all streams associated with a specific device."""
        session_ids = [
            s.session_id
            for s in self._sessions.values()
            if s.device_id == device_id and s.active
        ]
        for sid in session_ids:
            await self.stop_stream(sid)

    async def stop_all(self) -> None:
        """Stop all active stream sessions."""
        session_ids = list(self._sessions.keys())
        for sid in session_ids:
            await self.stop_stream(sid)

    def get_session(self, session_id: str) -> StreamSession | None:
        """Get a stream session by ID."""
        return self._sessions.get(session_id)

    def get_session_for_device(self, device_id: str) -> StreamSession | None:
        """Get the active stream session for a device."""
        for session in self._sessions.values():
            if session.device_id == device_id and session.active:
                return session
        return None

    async def read_stream(self, session_id: str):
        """Async generator that yields audio chunks from a stream session.

        For MP3 format (no FFmpeg), proxies directly from welle-cli.
        For transcoded formats, reads from FFmpeg stdout.
        """
        session = self._sessions.get(session_id)
        if session is None:
            return

        if session.target_format == "mp3":
            # Direct proxy from welle-cli
            import httpx
            url = self._build_input_url(session.service_id)
            async with httpx.AsyncClient(timeout=None) as client:
                try:
                    async with client.stream("GET", url) as response:
                        async for chunk in response.aiter_bytes(chunk_size=4096):
                            if not session.active:
                                break
                            yield chunk
                except httpx.HTTPError as exc:
                    logger.error("MP3 proxy error for session %s: %s", session_id, exc)
        else:
            # Read from FFmpeg stdout
            if session.process is None or session.process.stdout is None:
                return

            try:
                while session.active:
                    chunk = await session.process.stdout.read(4096)
                    if not chunk:
                        break
                    yield chunk
            except Exception as exc:
                logger.error(
                    "Error reading FFmpeg output for session %s: %s",
                    session_id, exc,
                )

    async def _monitor_process(self, session_id: str) -> None:
        """Monitor an FFmpeg process and auto-restart on crash."""
        while True:
            session = self._sessions.get(session_id)
            if session is None or not session.active:
                return

            if session.process is None:
                return

            # Wait for process to exit
            returncode = await session.process.wait()

            if not session.active:
                return  # Intentionally stopped

            logger.warning(
                "FFmpeg process for session %s exited with code %d; restarting in %.1fs",
                session_id, returncode, self._restart_delay,
            )

            # Log stderr if available
            if session.process.stderr:
                try:
                    stderr_bytes = await session.process.stderr.read()
                    stderr_text = stderr_bytes.decode(errors="replace")[:500]
                    if stderr_text.strip():
                        logger.warning("FFmpeg stderr: %s", stderr_text)
                except Exception:
                    pass

            await asyncio.sleep(self._restart_delay)

            if not session.active:
                return

            # Restart the process
            pipeline = FFMPEG_PIPELINES.get(session.target_format)
            if pipeline is None:
                return

            input_url = self._build_input_url(session.service_id)
            cmd = [arg.replace("{input_url}", input_url) for arg in pipeline]

            try:
                session.process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                logger.info(
                    "FFmpeg restarted for session %s (new pid=%d)",
                    session_id, session.process.pid,
                )
            except Exception as exc:
                logger.error(
                    "Failed to restart FFmpeg for session %s: %s",
                    session_id, exc,
                )
                session.active = False
                return
