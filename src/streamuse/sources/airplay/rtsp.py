"""The RTSP conversation a RAOP sender drives: handshake, session setup, and the metadata it pushes
as it plays."""

import asyncio
import base64
import ipaddress
import socket

from . import dmap, keys

RTSP_PORT = 5100
SERVER = "AirTunes/105.1"

#: What the sender is told to expect between sending audio and hearing it.
AUDIO_LATENCY = 11025

PUBLIC_METHODS = ("ANNOUNCE, SETUP, RECORD, PAUSE, FLUSH, TEARDOWN, OPTIONS, "
                  "GET_PARAMETER, SET_PARAMETER")


class SessionInfo:
    def __init__(self) -> None:
        self.key = b""
        self.iv = b""
        self.fmtp: list[int] = []
        self.codec = "alac"
        self.client = ""
        self.dacp_id = ""
        self.active_remote = ""
        self.address = ""


class RtspServer:
    """One sender at a time; a second gets 453."""

    def __init__(self, handler, hub, mac: bytes) -> None:
        self._handler = handler
        self._hub = hub
        self._mac = mac
        self._server: asyncio.Server | None = None
        self._owner: object | None = None
        self.port = RTSP_PORT

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._serve, "0.0.0.0", RTSP_PORT)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        try:
            await self._server.wait_closed()
        except Exception:
            pass
        self._server = None

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connection = object()
        session = SessionInfo()
        session.address = writer.get_extra_info("peername")[0]

        try:
            while True:
                request = await _read_request(reader)
                if request is None:
                    break

                method, headers, body = request
                response = await self._dispatch(connection, session, method, headers, body, writer)
                writer.write(response)
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception as exc:
            self._hub.warn(f"airplay: rtsp session ended ({exc})")
        finally:
            if self._owner is connection:
                self._owner = None
                await self._handler.on_teardown()
            writer.close()

    async def _dispatch(self, connection, session: SessionInfo, method: str,
                        headers: dict, body: bytes, writer) -> bytes:
        extra: dict[str, str] = {}
        payload = b""
        content_type = None

        challenge = headers.get("apple-challenge")
        if challenge:
            local_ip = writer.get_extra_info("sockname")[0]
            extra["Apple-Response"] = self._apple_response(challenge, local_ip)

        for name, attribute in (("dacp-id", "dacp_id"), ("active-remote", "active_remote"),
                                ("user-agent", "client")):
            if headers.get(name):
                setattr(session, attribute, headers[name])

        if method == "OPTIONS":
            extra["Public"] = PUBLIC_METHODS

        elif method == "ANNOUNCE":
            if self._owner is not None and self._owner is not connection:
                return _response("453 Not Enough Bandwidth", headers, {})
            if not _parse_sdp(session, body):
                # Nothing is set up yet, so the session must not stay claimed by this connection.
                self._owner = None
                return _response("456 Header Field Not Valid", headers, {})
            self._owner = connection
            self._hub.info(f"airplay: {session.client or 'a sender'} announced a stream")
            await self._handler.on_announce(session)

        elif method == "SETUP":
            if self._owner is not connection:
                return _response("455 Method Not Valid In This State", headers, {})
            ports = await self._handler.on_setup(session, _transport_ports(headers))
            if ports is None:
                return _response("461 Unsupported Transport", headers, {})
            server_port, control_port, timing_port = ports
            extra["Transport"] = (
                "RTP/AVP/UDP;unicast;interleaved=0-1;mode=record;"
                f"server_port={server_port};control_port={control_port};timing_port={timing_port}")
            extra["Session"] = "1"

        elif method == "RECORD":
            extra["Audio-Latency"] = str(AUDIO_LATENCY)
            await self._handler.on_record()

        elif method == "FLUSH":
            await self._handler.on_flush(_rtptime(headers))

        elif method == "TEARDOWN":
            extra["Connection"] = "close"
            if self._owner is connection:
                self._owner = None
            await self._handler.on_teardown()

        elif method == "GET_PARAMETER":
            if b"volume" in body:
                content_type = "text/parameters"
                payload = b"volume: 0.000000\r\n"

        elif method == "SET_PARAMETER":
            await self._set_parameter(headers, body)

        return _response("200 OK", headers, extra, payload, content_type)

    async def _set_parameter(self, headers: dict, body: bytes) -> None:
        kind = (headers.get("content-type") or "").lower()

        if kind.startswith("text/parameters"):
            for line in body.decode("utf-8", "replace").splitlines():
                name, _, value = line.partition(":")
                name, value = name.strip().lower(), value.strip()
                if name == "volume":
                    await self._handler.on_volume(_float(value))
                elif name == "progress":
                    parts = [p.strip() for p in value.split("/")]
                    if len(parts) == 3:
                        await self._handler.on_progress(*(int(p) for p in parts))

        elif kind.startswith("application/x-dmap-tagged"):
            await self._handler.on_metadata(dmap.parse(body))

        elif kind.startswith("image/"):
            await self._handler.on_artwork(body)

    def _apple_response(self, challenge: str, local_ip: str) -> str:
        payload = _b64decode(challenge)
        payload += ipaddress.ip_address(local_ip).packed
        payload += self._mac
        payload = payload.ljust(32, b"\x00")
        return base64.b64encode(keys.sign_challenge(payload)).decode().rstrip("=")


