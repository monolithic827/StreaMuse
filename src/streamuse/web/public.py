"""The public port: the HLS playlist and segments, the listener page and its now-playing feed.

This is a security boundary. Everything here is GET or HEAD, lives under /live/{streamKey}/, and is
one of a short allowlist; everything else 404s. It must never serialize the state snapshot - that
carries the Cloudflare token, dependency paths that leak the Windows username, and the log - so the
public now-playing record is declared here and built field by field, and a field added to the
panel's state cannot become public by being adjacent to one.
"""

import hashlib
import re

from aiohttp import web

from .. import paths
from ..artwork import content_type_of
from ..state import RUNNING, dumps

#: The only files this port will serve, and the type each is sent as.
ASSET_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
}

#: The files the page names with a version; the page itself is not one of them.
ASSET_NAMES = ("listen.css", "listen.js")

PLAYLIST_CACHE = "no-cache, no-store, must-revalidate"
SEGMENT_CACHE = "public, max-age=3600, immutable"
IMMUTABLE = "public, max-age=31536000, immutable"

OFF_AIR = {
    "title": "", "artist": "", "album": "", "playing": False,
    "positionSeconds": 0, "durationSeconds": 0, "artworkVersion": "0", "live": False,
}

_DRIVE_RELATIVE = re.compile(r"^[A-Za-z]:")
_asset_version: str | None = None


def is_safe_name(name: str) -> bool:
    """Rejecting '/', '\\' and '..' is not enough on Windows: a join discards its first argument for
    a drive-relative name, so 'C:seg.ts' would resolve against drive C's current directory."""
    return bool(name) and not (
        "/" in name or "\\" in name or ".." in name or _DRIVE_RELATIVE.match(name))


def build_app(hub, artwork, settings, dj=None) -> web.Application:
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", _make_handler(hub, artwork, settings, dj))
    return app


def _make_handler(hub, artwork, settings, dj):
    async def handle(request: web.Request) -> web.StreamResponse:
        if request.method not in ("GET", "HEAD"):
            return _not_found()

        prefix = f"/live/{settings.streamKey}/"
        path = request.path

        # The page's own URLs are relative, so it only resolves them from the directory form. This
        # link gets pasted around by hand, and without the redirect the slashless form is a 404.
        if path == prefix[:-1]:
            raise web.HTTPFound(prefix)

        if not path.startswith(prefix):
            return _not_found()

        name = path[len(prefix):]

        if name == "":
            return _serve_asset(request, "listen.html")
        if name == "now":
            return _serve_now(request, hub, dj)
        if name == "art":
            return _serve_art(request, hub, artwork, dj)
        if name.lower().endswith((".m3u8", ".ts")):
            return _serve_hls(request, name)
        return _serve_asset(request, name)

    return handle


def _send(request: web.Request, body: bytes, content_type: str, cache: str,
          extra: dict | None = None) -> web.Response:
    """A HEAD that 404s where GET returns 200 makes a pasted link look dead to the unfurlers in
    chat apps, so both methods answer identically and only the body is withheld."""
    headers = {"Cache-Control": cache}
    if extra:
        headers.update(extra)

    # The body is always attached so Content-Length matches; aiohttp drops it for HEAD itself.
    return web.Response(
        body=body,
        content_type=content_type.split(";")[0].strip(),
        charset="utf-8" if "charset" in content_type else None,
        headers=headers,
    )


def _not_found() -> web.Response:
    return web.Response(status=404, text="Not found")


def _serve_hls(request: web.Request, name: str) -> web.StreamResponse:
    if not is_safe_name(name):
        return _not_found()

    file = paths.HLS_DIR / name
    try:
        body = file.read_bytes()
    except OSError:
        return _not_found()

    playlist = name.lower().endswith(".m3u8")
    return _send(
        request, body,
        "application/vnd.apple.mpegurl" if playlist else "video/mp2t",
        PLAYLIST_CACHE if playlist else SEGMENT_CACHE,
        {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*"},
    )


def _serve_asset(request: web.Request, name: str) -> web.StreamResponse:
    if not is_safe_name(name):
        return _not_found()

    suffix = ("." + name.rsplit(".", 1)[-1]).lower() if "." in name else ""
    content_type = ASSET_TYPES.get(suffix)
    if content_type is None:
        return _not_found()

    # Confined to the listen/ subtree, or the panel's own index.html and app.js - siblings in the
    # same folder - would be reachable through the tunnel.
    file = paths.wwwroot() / "listen" / name
    try:
        body = file.read_bytes()
    except OSError:
        return _not_found()

    page = suffix == ".html"
    if page:
        body = body.replace(b"{v}", _version().encode())

    return _send(request, body, content_type, "no-cache" if page else IMMUTABLE)


def _version() -> str:
    """Content-derived, so a rebuild changes the URL the page asks for. Cloudflare rewrites
    Cache-Control on .css and .js, so an asset cannot be retired by a response header."""
    global _asset_version
    if _asset_version is None:
        digest = hashlib.sha256()
        for name in ASSET_NAMES:
            digest.update((paths.wwwroot() / "listen" / name).read_bytes())
        _asset_version = digest.hexdigest()[:12].upper()
    return _asset_version


def _serve_now(request: web.Request, hub, dj) -> web.Response:
    # The DJ track is already what the video shows once it's mixing (see CoverFrameRenderer), so
    # this has to agree with it - otherwise a listener combining the two sees a paused source's
    # metadata under audibly different, currently-playing audio.
    dj_state = dj.snapshot() if dj is not None else None
    playing_track = dj_state.nowMixing if dj_state is not None else None

    if hub.encoder.status == RUNNING and playing_track is not None:
        payload = {
            "title": playing_track.title,
            "artist": playing_track.artist,
            "album": dj_state.album,
            "playing": True,
            "positionSeconds": dj_state.positionSeconds,
            "durationSeconds": dj_state.durationSeconds,
            "artworkVersion": str(dj_state.artworkVersion),
            "live": True,
        }
    elif hub.encoder.status == RUNNING:
        now = hub.now_playing
        payload = {
            "title": now.title,
            "artist": now.artist,
            "album": now.album,
            "playing": now.playing,
            "positionSeconds": now.positionSeconds,
            "durationSeconds": now.durationSeconds,
            # A 63-bit version is rounded by JSON.parse past 2^53, and the v= that came back would
            # not be the version the host sent.
            "artworkVersion": str(now.artworkVersion),
            "live": True,
        }
    else:
        payload = OFF_AIR

    return _send(request, dumps(payload).encode(), "application/json; charset=utf-8", "no-store")


def _serve_art(request: web.Request, hub, artwork, dj) -> web.StreamResponse:
    # The tunnel outlives the encoder, so with the stream stopped this must answer nothing at all -
    # otherwise anyone holding the hostname could poll what the machine plays locally.
    if hub.encoder.status != RUNNING:
        return _not_found()

    dj_state = dj.snapshot() if dj is not None else None
    if dj_state is not None and dj_state.nowMixing is not None:
        version, data = dj.artwork.current
    else:
        version, data = artwork.current

    if not data:
        return _not_found()

    # The cover can change between the poll that named a version and the fetch for it; caching
    # those bytes under the old key would pin the wrong cover for a year.
    cache = IMMUTABLE if request.query.get("v") == str(version) else "no-store"

    return _send(request, data, content_type_of(data), cache,
                 {"X-Content-Type-Options": "nosniff"})
