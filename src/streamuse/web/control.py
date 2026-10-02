"""Loopback-only control surface. Never exposed through the tunnel."""

from aiohttp import web

from .. import paths, settings as settings_module
from ..artwork import content_type_of
from ..media import hls
from ..state import dumps

IMMUTABLE = "public, max-age=31536000, immutable"
PLAYER_COMMANDS = ("playpause", "next", "prev")


def build_app(hub, deps, artwork, settings, pipeline, tunnel, sources,
              control_port: int, public_port: int) -> web.Application:
    app = web.Application(middlewares=[_panel_only(control_port)])

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
        previous_name = _advertised_name(settings)
        settings.apply(incoming)
        settings.save()

        # The stream key is part of the URL and the public endpoint reads it live, so a URL built
        # at startup 404s after a key change - the tunnel's as much as the local one.
        hub.set_local_url(hls.local_url(public_port, settings.streamKey))
        tunnel.refresh_url()
        hub.refresh()
        hub.info("settings saved - encoder changes apply on next start")

        if settings.source != previous_source:
            await sources.select(settings.source)
        elif _advertised_name(settings) != previous_name:
            # A receiver advertises the name it was started with, so the new one needs a restart
            # to reach the device pickers.
            await sources.select(settings.source, restart=True)

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

    async def player(request):
        command = request.match_info["command"]
        if command not in PLAYER_COMMANDS:
            raise web.HTTPNotFound()
        if not await sources.control(command):
            raise web.HTTPInternalServerError(text="the source is not accepting commands")
        return web.Response()

    async def source_load(request):
        try:
            query = (await request.json()).get("query", "").strip()
        except ValueError:
            raise web.HTTPBadRequest()
        if not query:
            raise web.HTTPBadRequest(text="query is empty")
        if not await sources.load(query):
            raise web.HTTPInternalServerError(text="the source is not accepting a URL - pick it first")
        return web.Response()

    async def request_open(request):
        track_id = (await request.json()).get("id") or ""
        if not await sources.open_request(track_id):
            raise web.HTTPInternalServerError(text="could not open that request")
        hub.drop_request(track_id)
        return web.Response()

    async def request_drop(request):
        hub.drop_request((await request.json()).get("id") or "")
        return web.Response()

    async def websocket(request):
        socket = web.WebSocketResponse(heartbeat=20)
        await socket.prepare(request)
        await hub.accept_socket(socket)
        return socket

    async def index(_request):
        return _file(paths.wwwroot() / "index.html", "text/html")

    app.router.add_get("/api/state", state)
    app.router.add_get("/api/art", art)
    app.router.add_post("/api/settings", save_settings)
    app.router.add_post("/api/stream/start", stream_start)
    app.router.add_post("/api/stream/stop", stream_stop)
    app.router.add_post("/api/tunnel/start", tunnel_start)
    app.router.add_post("/api/tunnel/stop", tunnel_stop)
    app.router.add_post("/api/deps/refresh", deps_refresh)
    app.router.add_post("/api/player/{command}", player)
    app.router.add_post("/api/source/load", source_load)
    app.router.add_post("/api/requests/open", request_open)
    app.router.add_post("/api/requests/drop", request_drop)
    app.router.add_get("/ws", websocket)
    app.router.add_get("/", index)
    app.router.add_static("/", paths.wwwroot())

    return app


def _advertised_name(settings) -> str:
    return {"apple": settings.receiverName,
            "spotify": settings.spotifyConnectDeviceName}.get(settings.source, "")


def _panel_only(port: int):
    """The panel is this API's one client and is always loaded from this address, so a request
    has to name it as its host and, when it says where it came from, as its origin."""
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    origins = {f"http://{host}" for host in hosts}

    @web.middleware
    async def check(request, handler):
        origin = request.headers.get("Origin")
        if request.host not in hosts or (origin is not None and origin not in origins):
            raise web.HTTPForbidden()
        return await handler(request)

    return check


def _json(payload) -> web.Response:
    return web.Response(text=dumps(payload), content_type="application/json", charset="utf-8")


def _file(path, content_type: str) -> web.Response:
    return web.Response(body=path.read_bytes(), content_type=content_type, charset="utf-8")
