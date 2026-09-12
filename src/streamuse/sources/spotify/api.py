"""go-librespot's local HTTP API and its event socket.

Everything the panel shows about a Spotify track arrives pushed on the socket, so nothing here
polls; /status is only read on connect to resync after a reconnection.

Searching also runs through here: the daemon's /token hands out an access token for the session the
desktop app already handed over, so listener requests need nothing registered and nobody logged in.
"""

import asyncio
import time

import aiohttp

from .. import RequestTrack

RECONNECT_DELAY = 2
REQUEST_TIMEOUT = 5

COMMANDS = {"playpause": "playpause", "next": "next", "prev": "prev"}

SEARCH_URL = "https://api.spotify.com/v1/search"

#: This token is borrowed from the desktop app's own session rather than issued to a registered
#: app, so its search quota is tighter than the public Web API's and gets hit in normal use, not
#: just abuse. Spotify's 429 carries how long to actually wait in Retry-After; this is only the
#: fallback for when that header is missing or unparseable.
DEFAULT_RETRY_AFTER = 5.0


class LibrespotApi:
    def __init__(self, port: int, hub, on_event) -> None:
        self._base = f"http://127.0.0.1:{port}"
        self._hub = hub
        self._on_event = on_event
        self._session: aiohttp.ClientSession | None = None
        self._task: asyncio.Task | None = None
        self._token: str | None = None
        #: A monotonic deadline, not a bool: retrying the instant it flips would trip the same
        #: window again, since Spotify's own limiter has not moved either.
        self._search_retry_after = 0.0

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

    async def add_to_queue(self, uri: str) -> bool:
        if self._session is None:
            return False
        try:
            async with self._session.post(
                f"{self._base}/player/add_to_queue", json={"uri": uri}
            ) as reply:
                return reply.status < 400
        except aiohttp.ClientError as exc:
            self._hub.warn(f"spotify: could not queue the track ({exc})")
            return False

    async def search(self, query: str) -> RequestTrack | None:
        """One retry, because the cached token is only ever discovered to be stale by being
        refused."""
        if time.monotonic() < self._search_retry_after:
            return None

        for _ in range(2):
            token = await self._token_for_search()
            if token is None:
                return None

            try:
                async with self._session.get(
                    SEARCH_URL,
                    params={"q": query, "type": "track", "limit": "1"},
                    headers={"Authorization": f"Bearer {token}"},
                ) as reply:
                    if reply.status == 401:
                        self._token = None
                        continue
                    if reply.status == 429:
                        wait = _retry_after_seconds(reply.headers.get("Retry-After"))
                        self._search_retry_after = time.monotonic() + wait
                        self._hub.warn(f"spotify: search rate limited - waiting {wait:.0f}s")
                        return None
                    if reply.status != 200:
                        self._hub.warn(f"spotify: search failed ({reply.status})")
                        return None
                    return _first_track(await reply.json())
            except aiohttp.ClientError as exc:
                self._hub.warn(f"spotify: search failed ({exc})")
                return None

        return None

    async def _token_for_search(self) -> str | None:
        """Cached: the daemon forces a fresh login5 round trip on every call, and a listener page
        can ask for a search far more often than a token needs replacing."""
        if self._token is not None:
            return self._token
        if self._session is None:
            return None

        try:
            async with self._session.post(f"{self._base}/token") as reply:
                # 204 is the daemon saying there is no session yet, so there is nobody to search as.
                if reply.status != 200:
                    return None
                self._token = (await reply.json()).get("token") or None
        except aiohttp.ClientError as exc:
            self._hub.warn(f"spotify: could not get an access token ({exc})")
            return None

        return self._token

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


def _retry_after_seconds(header: str | None) -> float:
    """Spotify sends this as a plain integer count of seconds, not an HTTP-date, but a stray or
    absent header must not crash a rate-limit response - it is already the bad-news path."""
    try:
        return max(0.0, float(header))
    except (TypeError, ValueError):
        return DEFAULT_RETRY_AFTER


def _first_track(payload: dict) -> RequestTrack | None:
    items = (payload.get("tracks") or {}).get("items") or []
    if not items:
        return None

    track = items[0]
    album = track.get("album") or {}
    images = album.get("images") or []

    return RequestTrack(
        id=track.get("uri") or "",
        title=track.get("name") or "",
        artist=", ".join(a.get("name") or "" for a in track.get("artists") or []),
        album=album.get("name") or "",
        # Largest first, and the page wants a thumbnail.
        artUrl=images[-1].get("url") or "" if images else "",
    )
