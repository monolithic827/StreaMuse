"""The metadata format a sender puts in SET_PARAMETER as application/x-dmap-tagged: a flat sequence
of code(4) + length(u32 BE) + value, with a few codes holding nested items."""

import struct

_INT = {1: ">B", 2: ">H", 4: ">I", 8: ">Q"}
_STRINGS = frozenset((b"minm", b"asar", b"asal", b"asaa", b"asgn", b"ascp"))
_CONTAINERS = frozenset((b"mlit", b"mlcl", b"mdcl", b"msrv", b"cmst", b"cmgt"))

TITLE = "minm"
ARTIST = "asar"
ALBUM = "asal"
DURATION_MS = "astm"


def parse(buffer: bytes, out: dict | None = None) -> dict:
    out = {} if out is None else out
    offset = 0

    while offset + 8 <= len(buffer):
        code = buffer[offset:offset + 4]
        length = int.from_bytes(buffer[offset + 4:offset + 8], "big")
        offset += 8

        if length > len(buffer) - offset:
            break

        value = buffer[offset:offset + length]
        offset += length

        if code in _CONTAINERS:
            parse(value, out)
        elif code in _STRINGS:
            out[code.decode()] = value.decode("utf-8", "replace")
        elif length in _INT:
            out[code.decode()] = struct.unpack(_INT[length], value)[0]

    return out
