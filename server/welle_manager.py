"""Manages the welle-cli subprocess lifecycle and HTTP interface."""

import asyncio
import logging
import signal

import httpx

from .config import (
    WELLE_CLI_PATH,
    WELLE_CLI_PORT,
    WELLE_CLI_STARTUP_DELAY,
    SDR_GAIN,
    SDR_AGC,
    SDR_PPM,
)

logger = logging.getLogger(__name__)


class WelleManager:
    """Controls the welle-cli process and communicates via its HTTP API."""

    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._running: bool = False
        self._current_channel: str | None = None
        self._monitor_task: asyncio.Task | None = None
        self._http: httpx.AsyncClient = httpx.AsyncClient(timeout=5.0)
        self._device_name: str | None = None

    @property
    def running(self) -> bool:
        return self._running

    @property
    def current_channel(self) -> str | None:
        return self._current_channel

    @property
    def device_name(self) -> str | None:
        return self._device_name

    async def detect_device_name(self) -> str | None:
        """Detect RTL-SDR device name using rtl_test -t (brief run)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "rtl_test", "-t",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr_data = await asyncio.wait_for(proc.communicate(), timeout=3.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                stderr_data = b""

            output = stderr_data.decode(errors="replace")
            # rtl_test prints lines like:
            #   Found 1 device(s):
            #     0:  Realtek, RTL2838UHIDIR, SN: 00000001
            # or  Using device 0: Generic RTL2832U OEM
            for line in output.splitlines():
                stripped = line.strip()
                if stripped.startswith("0:"):
                    # "0:  Realtek, RTL2838UHIDIR, SN: 00000001"
                    self._device_name = stripped[2:].strip()
                    return self._device_name
                if "Using device" in stripped:
                    # "Using device 0: Generic RTL2832U OEM"
                    parts = stripped.split(":", 1)
                    if len(parts) > 1:
                        self._device_name = parts[1].strip()
                        return self._device_name
        except FileNotFoundError:
            logger.debug("rtl_test not found; cannot detect device name")
        except OSError as exc:
            logger.debug("Failed to run rtl_test: %s", exc)

        self._device_name = None
        return None

    async def start(self, channel: str = "9A") -> bool:
        """Start welle-cli on the given channel. Returns True on success."""
        if self._running:
            logger.warning("welle-cli already running; stopping first")
            await self.stop()

        cmd = [
            WELLE_CLI_PATH,
            "-c", channel,
            "-w", str(WELLE_CLI_PORT),
            "-F", "rtl_sdr",
        ]

        # SDR gain: -G (manual) and -Q (AGC) are mutually exclusive.
        # These flags may not be supported by all welle-cli versions,
        # so failures are caught below via stderr capture.
        if SDR_GAIN is not None:
            cmd.extend(["-G", str(int(SDR_GAIN))])
        elif SDR_AGC:
            cmd.append("-Q")

        # PPM frequency correction for RTL-SDR oscillator drift
        if SDR_PPM != 0:
            cmd.extend(["-p", str(SDR_PPM)])

        logger.info("Starting welle-cli: %s", " ".join(cmd))

        try:
            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            logger.error("welle-cli binary not found at %s", WELLE_CLI_PATH)
            return False
        except OSError as exc:
            logger.error("Failed to start welle-cli: %s", exc)
            return False

        await asyncio.sleep(WELLE_CLI_STARTUP_DELAY)

        if self._process.returncode is not None:
            # Capture stderr to diagnose why welle-cli failed
            stderr_output = ""
            try:
                stderr_data = await self._process.stderr.read()
                stderr_output = stderr_data.decode(errors="replace").strip()
            except Exception:
                pass
            logger.error(
                "welle-cli exited immediately with code %d: %s",
                self._process.returncode,
                stderr_output or "(no stderr output)",
            )
            self._process = None
            return False

        self._running = True
        self._current_channel = channel
        self._monitor_task = asyncio.create_task(self._process_monitor())
        logger.info("welle-cli started on channel %s (pid %d)", channel, self._process.pid)
        return True

    async def stop(self) -> None:
        """Gracefully terminate welle-cli (SIGTERM, then SIGKILL after 5s)."""
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None

        if self._process is None:
            self._running = False
            return

        pid = self._process.pid
        logger.info("Stopping welle-cli (pid %d)", pid)

        try:
            self._process.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            logger.debug("welle-cli already exited")
            self._process = None
            self._running = False
            return

        try:
            await asyncio.wait_for(self._process.wait(), timeout=5.0)
            logger.info("welle-cli terminated gracefully")
        except asyncio.TimeoutError:
            logger.warning("welle-cli did not exit after SIGTERM; sending SIGKILL")
            try:
                self._process.kill()
                await self._process.wait()
            except ProcessLookupError:
                pass

        self._process = None
        self._running = False

    async def restart(self, channel: str | None = None) -> bool:
        """Stop and restart welle-cli, optionally on a new channel."""
        target = channel or self._current_channel or "9A"
        await self.stop()
        return await self.start(target)

    async def tune(self, channel: str) -> bool:
        """Tune to a different channel.

        Tries the HTTP POST /channel API first (fast, ~instant).  If the
        endpoint hangs or fails (common with welle-cli 2.4+ds Debian
        package), falls back to a full stop/start cycle on the new channel.
        """
        if channel == self._current_channel:
            return True

        if await self._tune_http(channel):
            return True

        logger.info("HTTP tune failed; restarting welle-cli on channel %s", channel)
        return await self.restart(channel)

    async def _tune_http(self, channel: str) -> bool:
        """Attempt channel change via welle-cli HTTP API (2-second timeout)."""
        url = f"http://localhost:{WELLE_CLI_PORT}/channel"
        try:
            resp = await self._http.post(
                url, content=channel, timeout=2.0,
            )
            if resp.status_code == 200:
                self._current_channel = channel
                logger.info("Tuned to channel %s via HTTP", channel)
                return True
            logger.debug("HTTP tune returned status %d", resp.status_code)
            return False
        except httpx.HTTPError:
            return False

    async def get_mux_json(self) -> dict | None:
        """Fetch and parse /mux.json from welle-cli."""
        url = f"http://localhost:{WELLE_CLI_PORT}/mux.json"
        try:
            resp = await self._http.get(url)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.debug("Failed to fetch mux.json: %s", exc)
            return None

    async def get_stream_url(self, service_id: str) -> str:
        """Return the MP3 stream URL for a given service ID."""
        return f"http://localhost:{WELLE_CLI_PORT}/mp3/{service_id}"

    async def is_healthy(self) -> bool:
        """Check whether welle-cli is alive and responsive."""
        if self._process is None or self._process.returncode is not None:
            return False
        mux = await self.get_mux_json()
        return mux is not None

    async def _process_monitor(self) -> None:
        """Background task that auto-restarts welle-cli on unexpected exit."""
        if self._process is None:
            return

        try:
            returncode = await self._process.wait()
            stderr_output = ""
            try:
                stderr_data = await self._process.stderr.read()
                stderr_output = stderr_data.decode(errors="replace").strip()
                if stderr_output:
                    # Log last few lines to avoid flooding
                    lines = stderr_output.splitlines()[-5:]
                    stderr_output = "\n".join(lines)
            except Exception:
                pass
            logger.warning(
                "welle-cli exited unexpectedly with code %d; restarting. stderr: %s",
                returncode,
                stderr_output or "(none)",
            )
            self._running = False
            self._process = None
            await asyncio.sleep(1.0)
            await self.start(self._current_channel or "9A")
        except asyncio.CancelledError:
            logger.debug("Process monitor cancelled")
