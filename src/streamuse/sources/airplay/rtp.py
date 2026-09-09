"""The three UDP ports a RAOP session uses: paced audio in, sync on the control port, and timing.

The sender runs about two seconds ahead of playout and front-loads that much on RECORD, so packets
are not handed straight to the pacer - its jitter buffer would shed the burst and lose the first
seconds of every play. A release cursor plays them out on their own RTP timestamps instead, and the
pacer sees a steady stream with the cushion it was built for.
"""

import asyncio
import struct
import time

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

AUDIO_PORT = 6100
CONTROL_PORT = 6101
TIMING_PORT = 6102

PAYLOAD_AUDIO = 0x60
PAYLOAD_RESEND = 0x56
PAYLOAD_TIMING_REQUEST = 0xD2

RELEASE_INTERVAL = 0.02

#: How far ahead of the cursor a packet may be released, so the pacer always has a cushion.
LEAD_SECONDS = 0.25

#: How long a gap waits for a late packet before silence stands in for it.
GAP_TOLERANCE_SECONDS = 0.1

#: How often a run of undecodable packets is allowed to say so.
UNDECODABLE_WARN_INTERVAL = 5.0

NTP_EPOCH_OFFSET = 2208988800


class _Datagram(asyncio.DatagramProtocol):
    """Bound to 0.0.0.0 on a fixed, well-known port, so anything on the LAN can reach it. Only the
    sender that negotiated this session may be heard: a second device still streaming into the same
    port gets its packets decrypted with this session's key, which is indistinguishable from noise
    and decodes to nothing but errors."""

    def __init__(self, on_packet, peer: str, on_stranger) -> None:
        self._on_packet = on_packet
        self._peer = peer
        self._on_stranger = on_stranger
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, address) -> None:
        if self._peer and address[0] != self._peer:
            self._on_stranger(address)
            return
        self._on_packet(data, address)

    def error_received(self, exc) -> None:
        pass


