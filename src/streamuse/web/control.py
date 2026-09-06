"""Loopback-only control surface. Never exposed through the tunnel."""

from dataclasses import asdict

from aiohttp import web

from .. import paths, settings as settings_module
from ..artwork import content_type_of
from ..media import hls
from ..sources.device.receiver import list_devices
from ..state import dumps

IMMUTABLE = "public, max-age=31536000, immutable"
PLAYER_COMMANDS = ("playpause", "next", "prev")


def build_app(hub, deps, artwork, settings, pipeline, tunnel, sources, dj,
             public_port: int) -> web.Application:
    app = web.Application()

    async def state(_request):
        return _json(hub.snapshot())

    async def art(_request):
        # Versioned by the caller so the browser can cache each cover forever.
        data = artwork.bytes
        if not data:
            raise web.HTTPNotFound()
        return web.Response(body=data, content_type=content_type_of(data),
                            headers={"Cache-Control": IMMUTABLE})

    async def save_settings(request):
        try:
            incoming = settings_module.from_dict(await request.json())
        except ValueError:
            raise web.HTTPBadRequest()

        previous_source = settings.source
        settings.apply(incoming)
        settings.save()

        # The stream key is part of the URL and the public endpoint reads it live, so a URL built
        # at startup 404s after a key change. This also rebroadcasts the saved settings.
        hub.set_local_url(hls.local_url(public_port, settings.streamKey))
        hub.info("settings saved - encoder changes apply on next start")

        if settings.source != previous_source:
            await sources.select(settings.source)

        return _json(settings.to_dict())

    async def stream_start(_request):
        if await pipeline.start():
            return web.Response()
        raise web.HTTPInternalServerError(text=hub.encoder.error or "could not start the stream")

    async def stream_stop(_request):
        await pipeline.stop()
        return web.Response()

    async def tunnel_start(_request):
        if await tunnel.start():
            return web.Response()
        raise web.HTTPInternalServerError(text=hub.tunnel.error or "could not start the tunnel")

    async def tunnel_stop(_request):
        await tunnel.stop()
        return web.Response()

    async def deps_refresh(_request):
        await deps.ensure_all()
        return web.Response()

    async def devices(_request):
        return _json(list_devices())

    async def player(request):
        command = request.match_info["command"]
        if command not in PLAYER_COMMANDS:
            raise web.HTTPNotFound()
        if not await sources.control(command):
            raise web.HTTPInternalServerError(text="the source is not accepting commands")
        return web.Response()

    async def websocket(request):
        socket = web.WebSocketResponse(heartbeat=20)
        await socket.prepare(request)
        await hub.accept_socket(socket)
        return socket

    async def index(_request):
        return _file(paths.wwwroot() / "index.html", "text/html")

    async def dj_page(_request):
        return _file(paths.wwwroot() / "dj.html", "text/html")

    async def dj_request(request):
        try:
            body = await request.json()
        except ValueError:
            raise web.HTTPBadRequest()

        query = str(body.get("query", "") or "").strip()
        if not query:
            raise web.HTTPBadRequest()

        video_id = str(body.get("videoId") or "").strip() or None

        accepted, error, entry = dj.request(query, video_id)
        if not accepted:
            raise web.HTTPBadRequest(
                text=dumps({"detail": error or "could not queue that request"}),
                content_type="application/json")
        return _json(asdict(entry))

    async def dj_search(request):
        try:
            body = await request.json()
        except ValueError:
            raise web.HTTPBadRequest()

        query = str(body.get("query", "") or "").strip()
        if not query:
            return _json([])

        try:
            results = await dj.search(query)
        except Exception as exc:
            raise web.HTTPInternalServerError(text=str(exc))

        return _json([{"videoId": r.video_id, "title": r.title, "artist": r.artist,
                       "thumbnail": r.thumbnail} for r in results])

    async def dj_skip(_request):
        dj.skip()
        return web.Response()

    async def dj_art(_request):
        data = dj.artwork.bytes
        if not data:
            raise web.HTTPNotFound()
        return web.Response(body=data, content_type=content_type_of(data),
                            headers={"Cache-Control": IMMUTABLE})

    app.router.add_get("/api/state", state)
    app.router.add_get("/api/art", art)
    app.router.add_post("/api/settings", save_settings)
    app.router.add_post("/api/stream/start", stream_start)
    app.router.add_post("/api/stream/stop", stream_stop)
    app.router.add_post("/api/tunnel/start", tunnel_start)
    app.router.add_post("/api/tunnel/stop", tunnel_stop)
    app.router.add_post("/api/deps/refresh", deps_refresh)
    app.router.add_get("/api/devices", devices)
    app.router.add_post("/api/player/{command}", player)
    app.router.add_post("/api/dj/request", dj_request)
    app.router.add_post("/api/dj/search", dj_search)
    app.router.add_post("/api/dj/skip", dj_skip)
    app.router.add_get("/api/dj/art", dj_art)
    app.router.add_get("/ws", websocket)
    app.router.add_get("/", index)
    app.router.add_get("/dj", dj_page)
    app.router.add_static("/", paths.wwwroot())

    return app


def _json(payload) -> web.Response:
    return web.Response(text=dumps(payload), content_type="application/json", charset="utf-8")


def _file(path, content_type: str) -> web.Response:
    return web.Response(body=path.read_bytes(), content_type=content_type, charset="utf-8")
