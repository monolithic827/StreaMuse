"""Current album art. The version tells the renderer when to rebuild and is the cache key the
panel and the listener page fetch the cover with."""

import hashlib
import threading


class ArtworkStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._bytes: bytes | None = None
        self._version = 0

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    @property
    def bytes(self) -> bytes | None:
        with self._lock:
            return self._bytes

    @property
    def current(self) -> tuple[int, bytes | None]:
        """Both together, for a caller that caches the bytes under the version: read apart, a track
        change between them pins one cover under the other's key."""
        with self._lock:
            return self._version, self._bytes

    def set(self, data: bytes | None) -> bool:
        """Returns True when the artwork actually changed."""
        version = _fingerprint(data) if data else 0

        with self._lock:
            if version == self._version:
                return False
            self._bytes = data
            self._version = version
            return True


def content_type_of(data: bytes) -> str:
    """The sender hands over whatever its own library held, so the type has to come from the bytes.
    Takes them rather than reading the store, or a track change between the two reads would label
    one image with another's type."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:2] == b"\x89P":
        return "image/png"
    if len(data) >= 12 and data[8:10] == b"WE":
        return "image/webp"
    if data[:2] == b"GI":
        return "image/gif"
    return "application/octet-stream"


def _fingerprint(data: bytes) -> int:
    """Content-derived, 63-bit and forced odd: a counter restarting each run served the previous
    run's covers out of the browser's persistent cache, and the panel reads 0 as "no artwork"."""
    digest = hashlib.sha256(data).digest()
    return (int.from_bytes(digest[:8], "little") & 0x7FFFFFFFFFFFFFFF) | 1
