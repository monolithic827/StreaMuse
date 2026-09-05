"""Advertising the speaker, and finding the sender's remote-control port.

The TXT record is shairport-sync's classic set, which is what iTunes and Apple Music for Windows
have been driving for a decade. `md=0,1,2` is what makes a sender push text, artwork and progress.
Nothing here may advertise AirPlay 2 keys or an `_airplay._tcp` service: a sender that sees them
tries AirPlay 2 first, and the Windows apps only speak AirPlay 1.
"""

import socket
import uuid

from zeroconf import IPVersion, ServiceInfo
from zeroconf.asyncio import AsyncServiceInfo, AsyncZeroconf

RAOP_TYPE = "_raop._tcp.local."
DACP_TYPE = "_dacp._tcp.local."

TXT = {
    "txtvers": "1",
    "ch": "2",
    "cn": "0,1",
    "et": "0,1",
    "ek": "1",
    "md": "0,1,2",
    "sr": "44100",
    "ss": "16",
    "pw": "false",
    "sv": "false",
    "da": "true",
    "vn": "65537",
    "tp": "UDP",
    "vs": "105.1",
    "am": "StreaMuse1,1",
    "sf": "0x4",
}


def hardware_address() -> bytes:
    """The six bytes that both name the service and get signed into Apple-Response."""
    return uuid.getnode().to_bytes(6, "big")


class RaopAdvertisement:
    def __init__(self, name: str, port: int, address: str, mac: bytes) -> None:
        self._zeroconf: AsyncZeroconf | None = None
        self._info = ServiceInfo(
            RAOP_TYPE,
            f"{mac.hex().upper()}@{name}.{RAOP_TYPE}",
            port=port,
            addresses=[socket.inet_aton(address)],
            properties=dict(TXT),
            server=f"{socket.gethostname()}.local.",
        )

    async def start(self) -> None:
        self._zeroconf = AsyncZeroconf(ip_version=IPVersion.V4Only)
        await self._zeroconf.async_register_service(self._info)

    async def stop(self) -> None:
        if self._zeroconf is None:
            return
        try:
            await self._zeroconf.async_unregister_service(self._info)
        finally:
            await self._zeroconf.async_close()
            self._zeroconf = None

    @property
    def zeroconf(self) -> AsyncZeroconf | None:
        return self._zeroconf


async def resolve_dacp(zeroconf: AsyncZeroconf, dacp_id: str, timeout: int = 3000):
    """The sender advertises its control port only while it is streaming, and re-advertises with a
    new id per session, so this is resolved on demand rather than cached across sessions."""
    info = AsyncServiceInfo(DACP_TYPE, f"iTunes_Ctrl_{dacp_id}.{DACP_TYPE}")
    if not await info.async_request(zeroconf.zeroconf, timeout):
        return None

    addresses = info.parsed_addresses()
    if not addresses:
        return None
    return addresses[0], info.port
