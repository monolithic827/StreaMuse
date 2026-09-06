"""The AirPlay speaker Apple Music sends to.

Apple Music and iTunes for Windows speak AirPlay 1 only, so this is a RAOP receiver: no HomeKit
pairing, no FairPlay. Audio, metadata and artwork all arrive over the one session, which is why -
unlike the media transport controls this replaced - a track can never be attributed to the wrong
source.
"""

import time

from .. import Receiver, TrackState
from ...artwork import content_type_of
from . import dmap
from .alac import AlacDecoder, PcmDecoder
from .dacp import DacpClient
from .mdns import RaopAdvertisement, hardware_address
from .rtp import RtpSession
from .rtsp import RtspServer, local_ipv4

#: An em dash between artist and album is how the Apple apps have long packed both into one field.
APPLE_SPLIT = " — "

#: What a sender puts in one uncompressed packet.
FRAMES_PER_PCM_PACKET = 352

#: A sender stops sending the moment it pauses, so "playing" is a recent-packet question.
SILENCE_TIMEOUT = 1.0


class AirPlayReceiver(Receiver):
    source = "apple"

    def __init__(self, settings, hub, artwork) -> None:
        self._settings = settings
        self._hub = hub
        self._artwork = artwork

        self._track = TrackState()
        self._dacp = DacpClient(hub)
        self._mac = hardware_address()

        self._rtsp: RtspServer | None = None
        self._advertisement: RaopAdvertisement | None = None
        self._rtp: RtpSession | None = None
        self._session = None
        self._sink = None
        self._recording = False
        self._name = ""

    @property
    def available(self) -> bool:
        return True

    @property
    def reason(self) -> str:
        return ""

    @property
    def connected(self) -> bool:
        return self._recording

    @property
    def client(self) -> str:
        return self._session.client if self._session else ""

    @property
    def status_text(self) -> str:
        if self._rtsp is None:
            return "AirPlay isn't running - the RTSP server stopped"
        if not self._recording:
            return f"AirPlay speaker '{self._name}' - waiting for Apple Music"
        return f"Apple Music connected from {self._session.address}"

    def track(self) -> TrackState:
        if self._recording and self._rtp is not None:
            quiet = time.monotonic() - self._rtp.last_packet_at > SILENCE_TIMEOUT
            self._track.set_playing(not quiet)
        return self._track

    async def start(self, sink) -> None:
        self._sink = sink
        self._name = self._settings.receiverName

        self._rtsp = RtspServer(self, self._hub, self._mac)
        port = await self._rtsp.start()

        address = local_ipv4()
        self._advertisement = RaopAdvertisement(self._name, port, address, self._mac)
        await self._advertisement.start()

        self._hub.info(
            f"airplay: advertising '{self._name}' on {address}:{port} - "
            "pick it from Apple Music's AirPlay menu")

    async def stop(self) -> None:
        await self.on_teardown()

        if self._advertisement is not None:
            await self._advertisement.stop()
            self._advertisement = None

        if self._rtsp is not None:
            await self._rtsp.stop()
            self._rtsp = None

        self._sink = None
        self._track.clear()

    async def control(self, command: str) -> bool:
        advertisement = self._advertisement
        return await self._dacp.send(command, advertisement.zeroconf if advertisement else None)

    # The RTSP server calls these as the sender drives the session.

    async def on_announce(self, session) -> None:
        self._session = session
        self._dacp.bind(session.dacp_id, session.active_remote)
        self._track.clear()
        self._artwork.set(None)

    async def on_setup(self, session, client_ports: dict):
        if self._rtp is not None:
            self._rtp.close()

        alac = session.codec == "alac"
        decoder = AlacDecoder(session.fmtp) if alac else PcmDecoder()
        frames = session.fmtp[0] if alac else FRAMES_PER_PCM_PACKET
        rate = session.fmtp[-1] if alac else 44100

        self._rtp = RtpSession(session.key, session.iv, decoder, frames, rate,
                               self._deliver, self._hub)
        return await self._rtp.start()

    async def on_record(self) -> None:
        self._recording = True
        self._track.set_playing(True)

    async def on_flush(self, rtptime) -> None:
        if self._rtp is not None:
            self._rtp.flush(rtptime)

    async def on_teardown(self) -> None:
        self._recording = False
        self._session = None
        self._dacp.forget()
        self._track.set_playing(False)

        if self._rtp is not None:
            self._rtp.close()
            self._rtp = None

    async def on_metadata(self, fields: dict) -> None:
        title = fields.get(dmap.TITLE, "")
        artist = fields.get(dmap.ARTIST, "")
        album = fields.get(dmap.ALBUM, "")

        # The Apple apps have been seen packing "artist - album" into the artist field with the
        # album left empty. Splitting a field that does carry an album would corrupt both, and the
        # result is burned into the outgoing video, so this only fires when there is nothing to lose.
        if not album and APPLE_SPLIT in artist:
            artist, _, album = artist.partition(APPLE_SPLIT)
            self._hub.info("airplay: split artist and album out of one field")

        self._track.set_text(title, artist, album)

        duration = fields.get(dmap.DURATION_MS)
        if duration:
            self._track.duration = duration / 1000

    async def on_artwork(self, data: bytes) -> None:
        # An empty body is how a sender says this track has no cover.
        if not data:
            self._artwork.set(None)
            return

        if content_type_of(data) == "application/octet-stream":
            self._hub.warn("airplay: ignoring artwork in an unrecognised format")
            return

        self._artwork.set(data)

    async def on_progress(self, start: int, current: int, end: int) -> None:
        rate = self._session.fmtp[-1] if self._session and self._session.fmtp else 44100
        self._track.set_position((current - start) / rate, (end - start) / rate)

    async def on_volume(self, db: float) -> None:
        # The broadcast stays at full scale: listeners have their own volume, and unlike the
        # loopback capture this replaced, nothing here has already applied the sender's slider.
        pass

    def _deliver(self, pcm: bytes) -> None:
        if self._sink is not None:
            self._sink(pcm)
