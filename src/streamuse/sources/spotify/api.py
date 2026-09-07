"""go-librespot's local HTTP API and its event socket.

Everything the panel shows about a Spotify track arrives pushed on the socket, so nothing here
polls; /status is only read on connect to resync after a reconnection.
"""

import asyncio

import aiohttp

RECONNECT_DELAY = 2
REQUEST_TIMEOUT = 5

COMMANDS = {"playpause": "playpause", "next": "next", "prev": "prev"}


class LibrespotApi:
    def __init__(self, port: int, hub, on_event) -> None:
        self._base = f"http://127.0.0.1:{port}"
        self._hub = hub
        self._on_event = on_event
        self._session: aiohttp.ClientSession | None = None
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT))
        self._task = asyncio.create_task(self._listen())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def command(self, name: str) -> bool:
        path = COMMANDS.get(name)
        if path is None or self._session is None:
            return False
        try:
            async with self._session.post(f"{self._base}/player/{path}") as reply:
                return reply.status < 400
        except aiohttp.ClientError as exc:
            self._hub.warn(f"spotify: command failed ({exc})")
            return False

    async def fetch_cover(self, url: str) -> bytes | None:
        if self._session is None:
            return None
        try:
            async with self._session.get(url) as reply:
                if reply.status != 200:
                    return None
                return await reply.read()
        except aiohttp.ClientError:
            return None

    async def _listen(self) -> None:
        """The daemon takes a moment to bind its port, and restarts are its own business, so this
        keeps trying for as long as the receiver is selected."""
        announced = False
        while True:
            try:
                await self._pump(announced)
                announced = True
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(RECONNECT_DELAY)

    async def _pump(self, announced: bool) -> None:
        assert self._session is not None

        async with self._session.ws_connect(f"{self._base}/events", timeout=REQUEST_TIMEOUT) as ws:
            if not announced:
                self._hub.info("spotify: connected to the Spotify Connect device")
            await self._resync()

            async for message in ws:
                if message.type is not aiohttp.WSMsgType.TEXT:
                    break
                payload = message.json()
                await self._on_event(payload.get("type", ""), payload.get("data") or {})

    async def _resync(self) -> None:
        assert self._session is not None
        try:
            async with self._session.get(f"{self._base}/status") as reply:
                if reply.status == 200:
                    await self._on_event("status", await reply.json())
        except aiohttp.ClientError:
            pass
