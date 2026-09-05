"""Runs go-librespot as the Spotify Connect device and points its PCM at our named pipe.

Upstream's Windows build stubs out the pipe backend, so the binary this downloads is the same
version with that one file implemented; see CLAUDE.md. Credentials come from the desktop app
handing off over zeroconf - there is no password login left - and are persisted so later runs need
no re-pick.
"""

import asyncio
import subprocess

from ... import jobs, paths
from .pipe import PIPE_NAME

CREATE_NO_WINDOW = 0x08000000

#: A fixed port so one firewall rule covers the device across runs.
ZEROCONF_PORT = 5354


def write_config(directory, device_name: str, api_port: int) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    # Single quotes so YAML keeps the pipe path's backslashes verbatim.
    config = f"""log_level: info
device_name: '{device_name}'
device_type: computer
zeroconf_enabled: true
zeroconf_port: {ZEROCONF_PORT}
credentials:
  type: zeroconf
  zeroconf:
    persist_credentials: true
audio_backend: pipe
audio_output_pipe: '{PIPE_NAME}'
audio_output_pipe_format: s16le
bitrate: 320
normalisation_disabled: false
external_volume: true
server:
  enabled: true
  address: localhost
  port: {api_port}
  image_size: large
"""
    (directory / "config.yml").write_text(config, encoding="utf-8")


class LibrespotProcess:
    def __init__(self, hub) -> None:
        self._hub = hub
        self._process: asyncio.subprocess.Process | None = None
        self._readers: list[asyncio.Task] = []

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def start(self, executable: str, device_name: str, api_port: int) -> None:
        write_config(paths.LIBRESPOT_DIR, device_name, api_port)

        self._process = await asyncio.create_subprocess_exec(
            executable, "--config_dir", str(paths.LIBRESPOT_DIR),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
        )
        jobs.adopt(self._process)

        self._readers = [
            asyncio.create_task(self._read(self._process.stdout)),
            asyncio.create_task(self._read(self._process.stderr)),
        ]

    async def _read(self, stream) -> None:
        async for raw in stream:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            lowered = line.lower()
            if "level=error" in lowered or "level=fatal" in lowered:
                self._hub.error(f"go-librespot: {line[:200]}")
            elif "level=warn" in lowered:
                self._hub.warn(f"go-librespot: {line[:200]}")

    async def stop(self) -> None:
        process, self._process = self._process, None
        for task in self._readers:
            task.cancel()
        self._readers = []

        if process is None:
            return

        try:
            process.kill()
            await asyncio.wait_for(process.wait(), 3)
        except (OSError, ProcessLookupError, TimeoutError):
            pass
