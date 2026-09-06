"""Remote control back to the sender.

The Active-Remote token the sender put in its RTSP headers is the whole authentication, so there is
no pairing step. The endpoint is only advertised while it streams and moves between sessions, so it
is resolved lazily and dropped when the session ends.
"""

import aiohttp

from .mdns import resolve_dacp

COMMANDS = {
    "playpause": "playpause",
    "pause": "pause",
    "resume": "play",
    "next": "nextitem",
    "prev": "previtem",
}


class DacpClient:
    def __init__(self, hub) -> None:
        self._hub = hub
        self._endpoint: tuple[str, int] | None = None
        self._dacp_id = ""
        self._active_remote = ""

    def bind(self, dacp_id: str, active_remote: str) -> None:
        if dacp_id != self._dacp_id:
            self._endpoint = None
        self._dacp_id = dacp_id
        self._active_remote = active_remote

    def forget(self) -> None:
        self._endpoint = None
        self._dacp_id = ""
        self._active_remote = ""

    @property
    def available(self) -> bool:
        return bool(self._dacp_id and self._active_remote)

    async def send(self, command: str, zeroconf) -> bool:
        target = COMMANDS.get(command)
        if target is None or not self.available or zeroconf is None:
            return False

        endpoint = self._endpoint
        if endpoint is None:
            endpoint = await resolve_dacp(zeroconf, self._dacp_id)
            if endpoint is None:
                self._hub.warn("airplay: the sender is not advertising a remote-control port")
                return False
            self._endpoint = endpoint

        host, port = endpoint
        url = f"http://{host}:{port}/ctrl-int/1/{target}"

        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=3)
            ) as session:
                async with session.get(url, headers={"Active-Remote": self._active_remote}) as reply:
                    if reply.status in (200, 204):
                        return True
                    self._hub.warn(f"airplay: remote command refused ({reply.status})")
        except aiohttp.ClientError as exc:
            # The port moves between sessions; a refusal means the cached one is stale.
            self._endpoint = None
            self._hub.warn(f"airplay: remote command failed ({exc})")

        return False
