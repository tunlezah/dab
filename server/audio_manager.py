"""Server-side audio playback management."""

import asyncio
import logging
from typing import TYPE_CHECKING

from .config import WELLE_CLI_PORT

if TYPE_CHECKING:
    from .welle_manager import WelleManager

logger = logging.getLogger(__name__)

VALID_OUTPUT_MODES = ("browser", "server", "both")


class AudioManager:
    """Manages server-side audio playback via mpg123."""

    def __init__(self, welle_manager: "WelleManager") -> None:
        self._welle = welle_manager
        self._playback_process: asyncio.subprocess.Process | None = None
        self._current_service_id: str | None = None
        self._output_mode: str = "browser"

    @property
    def current_service_id(self) -> str | None:
        return self._current_service_id

    @property
    def output_mode(self) -> str:
        return self._output_mode

    async def play_server(self, service_id: str) -> bool:
        """Start server-side playback of the given service via mpg123."""
        # Stop any existing playback first
        await self.stop_server()

        url = f"http://localhost:{WELLE_CLI_PORT}/mp3/{service_id}"
        logger.info("Starting server playback: %s", url)

        try:
            self._playback_process = await asyncio.create_subprocess_exec(
                "mpg123", "-q", url,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError:
            logger.error("mpg123 not found; install it for server-side playback")
            return False
        except OSError as exc:
            logger.error("Failed to start mpg123: %s", exc)
            return False

        # Brief check that it didn't exit immediately
        await asyncio.sleep(0.2)
        if self._playback_process.returncode is not None:
            logger.error(
                "mpg123 exited immediately with code %d",
                self._playback_process.returncode,
            )
            self._playback_process = None
            return False

        self._current_service_id = service_id
        logger.info(
            "Server playback started for service %s (pid %d)",
            service_id,
            self._playback_process.pid,
        )
        return True

    async def stop_server(self) -> None:
        """Stop the mpg123 subprocess if running."""
        if self._playback_process is None:
            return

        pid = self._playback_process.pid
        logger.info("Stopping server playback (pid %d)", pid)

        try:
            self._playback_process.terminate()
            await asyncio.wait_for(self._playback_process.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            logger.warning("mpg123 did not exit after terminate; killing")
            try:
                self._playback_process.kill()
                await self._playback_process.wait()
            except ProcessLookupError:
                pass
        except ProcessLookupError:
            pass

        self._playback_process = None
        self._current_service_id = None

    async def set_output_mode(self, mode: str) -> None:
        """Set the output mode: browser, server, or both.

        If switching to browser-only, stop any server-side playback.
        """
        if mode not in VALID_OUTPUT_MODES:
            raise ValueError(
                f"Invalid output mode '{mode}'; must be one of {VALID_OUTPUT_MODES}"
            )

        previous = self._output_mode
        self._output_mode = mode
        logger.info("Output mode changed from %s to %s", previous, mode)

        if mode == "browser":
            await self.stop_server()

    async def is_playing(self) -> bool:
        """Check whether mpg123 is currently running."""
        if self._playback_process is None:
            return False
        return self._playback_process.returncode is None
