"""Publishes the public port through cloudflared. The tunnel's lifetime is independent of the
encoder's: separate buttons, plus the auto-tunnel setting."""

import asyncio
import re
import subprocess

from . import jobs
from .state import TUNNEL_ERROR, TUNNEL_OFF, TUNNEL_STARTING, TUNNEL_UP, TunnelState

CREATE_NO_WINDOW = 0x08000000
QUICK_TUNNEL_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
STOP_TIMEOUT = 3


class CloudflaredTunnel:
    def __init__(self, settings, hub, deps, public_port: int) -> None:
        self._settings = settings
        self._hub = hub
        self._deps = deps
        self._public_port = public_port

        self._gate = asyncio.Lock()
        self._process: asyncio.subprocess.Process | None = None
        self._readers: list[asyncio.Task] = []
        self._public_url: str | None = None

    async def start(self) -> bool:
        async with self._gate:
            if self._process is not None:
                return True

            if self._deps.cloudflared is None:
                self._fail("cloudflared is not available - check the Dependencies panel")
                return False

            named = self._settings.tunnelMode == "Named"
            if named and not self._settings.namedTunnelToken.strip():
                self._fail("a named tunnel needs its token in Settings")
                return False

            self._hub.set_tunnel(TunnelState(TUNNEL_STARTING, None, None))
            self._public_url = None

            try:
                process = await asyncio.create_subprocess_exec(
                    self._deps.cloudflared, *self._arguments(named),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    creationflags=CREATE_NO_WINDOW,
                )
            except OSError as exc:
                self._fail(f"could not start cloudflared: {exc}")
                return False

            self._process = process
            jobs.adopt(process)

            self._readers = [
                asyncio.create_task(self._inspect(process.stdout)),
                asyncio.create_task(self._inspect(process.stderr)),
                asyncio.create_task(self._watch_exit(process)),
            ]

            if named:
                # A named tunnel prints no URL of its own; the hostname is the one configured.
                host = self._settings.namedTunnelHostname.strip()
                url = f"https://{host}/live/{self._settings.streamKey}/index.m3u8" if host else None
                self._public_url = url
                self._hub.set_tunnel(TunnelState(TUNNEL_UP, url, None))
                self._hub.info("named tunnel started")

            return True

    def _arguments(self, named: bool) -> list[str]:
        if named:
            return ["tunnel", "--no-autoupdate", "run", "--token",
                    self._settings.namedTunnelToken.strip()]
        return ["tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{self._public_port}"]

    async def _inspect(self, stream) -> None:
        async for raw in stream:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue

            if self._public_url is None:
                match = QUICK_TUNNEL_URL.search(line)
                if match:
                    url = f"{match.group()}/live/{self._settings.streamKey}/index.m3u8"
                    self._public_url = url
                    self._hub.set_tunnel(TunnelState(TUNNEL_UP, url, None))
                    self._hub.info(f"tunnel up - {url}")
                    continue

            if "ERR " in line or "error" in line.lower():
                self._hub.warn(f"cloudflared: {line[:200]}")

    async def _watch_exit(self, process) -> None:
        code = await process.wait()
        # A late exit from the previous process must not overwrite the current one's state.
        if self._process is not process:
            return
        self._process = None
        if self._hub.tunnel.status != TUNNEL_OFF:
            self._fail(f"cloudflared exited (code {code})")

    async def stop(self) -> None:
        async with self._gate:
            process, self._process = self._process, None
            if process is None:
                return

            for task in self._readers:
                task.cancel()
            self._readers = []
            self._public_url = None

            try:
                process.kill()
                await asyncio.wait_for(process.wait(), STOP_TIMEOUT)
            except (OSError, ProcessLookupError, TimeoutError):
                pass

            self._hub.set_tunnel(TunnelState(TUNNEL_OFF, None, None))
            self._hub.info("tunnel stopped")

    def _fail(self, message: str) -> None:
        self._hub.error(message)
        self._hub.set_tunnel(TunnelState(TUNNEL_ERROR, None, message))
