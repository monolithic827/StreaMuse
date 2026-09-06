"""Audio frames off the wire into interleaved s16le.

ffmpeg's ALAC decoder wants the 36-byte 'alac' atom, not the bare 24-byte body the SDP describes,
so the fmtp numbers are wrapped back into one here. The TXT record also offers uncompressed audio,
which a sender may take instead, and that needs no decoder at all.
"""

import array
import struct

import av

#: fmtp fields after the payload type, in the order the sender lists them.
COOKIE_FORMAT = ">IBBBBBBHIII"

#: s16le stereo, what the decoder is asked to pack to.
BYTES_PER_FRAME = 4


def magic_cookie(fmtp: list[int]) -> bytes:
    """frameLength, compatibleVersion, bitDepth, pb, mb, kb, channels, maxRun, maxFrameBytes,
    avgBitRate, sampleRate."""
    return struct.pack(">I4sI", 36, b"alac", 0) + struct.pack(COOKIE_FORMAT, *fmtp)


class AlacDecoder:
    def __init__(self, fmtp: list[int]) -> None:
        self._context = av.CodecContext.create("alac", "r")
        self._context.extradata = magic_cookie(fmtp)
        self._context.open()
        # ALAC decodes to planar s16p; the encoder input is interleaved.
        self._packer = av.AudioResampler(format="s16", layout="stereo", rate=fmtp[-1])

    def decode(self, frame: bytes) -> bytes:
        chunks = []
        for decoded in self._context.decode(av.Packet(frame)):
            for packed in self._packer.resample(decoded):
                # A plane is allocated with alignment padding past the samples it holds, so it has
                # to be cut to the frame's own length. Sending the whole buffer appends 128 bytes
                # of silence to every packet - inaudible on long frames, a 9% overrun and a buzz at
                # the packet rate on the 352-frame packets AirPlay actually sends.
                chunks.append(bytes(packed.planes[0])[:packed.samples * BYTES_PER_FRAME])
        return b"".join(chunks)

    def flush(self) -> None:
        self._context.flush_buffers()


class PcmDecoder:
    """AirPlay's uncompressed mode is big-endian 16-bit stereo."""

    def decode(self, frame: bytes) -> bytes:
        samples = array.array("h")
        samples.frombytes(frame[:len(frame) - len(frame) % 2])
        samples.byteswap()
        return samples.tobytes()

    def flush(self) -> None:
        pass