class RtpSession:
    """Owns the UDP sockets for one RECORD. Decrypts, decodes and releases audio to the sink."""

    def __init__(self, key: bytes, iv: bytes, decoder, frames_per_packet: int,
                 sample_rate: int, sink, hub, peer: str = "") -> None:
        self._cipher = Cipher(algorithms.AES(key), modes.CBC(iv)) if key else None
        self._decoder = decoder
        self._frames = frames_per_packet
        self._rate = sample_rate
        self._sink = sink
        self._hub = hub
        self._peer = peer

        self._undecodable = 0
        self._warned_at = 0.0
        self._strangers: set[str] = set()

        self._buffer: dict[int, bytes] = {}
        self._next_ts: int | None = None
        self._cursor_ts = 0
        self._cursor_at = 0.0
        self._flush_before: int | None = None

        self._transports: list[asyncio.DatagramTransport] = []
        self._release_task: asyncio.Task | None = None
        self.last_packet_at = 0.0

    async def start(self) -> tuple[int, int, int]:
        loop = asyncio.get_running_loop()
        ports = []

        for port, handler in (
            (AUDIO_PORT, self._on_audio),
            (CONTROL_PORT, self._on_control),
            (TIMING_PORT, self._on_timing),
        ):
            transport, _ = await loop.create_datagram_endpoint(
                lambda handler=handler: _Datagram(handler, self._peer, self._note_stranger),
                local_addr=("0.0.0.0", port))
            self._transports.append(transport)
            ports.append(transport.get_extra_info("socket").getsockname()[1])

        self._release_task = asyncio.create_task(self._release_loop())
        return tuple(ports)

    def close(self) -> None:
        if self._release_task is not None:
            self._release_task.cancel()
            self._release_task = None
        for transport in self._transports:
            transport.close()
        self._transports = []

    def flush(self, rtptime: int | None) -> None:
        """The sender pauses, seeks and skips with FLUSH; everything older than rtptime is stale."""
        self._buffer.clear()
        self._next_ts = None
        self._flush_before = rtptime
        self._decoder.flush()

    def _on_audio(self, data: bytes, _address) -> None:
        if len(data) < 16:
            return

        kind = data[1] & 0x7F
        if kind == PAYLOAD_RESEND:
            # A retransmitted packet arrives wrapped in a four-byte prefix.
            data = data[4:]
            kind = data[1] & 0x7F if len(data) >= 16 else 0

        if kind != PAYLOAD_AUDIO:
            return

        self._accept(data)

    def _on_control(self, data: bytes, _address) -> None:
        if len(data) < 4:
            return
        kind = data[1] & 0x7F
        if kind == PAYLOAD_RESEND and len(data) >= 16:
            self._accept(data[4:])
        # Sync packets carry the sender's clock; we repace on our own, so they are only a diagnostic.

    def _on_timing(self, data: bytes, address) -> None:
        if len(data) < 32 or (data[1] & 0x7F) != PAYLOAD_TIMING_REQUEST:
            return

        transport = self._transports[2] if len(self._transports) > 2 else None
        if transport is None:
            return

        now = time.time() + NTP_EPOCH_OFFSET
        seconds = int(now)
        fraction = int((now - seconds) * (1 << 32))
        reply = (b"\x80\xd3\x00\x07" + bytes(4) + data[24:32]
                 + struct.pack(">II", seconds, fraction) * 2)
        transport.sendto(reply, address)

    def _accept(self, packet: bytes) -> None:
        timestamp = int.from_bytes(packet[4:8], "big")
        marker = bool(packet[1] & 0x80)

        if self._flush_before is not None and _before(timestamp, self._flush_before):
            return
        self._flush_before = None

        payload = packet[12:]
        if self._cipher is not None:
            # Only whole blocks are encrypted; the tail rides in the clear, and the IV is reset for
            # every packet rather than chained.
            aligned = len(payload) & ~0xF
            decryptor = self._cipher.decryptor()
            payload = decryptor.update(payload[:aligned]) + decryptor.finalize() + payload[aligned:]

        try:
            pcm = self._decoder.decode(payload)
        except Exception as exc:
            self._report_undecodable(exc)
            return

        if not pcm:
            return

        self.last_packet_at = time.monotonic()
        self._buffer[timestamp] = pcm

        if self._next_ts is None or marker:
            self._start_cursor(timestamp)

    def _note_stranger(self, address) -> None:
        """Said once per address: dropping these is right when it really is a second device, but if
        a sender ever puts its audio on a different address than its RTSP connection this is the
        only thing standing between the user and silence with no explanation."""
        if address[0] in self._strangers:
            return

        self._strangers.add(address[0])
        self._hub.warn(f"airplay: ignoring audio from {address[0]} - "
                       f"this session belongs to {self._peer}")

    def _report_undecodable(self, exc: Exception) -> None:
        """Whatever makes one packet undecodable makes all of them undecodable, and a sender fills
        125 a second - one log line each, every one of them printed and fanned out to every panel
        socket. Count them and say so periodically instead."""
        self._undecodable += 1
        now = time.monotonic()
        if now - self._warned_at < UNDECODABLE_WARN_INTERVAL:
            return

        dropped, self._undecodable, self._warned_at = self._undecodable, 0, now
        self._hub.warn(f"airplay: dropped {dropped} packet(s) that would not decode ({exc})")

    def _start_cursor(self, timestamp: int) -> None:
        self._next_ts = timestamp
        self._cursor_ts = timestamp
        self._cursor_at = time.monotonic()

    async def _release_loop(self) -> None:
        while True:
            await asyncio.sleep(RELEASE_INTERVAL)
            if self._next_ts is None:
                continue

            elapsed = time.monotonic() - self._cursor_at
            horizon = self._cursor_ts + int((elapsed + LEAD_SECONDS) * self._rate)
            tolerance = int(GAP_TOLERANCE_SECONDS * self._rate)

            while not _before(horizon, self._next_ts):
                pcm = self._buffer.pop(self._next_ts, None)

                if pcm is None:
                    # Nothing behind the hole means the sender stopped rather than dropped a
                    # packet: park the cursor and pick it up wherever it resumes. The pacer fills
                    # the silence meanwhile, which is what it is for.
                    if not self._buffer:
                        self._next_ts = None
                        break

                    # A real hole. Hold the slot briefly for a reordered packet, then keep the
                    # timeline honest with silence rather than letting the stream slide early.
                    if _before(horizon, self._next_ts + tolerance):
                        break
                    pcm = bytes(self._frames * 4)

                self._sink(pcm)
                self._next_ts = (self._next_ts + self._frames) & 0xFFFFFFFF


def _before(a: int, b: int) -> bool:
    """32-bit RTP timestamps wrap, so ordering is the sign of the wrapped difference."""
    return ((a - b) & 0xFFFFFFFF) >= 0x80000000