async def _read_request(reader: asyncio.StreamReader):
    head = await reader.readuntil(b"\r\n\r\n")
    lines = head.decode("utf-8", "replace").split("\r\n")
    if not lines or not lines[0]:
        return None

    method = lines[0].split(" ", 1)[0].upper()
    headers = {}
    for line in lines[1:]:
        name, separator, value = line.partition(":")
        if separator:
            headers[name.strip().lower()] = value.strip()

    length = int(headers.get("content-length") or 0)
    body = await reader.readexactly(length) if length else b""
    return method, headers, body


def _response(status: str, request_headers: dict, extra: dict,
              body: bytes = b"", content_type: str | None = None) -> bytes:
    lines = [
        f"RTSP/1.0 {status}",
        f"CSeq: {request_headers.get('cseq', '0')}",
        f"Server: {SERVER}",
        "Audio-Jack-Status: connected; type=analog",
    ]
    lines += [f"{name}: {value}" for name, value in extra.items()]

    if content_type:
        lines.append(f"Content-Type: {content_type}")
    lines.append(f"Content-Length: {len(body)}")

    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


def _parse_sdp(session: SessionInfo, body: bytes) -> bool:
    for line in body.decode("utf-8", "replace").splitlines():
        if not line.startswith("a="):
            continue
        name, _, value = line[2:].partition(":")

        if name == "rsaaeskey":
            try:
                session.key = keys.unwrap_aes_key(_b64decode(value))
            except Exception:
                return False
        elif name == "aesiv":
            session.iv = _b64decode(value)
        elif name == "fmtp":
            session.fmtp = [int(p) for p in value.split()[1:]]
        elif name == "rtpmap":
            session.codec = "alac" if "applelossless" in value.lower() else "pcm"

    if session.codec == "alac" and len(session.fmtp) != 11:
        return False
    return not session.key or len(session.key) == 16


def _transport_ports(headers: dict) -> dict:
    ports = {}
    for part in (headers.get("transport") or "").split(";"):
        name, separator, value = part.partition("=")
        if separator and value.isdigit():
            ports[name.strip().lower()] = int(value)
    return ports


def _rtptime(headers: dict) -> int | None:
    for part in (headers.get("rtp-info") or "").split(";"):
        name, separator, value = part.partition("=")
        if separator and name.strip().lower() == "rtptime" and value.isdigit():
            return int(value)
    return None


def _b64decode(value: str) -> bytes:
    """AirPlay sends base64 without its padding."""
    return base64.b64decode(value + "=" * (-len(value) % 4))


def _float(value: str) -> float:
    try:
        return float(value)
    except ValueError:
        return 0.0


def local_ipv4() -> str:
    """The address the sender will be told to send audio to, and the one signed into
    Apple-Response. A UDP connect picks the interface that actually routes off-box."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("8.8.8.8", 53))
            return probe.getsockname()[0]
        except OSError:
            return "127.0.0.1"
