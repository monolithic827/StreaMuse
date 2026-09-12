"""The public port: the HLS playlist and segments, the listener page and its now-playing feed.

This is a security boundary. Everything here lives under /live/{streamKey}/ and is one of a short
allowlist; everything else 404s. It must never serialize the state snapshot - that carries the
Cloudflare token, dependency paths that leak the Windows username, and the log - so the public
now-playing record is declared here and built field by field, and a field added to the panel's
state cannot become public by being adjacent to one.

Song requests are the one write, and the only POST: `request` alone answers it, and both it and
`search` need the host to have turned songRequests on and the encoder to be running, or they 404
like everything else that is not on the list.
"""

import hashlib
import re
import time

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
    "requests": "",
}

#: One spammer must not be what gets the host's account throttled by Spotify or Apple.
SEARCH_COOLDOWN = 3.0
QUEUE_COOLDOWN = 60.0

MAX_QUERY = 120

_DRIVE_RELATIVE = re.compile(r"^[A-Za-z]:")
_asset_version: str | None = None


def is_safe_name(name: str) -> bool:
    """Rejecting '/', '\\' and '..' is not enough on Windows: a join discards its first argument for
    a drive-relative name, so 'C:seg.ts' would resolve against drive C's current directory."""
    return bool(name) and not (
        "/" in name or "\\" in name or ".." in name or _DRIVE_RELATIVE.match(name))


class Cooldown:
    """One listener at a time, per address. This port is bound to loopback, so every connection
    arrives from cloudflared and request.remote is always 127.0.0.1; CF-Connecting-IP is set by
    Cloudflare itself and overwrites whatever the client sent, so it is the one usable identity."""

    def __init__(self, seconds: float) -> None:
        self._seconds = seconds
        self._seen: dict[str, float] = {}

    def take(self, request: web.Request) -> bool:
        who = request.headers.get("CF-Connecting-IP") or request.remote or ""
        now = time.monotonic()

        # Pruned on every call, so the table cannot grow past the addresses inside one window.
        self._seen = {k: v for k, v in self._seen.items() if now - v < self._seconds}

        if who in self._seen:
            return False

        self._seen[who] = now
        return True


def build_app(hub, artwork, settings, sources) -> web.Application:
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", _make_handler(hub, artwork, settings, sources))
    return app


def _make_handler(hub, artwork, settings, sources):
    searches = Cooldown(SEARCH_COOLDOWN)
    queues = Cooldown(QUEUE_COOLDOWN)

    async def handle(request: web.Request) -> web.StreamResponse:
        if request.method not in ("GET", "HEAD", "POST"):
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

        if request.method == "POST":
            if name != "request" or not _requests_open(hub, settings):
                return _not_found()
            return await _serve_request(request, hub, sources, queues)

        if name == "":
            return _serve_asset(request, "listen.html")
        if name == "now":
            return _serve_now(request, hub, settings, sources)
        if name == "art":
            return _serve_art(request, hub, artwork)
        if name == "search":
            if not _requests_open(hub, settings):
                return _not_found()
            return await _serve_search(request, sources, searches)
        if name.lower().endswith((".m3u8", ".ts")):
            return _serve_hls(request, name)
        return _serve_asset(request, name)

    return handle


def _requests_open(hub, settings) -> bool:
    # Off air the whole public surface goes quiet, and requests are no exception: with no stream
    # there is no audience to be requesting for.
    return bool(settings.songRequests) and hub.encoder.status == RUNNING


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


def _json(payload: dict, status: int = 200) -> web.Response:
    return web.Response(status=status, text=dumps(payload), content_type="application/json",
                        charset="utf-8", headers={"Cache-Control": "no-store"})


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


def _serve_now(request: web.Request, hub, settings, sources) -> web.Response:
    now = hub.now_playing

    if hub.encoder.status == RUNNING:
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
            # "queue", "play" or "" - what the button will do, so the page can say which. The page
            # supplies the wording; this is only ever one of the three.
            "requests": sources.request_action if settings.songRequests else "",
        }
    else:
        payload = OFF_AIR

    return _send(request, dumps(payload).encode(), "application/json; charset=utf-8", "no-store")


async def _serve_search(request: web.Request, sources, searches: Cooldown) -> web.Response:
    query = (request.query.get("q") or "").strip()
    if not query or len(query) > MAX_QUERY:
        return _json({"error": "Type something to search for."}, 400)

    if not searches.take(request):
        return _json({"error": "One search at a time - try again in a moment."}, 429)

    try:
        found = await sources.search(query)
    except LookupError as exc:
        return _json({"error": str(exc)}, 400)

    if found is None or not found.id:
        return _json({"found": False})

    return _json({
        "found": True,
        "id": found.id,
        "title": found.title,
        "artist": found.artist,
        "album": found.album,
        "artUrl": found.artUrl,
    })


async def _serve_request(request: web.Request, hub, sources, queues: Cooldown) -> web.Response:
    try:
        body = await request.json()
        track_id = str(body["id"])
        # Straight from the listener and headed for the log, so anything that could forge a second
        # line - or any other control character - comes out first.
        title = "".join(c for c in str(body.get("title") or "") if c.isprintable())[:80]
    except (ValueError, KeyError, TypeError):
        return _json({"error": "Bad request."}, 400)

    if not queues.take(request):
        return _json({"error": "You have already requested a song - give it a minute."}, 429)

    if not await sources.enqueue(track_id):
        return _json({"error": "The source would not take that one."}, 503)

    # The host is handing the audience a lever on their own playback, so what it did goes in the log.
    hub.info(f"request: {title or track_id}")
    return _json({"queued": True})


def _serve_art(request: web.Request, hub, artwork) -> web.StreamResponse:
    # The tunnel outlives the encoder, so with the stream stopped this must answer nothing at all -
    # otherwise anyone holding the hostname could poll what the machine plays locally.
    if hub.encoder.status != RUNNING:
        return _not_found()

    version, data = artwork.current
    if not data:
        return _not_found()

    # The cover can change between the poll that named a version and the fetch for it; caching
    # those bytes under the old key would pin the wrong cover for a year.
    cache = IMMUTABLE if request.query.get("v") == str(version) else "no-store"

    return _send(request, data, content_type_of(data), cache,
                 {"X-Content-Type-Options": "nosniff"})
