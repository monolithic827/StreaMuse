# CLAUDE.md

This file provides guidance to AI coding agents when working with code in this repository.

## What this is

StreaMuse **is the speaker**. It advertises itself as an AirPlay device that Apple Music streams to,
and as a Spotify Connect device that the Spotify app hands playback to, then re-streams what arrives
as an HLS (`.m3u8`) stream published through a Cloudflare tunnel. A third source, YouTube and
SoundCloud through yt-dlp, is not a receiver at all - nothing connects to us, a URL or search typed
into the panel is resolved and decoded on request. One Python package hosts everything: two aiohttp
servers, the receivers, the encoder pipeline, and a pywebview window showing the control panel.
Windows-only by design.

Because audio, metadata and artwork all arrive over the same session, a track can never be
attributed to the wrong source - which is the whole reason for the receiver design over the WASAPI
process-loopback capture it replaced.

## Style

No AI slop. Write what a careful engineer on this codebase would write and nothing more: no defensive
scaffolding for cases that cannot happen, no abstraction with one caller, no options or hooks for
imagined future needs. Match the naming and idiom already around you instead of importing a house
style.

Comments are a last resort. Code needing one usually wants a better name or a smaller function first.
The ones that earn their place say *why*: a constraint that isn't visible locally, a workaround for
someone else's bug, an order that looks arbitrary but isn't. Never restate the line above, never
narrate a change, never leave commented-out code.

Anything that generalises past a single line - an invariant, a trap, why a fix looks strange - goes in
this file instead, so it's read once at the start rather than found by luck. Keep it short and
factual, and extend an existing section rather than adding one.

## Commands

```powershell
uv sync                                        # install
uv run streamuse                               # run

uv run streamuse --test-receiver apple 20      # run one receiver alone for 20s and report
uv run streamuse --test-receiver spotify 20

uv run pyinstaller streamuse.spec --noconfirm  # dist/StreaMuse.exe
```

`.github/workflows/build.yml` produces that same one-file exe. `wwwroot` is data rather than code, so
the spec adds it explicitly and `paths.wwwroot()` resolves it identically from a checkout and from the
unpacked bundle; editing the panel needs no rebuild when running from source.

**The exe ships ffmpeg and cloudflared inside it**, so streaming works offline and on first launch -
which is the point, and why the release build must not silently produce an exe without them. CI
stages the two into `vendor/bin` (gitignored) and *fails* if either is missing; the spec adds
whatever is staged as `datas` under `bin/`, and `paths.bundled_bin()` is the first place
`deps.resolve` looks. A local `pyinstaller` run with nothing staged still builds - the result just
falls back to downloading, which is what a source checkout does anyway.

**go-librespot is downloaded, never bundled**, by every install alike. It is the one dependency with
no upstream Windows build we can use, so `.github/workflows/go-librespot.yml` builds the patched one
and publishes it under its own `go-librespot-{ref}` tag; `deps.GO_LIBRESPOT_REF` names the same
upstream release and builds the URL from it, so the two must move together. Keeping it out of the exe
is deliberate: the app build then neither waits on that job nor can ship a stale copy, and one
publish fixes Spotify for everyone already holding an exe. Nothing triggers that workflow on an
ordinary push - run it by hand when the patch or the ref changes.

**yt-dlp is downloaded too, and for the same reason**, straight from upstream's own
`releases/latest/download/yt-dlp.exe` - no tag of ours, nothing to build. It is the only dependency
whose *staleness* is a functional bug rather than a missed improvement, since its extractors break
whenever YouTube changes, so bundling it or pinning a ref would guarantee the one failure mode that
matters. See the yt-dlp section for what that costs. Downloading `latest` is only half of it:
`resolve()` is satisfied by any copy at all, so the one fetched on first launch was the one used
forever. `ensure_all` therefore runs `yt-dlp -U` on the copy in `BIN_DIR` - its own updater, about
1.5 s when there is nothing to do - and leaves one found on PATH alone, since that is somebody
else's install. A failed check only warns; the existing exe keeps working.

There is **no test project**. Verification is done by running the app and checking real behaviour.

**Never launch the app as a background job from an agent or IDE shell.** Doing so once took the whole
VS Code instance down with it when the process was reaped: the app died without running `_shutdown`
(its log ends mid-session with no teardown lines), and the editor went with it. The exact mechanism
was never pinned down - `jobs.py` is not it, since that only ever adopts our own children and they
were confirmed dead and cleaned up - so treat this as a rule rather than something to reason around.
Either have the human start it (`! uv run streamuse` from the Claude Code prompt puts its output in
the conversation), or run it in the foreground with an explicit timeout and stop it yourself before
the turn ends. Stopping the *stream* and the *tunnel* over the API is safe and unrelated.

Useful techniques used in practice:

- `curl http://127.0.0.1:7788/api/state` - full state as JSON; poll it in a loop to catch flapping.
- Console errors in the panel: `msedge --headless=new --virtual-time-budget=4000 --dump-dom
  http://127.0.0.1:7788/` and grep stderr for `Uncaught`.
- Screenshot the window with `PrintWindow(hwnd, hdc, 2)` - `PW_RENDERFULLCONTENT` is required to
  capture the web view, the capturing script must be DPI-aware, and the handle has to come from the
  process's `MainWindowHandle` (`FindWindow` by title does not find it).
- A synthetic RAOP sender is the fastest way to exercise the whole Apple path without Apple Music:
  drive the RTSP handshake, then push AES-encrypted ALAC packets over UDP. That is how the receiver
  was verified end to end.
- `--test-receiver` answers "why is the stream silent" fastest: it reports whether a sender ever
  connected, what it said, peak level, silence share and drift from wall clock, and writes a WAV.

## Architecture

```
AirPlayReceiver  mDNS _raop._tcp ─ RTSP :5100 ─ RTP udp 6100-6102 ─┐
SpotifyReceiver  go-librespot.exe ─ \\.\pipe\streamuse-spotify ────┤ only the selected one runs
                 + its HTTP API on loopback                        │
YtDlpReceiver    panel URL/search ─ yt-dlp extract ─ ffmpeg decode ┘
                                                                   ▼
                          track + artwork ──> StateHub ──> WebSocket ──> control panel
                          PCM s16le 44.1k ──> LevelMeter + AudioPacer ─┐
                          CoverFrameRenderer ────────────> VideoPacer ─┤ one shared Clock
                                                                       ▼
                    tcp://127.0.0.1:{audio,video} ──> ffmpeg ──> %LOCALAPPDATA%\StreaMuse\hls
                                                                       │
                                     :7789 public app <── cloudflared <┘
                                     :7788 control app <── pywebview window
```

`StateHub` is the single source of truth. Everything the UI shows arrives in one snapshot pushed
over the WebSocket; the panel is a pure view and only ever posts intents back (start/stop, settings,
transport). When adding UI data, put it in the snapshot rather than adding a poll endpoint.

**Both receivers deliver interleaved s16le at 44.1 kHz**, which is what `sources.SAMPLE_RATE`, the
pacer and ffmpeg's audio input are set to. The encoder still emits 48 kHz AAC, so the output contract
is unchanged; ffmpeg does the conversion. Do not resample in Python - the pacer counts frames of the
*source* rate against wall clock, and a second rate to keep honest buys nothing.

**Two ports, and this is a security boundary.** The control API, WebSocket, settings and log live on
the control port (7788) and must never appear on the other one. Only the public port (7789) is handed
to cloudflared, and everything it serves sits under `/live/{streamKey}/` and is one of: the HLS
playlist and segments, the listener page's own files out of `wwwroot/listen/`, `now` (current track
as JSON), `art`, `search` and `request`. Everything else 404s, including traversal attempts. They
are two separate aiohttp applications on two runners, so the boundary is structural rather than a
guard.

`request` is the **one write on the public port and the only POST**, and no other name answers one.
It and `search` both need `songRequests` on *and* the encoder running, or they 404 like anything
else that is not on the list - so the default install exposes exactly what it always did. Each is
rate limited per address by a `Cooldown`, because one spammer must not be what gets the host's
Spotify account throttled or the machine blocked by Apple. The address is `CF-Connecting-IP`: this
port is bound to loopback, so `request.remote` is always cloudflared, and Cloudflare overwrites that
header itself rather than passing the client's. Nothing a listener sends is trusted further than a
strict id match in the receiver (`spotify:track:` plus 22 alphanumerics, or an all-digit trackId),
and the title that rides along for the log has its non-printables stripped so it cannot forge a
second log line.

The public surface must never serialize the state snapshot. That snapshot carries
`namedTunnelToken` - a Cloudflare credential - along with dependency paths that leak the Windows
username and 200 log lines. `web/public.py` therefore declares its own now-playing record and builds
it field by field, so a field added to the panel's state cannot become public by being adjacent to
one. Title, artist, album and cover are already rendered into the video, which is why those are the
ones it may carry - and only *while the video exists*. The tunnel's lifetime is independent of the
encoder's (separate buttons, plus `autoTunnel`), so with the stream stopped `now` answers a fixed
off-air record and `art` 404s; otherwise anyone holding the hostname could poll what the machine
plays locally. Do not move that gate into the page: `streamKey` defaults to a constant, so the URL is
not a secret either.

The listener page needs to know what its request button will do, so `now` carries `requests` as
`"queue"`, `"ask"` or `""` - the receiver's own `request_action`, never wording. It does tell the
public which service the host streams from; that is the minimum needed for the button to be honest
about interrupting, and the host opted in by enabling the feature.

Nothing on the wire carries a display placeholder. `NowPlaying` holds `""` for a field the source did
not report, and each of the three views - the panel, the video, the listener page - supplies its own
text. A sentinel like `"Nothing playing"` reaching the browser makes a panel-only string into
something the listener page has to string-match, and renaming it there would silently show it as a
track title.

`web/public.is_safe_name` guards every public filename. Rejecting `/`, `\` and `..` is not enough on
Windows: a path join discards its first argument for a drive-relative name, so `C:seg.ts` would
resolve against drive C's current directory. The listener's asset lookup is additionally confined to
the `listen/` subtree and to `.html`/`.css`/`.js`, or the control panel's own `index.html` and
`app.js` - siblings in the same folder - would be reachable through the tunnel. `art` is the one
public body whose type is *guessed* - `content_type_of` sniffs bytes a third-party app sent us and
falls back to `application/octet-stream` - so it is sent `nosniff`; it is same-origin with the page
out there.

Both public handlers answer HEAD as well as GET. A page link gets pasted into chat apps whose
unfurlers probe with HEAD first, and a HEAD that 404s where GET returns 200 makes the link look dead.
`_send` keeps the two identical by always attaching the body; aiohttp sets `Content-Length` from it
and drops the body for HEAD itself. Never set `content_length` by hand there - aiohttp raises.

**Cloudflare overrides `Cache-Control` on `.css` and `.js`.** Measured through the named tunnel, a
`no-cache` on those came back to the client as `max-age=14400`, so a response header cannot be relied
on to retire an asset: after a rebuild, listeners would run the previous build's script against this
build's feed for up to four hours. The page therefore names its assets `listen.css?v={v}` /
`listen.js?v={v}`, `web/public.py` substitutes a hash of their bytes into the page as it serves it,
and the assets are sent `immutable`. Only the page itself is `no-cache`, and it is `.html`, which
Cloudflare leaves as `DYNAMIC`. Bust by URL here, never by header.

`ArtworkStore.version` is a 63-bit int, so it goes to the listener page as a **string**: through JSON
a number past 2^53 is rounded by `JSON.parse` and the `art?v=` that comes back is not the version the
host sent. That endpoint compares `v` against the current version and serves `immutable` only when
they agree, `no-store` when they do not - the cover can change between the poll that named a version
and the fetch for it, and caching those bytes under the old key pins the wrong cover for a year (the
panel has the same shape, but it is push-driven over loopback, so its window is milliseconds). The
comparison is only sound because `ArtworkStore.current` reads version and bytes under one lock. The
panel still receives the version as a number: nothing validates it there.

## Invariants that are easy to break

**Pacing (the reason the stream never stalls)**
- A receiver delivers *nothing* while the sender is paused. `AudioPacer` therefore writes exactly one
  second of frames per second of wall clock, filling silence on underrun, and `VideoPacer` emits a
  fixed frame rate from the same `Clock`. Both demuxers derive timestamps from data received, so this
  is also what keeps A/V in sync. Never make either pacer emit on source activity.
- **The ffmpeg input writers need a raised high-water mark.** ffmpeg consumes each input in bursts
  while it interleaves and fills a segment, so a writer left at asyncio's default 64 KiB blocks in
  `drain()` for seconds at a time. The pacer reads the clock rather than its own progress, so a
  blocked write comes out as a long burst followed by filled silence - measured, four seconds of
  silence and three seconds shed per eight seconds of stream. `WRITE_BUFFER_LIMIT` is the buffer the
  named pipes had before the move to loopback sockets.
- A pacer only returns when it can no longer write, so its exit has to end the session - that is what
  `_run_pacer` wraps both in. Silence is the *designed* output for a paused source, which means a dead
  pacer looks identical to a quiet one from the outside - nothing else will notice. Several paths can
  report one fault (both pacers see the broken socket, then the encoder exits); `_stop_core` is a
  no-op once the session is gone, so only the first does anything.
- Teardown kills the encoder first so a blocked write faults, then waits for the pacers before
  aborting the writers and closing the servers under them. The wait is bounded.
- The receiver is *not* part of the session. It runs whenever selected and `push_audio` drops what it
  delivers while no session exists, so selecting a source and starting a stream stay independent.
- `app._prepare` starts the receiver **before** `deps.ensure_all`. The receiver needs none of the
  downloads, and behind ~135 MB of them a first launch offers Apple Music no speaker to pick for
  minutes - which reads as the app being broken. ffmpeg and cloudflared are wanted later, by the
  stream and tunnel buttons, and both report their own absence.

**AirPlay**
- Apple Music and iTunes for Windows speak **AirPlay 1 (RAOP) only** - confirmed by shairport-sync's
  maintainer. No HomeKit pairing, no FairPlay, no SRP. Do not advertise AirPlay 2 keys (`ft`) or an
  `_airplay._tcp` service: a sender that sees them tries AirPlay 2 first and then fails.
- The six MAC bytes in the mDNS service name and the six signed into `Apple-Response` must be the same
  bytes, and the A record must be the LAN IPv4 rather than loopback - the sender connects to that
  address and it is also what gets signed.
- `Apple-Challenge` is signed with PKCS#1 v1.5 and **no hash**. `cryptography` has no primitive for
  an unhashed signature, so `keys.sign_challenge` builds the padded block and does the modpow itself.
- The AES payload is decrypted with the IV reset **per packet**, not chained, and only
  `len & ~0xF` bytes are encrypted - the tail rides in the clear.
- A sender front-loads about two seconds on RECORD. Handing that straight to the pacer would trip its
  600 ms shed cap and lose the start of every play, so `RtpSession` plays packets out on their own RTP
  timestamps with a 250 ms lead and only then feeds the pacer.
- When a hole in the sequence has nothing behind it, the sender stopped rather than dropped a packet:
  park the cursor and resume wherever it speaks again. Filling silence there instead would emit
  forever. A hole with later packets waiting is a real loss and does get silence, so the timeline
  stays honest.
- ffmpeg's ALAC decoder needs the **36-byte `alac` atom**, not the bare 24-byte body the SDP fmtp
  describes. `alac.magic_cookie` rebuilds it, and its output is byte-identical to what ffmpeg writes
  for its own ALAC files - which is how it was verified.
- **A decoded plane is longer than the samples it holds.** ffmpeg allocates audio buffers with
  alignment padding, so `bytes(frame.planes[0])` returns 128 bytes more than
  `samples * 4` and every one of those bytes is zero. Sending the whole buffer appends silence to
  every packet: 0.8 % on the 4096-frame packets a test file produces, and **9 % on the 352-frame
  packets AirPlay actually sends**, which is both an audible buzz at the packet rate and a 9 %
  overrun the pacer then sheds continuously. Always cut a plane to `frame.samples`. The receiver's
  output is bit-exact against a reference decode, and that comparison is the test that catches this.
- The TXT record offers uncompressed audio as well (`cn=0,1`), so `PcmDecoder` has to exist; a sender
  that takes it would otherwise crash the session on an ALAC decoder it never announced.
- **The RTP sockets must only hear the sender that announced the session.** They bind `0.0.0.0` on
  fixed, well-known ports, so anything on the LAN reaches them, and a second device streaming into
  6100 gets its packets decrypted with *this* session's key - which is noise, decodes to nothing,
  and is exactly what "metadata fine, no sound, a wall of `avcodec_send_packet` errors" looks like.
  `_Datagram` filters on the RTSP peer address for that reason. If a sender ever puts its audio on
  a different address than its RTSP connection this drops everything, so the first stray address is
  logged rather than silently ignored - that line is the only thing between the user and unexplained
  silence.
- **One sender at a time means every stateful method, not just ANNOUNCE.** `OWNED_METHODS` covers
  SETUP, RECORD, FLUSH, TEARDOWN and SET_PARAMETER; a non-owner gets 455. TEARDOWN was the dangerous
  omission - a second device could end the first one's session outright. OPTIONS and GET_PARAMETER
  stay open on purpose: senders probe with both before they announce anything, and gating them
  breaks the handshake.
- **A decode failure is never one packet.** Whatever makes one undecodable makes all of them, and a
  sender fills about 125 a second - each one a `hub.warn`, which prints *and* fans a broadcast out
  to every panel socket. `_report_undecodable` counts them and reports at an interval instead.
- **`RtspServer.stop` has to abort its connections, not just close the server.** Since 3.12.1
  `Server.wait_closed()` waits for the live handlers as well as the listening socket, and a sender
  keeps its RTSP connection open for as long as it likes - so with Apple Music still attached,
  `close()` + `wait_closed()` never returns. Measured, that made every exit hang until
  `app._shutdown`'s 10 s timeout, log `could not stop the receiver -` with nothing after the dash
  (`TimeoutError` stringifies to `""`), and then spray "Task was destroyed but it is pending" and
  `RuntimeError: Event loop is closed` as `runtime.shutdown()` pulled the loop out from under the
  still-running `_serve`. `abort_clients()` between the two is what lets `_serve` unwind.
- A failed ANNOUNCE must release the session. Otherwise the connection stays the owner and the SETUP
  that follows runs against parameters that were rejected.
- **A listener's request cannot reach Apple playback at all, and this is settled.** The Windows app
  is not scriptable - the COM interface died with iTunes - DACP carries nothing past `playpause`,
  `nextitem` and `previtem`, and `music://music.apple.com/us/song/{id}` **only opens the app at the
  track**: measured against the real app, it does not start it, and Apple documents no parameter
  that would (`MPMusicPlayerController.openToPlay` is the sanctioned equivalent and is
  iOS/macOS-only). Firing DACP `play` afterwards is not a fix either - the opened page is not
  selected for playback, so it would resume whatever was playing before, which is a *wrong* track
  rather than no track. So `request_action` is `"ask"`: `enqueue` looks the id up in the iTunes
  catalogue and parks a `Requested` on the hub for the panel, and `open_request` is what the host's
  Open button calls. Search and lookup are the public iTunes APIs - no key, no account.
- **What the panel shows about a request is looked up again, never taken from the listener.** The
  POST carries only an id; `itunes.lookup` turns that into the title, artist, album and cover the
  host actually sees, so nothing a stranger typed is rendered in the panel. A repeat of an id
  already pending is dropped rather than stacked - the per-address cooldown is no defence against
  the same track arriving from a roomful of people.
- Metadata arrives as DMAP over SET_PARAMETER. The Apple apps have been seen packing `artist — album`
  into the artist field with the album empty; that split only fires when the album is genuinely empty,
  because the result is burned into the outgoing video, and it logs when it does so it can be deleted
  once the behaviour is confirmed either way.

**Spotify**
- go-librespot's Windows build **stubs out the pipe backend**, and its WASAPI backend plays to the
  system default device with no way to select another. `vendor/go-librespot/` holds the one-file patch
  that implements the pipe there and the steps to build it; until that binary exists the Spotify
  source reports itself unavailable and Apple Music is unaffected.
- **`go-librespot.exe` is downloaded from our own release asset**, because no upstream release
  carries the patch. A URL for it must therefore be one this repo publishes - point it anywhere else
  and it 404s on every launch, as a red error in the log of everyone using Apple Music, which is
  what it did while no such asset existed. `DependencyManager.go_librespot` stays a property over
  `resolve` rather than an attribute set during `ensure_all`: the source is selected *before* the
  downloads run, so the receiver has to be able to see the binary the moment it lands, and a
  hand-built one dropped into `BIN_DIR` needs no restart either. `app._prepare` selects the source a
  second time afterwards for the same reason - on a first launch with Spotify selected there is
  nothing to start until the download finishes.
- The downloaded archive carries the exe **and** its DLLs together, so a machine gets a working set
  or none at all. Do not split them into two downloads again: the pair that is half-installed is the
  one that fails in Windows' own "DLL was not found" dialog, which never names go-librespot.
- The named pipe instance must exist **before** the daemon starts, because go-librespot is the client
  and its open fails outright when nothing is listening.
- go-librespot closes the pipe on stop and on playback moving to another device, and reopens it on the
  next play. The reader is therefore a loop that survives any number of connect cycles; the pacer
  fills the gaps and never learns anything happened. Never close the pipe while streaming should
  continue - a write error makes the daemon emit `stopped` and stay stopped until the user presses
  play.
- **The pipe reader has to pace its own drain to real time.** go-librespot's pipe output has no pacing
  of its own - `Write()` blocking on a full buffer is its only throttle, the role a real device's
  small hardware buffer plays on Unix. Draining as fast as bytes arrive removes that backpressure:
  go-librespot decodes and writes an entire track in a few CPU-bound seconds, then considers it
  finished and skips to the next one. `PipeReader._read` holds its drain at `SAMPLE_RATE` with
  `LEAD_SECONDS` of allowed slack, and caps catch-up (`MAX_CATCH_UP_SECONDS`) so a moment this thread
  doesn't get scheduled - go-librespot keeps writing regardless - doesn't flush as a burst once it
  resumes; `AudioPacer` already fills a gap like that with silence, the same as a paused source.
- **`BUFFER_SIZE`, the pipe's kernel buffer, has to stay small.** Sized for several seconds, it let
  go-librespot dump that much pre-buffered audio in almost instantly on connect - and since its
  position tracking advances with every `Write()` regardless of whether this reader has actually
  forwarded the audio yet, that showed up as a fixed multi-second gap between what the panel reports
  playing and what the stream is actually playing, present from the first sample of every track.
  Sized close to what a real hardware buffer would hold instead, `Write()` blocks almost immediately,
  so go-librespot can never get more than a fraction of a second ahead of what has actually reached
  `AudioPacer`.
- **Song requests need nothing registered and nobody logged in.** `POST /token` hands back an access
  token for the session the desktop app already gave the daemon over zeroconf, which is enough for
  `api.spotify.com/v1/search`, and `POST /player/add_to_queue` is literally "play next" - Spotify
  runs its queue ahead of the rest of the context. The token is cached in `LibrespotApi` and dropped
  only on a 401, because the daemon's handler calls `GetAccessToken(ctx, force=true)` and an
  uncached call is a real login5 round trip for every search a listener types.
- **This token's search quota is tight enough to hit in ordinary use, not just abuse** - reproduced
  live from a handful of manual test searches within a couple of minutes, all in a row failing
  `429`. It is a borrowed session token, not one issued to a registered app with its own quota, so
  it is not the public Web API's usual generous per-app limit. `search()` reads Spotify's own
  `Retry-After` off the `429` and will not attempt another search until that many seconds have
  passed - checked before touching the network at all, not just before touching `/v1/search`, so a
  still-throttled window costs nothing per listener who tries anyway. A missing or unparseable
  header - `429`s do not have to carry one - falls back to `DEFAULT_RETRY_AFTER` rather than either
  retrying immediately or refusing to.
- `external_volume: true` keeps the broadcast at full scale; Spotify's slider is the listener's
  business, not ours. The AirPlay side ignores its `volume:` messages for the same reason.
- Password login is gone from Spotify. Credentials arrive by the desktop app handing off over
  zeroconf, and are persisted so later runs need no re-pick.
- **Its Windows build also drags in six MSYS2 DLLs** - `libmpg123-0`, `libFLAC`, `libvorbisenc-2`,
  `libvorbis-0`, `libogg-0`, `libwinpthread-1` - because CGO links them dynamically. A machine
  without MSYS2 got Windows' bare "DLL was not found" dialog, never mentioning go-librespot. Ship
  the whole closure, not just what `go-librespot.exe` itself imports: forcing `libogg` static (see
  the build step's own comment for why) only settles go-librespot's own link, and the prebuilt
  `libFLAC` and `libvorbis-0` still import the shared `libogg-0`, `libFLAC` also `libwinpthread-1`.
  Check by dumping the import table of each built DLL rather than trusting the exe's. All six go into
  the published archive beside the exe, and `deps.py` extracts the lot.
  They land in `BIN_DIR` rather than next to the exe, because the
  exe can be running out of a onefile temp extraction that is gone by the next launch while
  `BIN_DIR` survives - so `LibrespotProcess` puts `BIN_DIR` on the child's `PATH` instead of relying
  on the exe's own directory.

**yt-dlp**
- **yt-dlp is a downloaded exe like the other three, not a pip dependency**, and tracks `latest`
  rather than a pinned ref. It was a pip dependency, which froze its version into a release build -
  the worst arrangement for the one library here that chases a moving target, since YouTube breaks
  extractors regularly and upstream ships releases at that cadence. As a download, a fix reaches
  everyone already holding an exe without a StreaMuse build, which is the same reason go-librespot
  is not bundled. Only `sources/ytdlp/extractor.py` runs it, and only to extract; ffmpeg does the
  actual decode, the same as everywhere else in this app.
- **It costs a process per resolve, and the exe is a repackaged Python interpreter** (~18 MB) that
  unpacks to temp on every run, so a resolve carries ~2 s of startup the in-process API did not.
  Measured against a real search that already takes seconds of network, it is not dominant - time to
  first audio went 4.4 s to 3.7 s across the change - but it is why nothing here should call
  `extract()` speculatively.
- `extract()` reads `yt-dlp.exe -J`, whose JSON is the same dict the Python API returned: a search
  comes back as a `playlist` with `entries`, a direct URL as a `video` with the chosen format's
  `url` and `http_headers` already merged in at the top level. Verified against both shapes. It
  passes `--` before the query so a listener's search starting with a dash is a search term and not
  a flag, and takes the *last* stderr line for its error, since yt-dlp prints warnings ahead of the
  `ERROR:` line that says why.
- `YtDlpReceiver` is not a receiver in the AirPlay/Spotify sense - nothing ever connects to it, so
  `start()` only records the sink and `load()` (called from `/api/source/load`) is what actually
  begins a track. `SourceManager` and `Receiver` both grew a `load()` alongside `control()` for this;
  every other source's default just returns `False`.
- **A resolved stream URL is signed to the request that fetched it.** The JSON's `http_headers`
  (User-Agent above all) have to travel with the URL to ffmpeg's `-headers`, or YouTube's and
  SoundCloud's CDNs answer 403 - verified end to end against both.
- **`_fetch()`'s cover download catches `TimeoutError` alongside `aiohttp.ClientError`** - the
  session's own timeout raises the former, which is not a subclass of the latter, so a slow or
  unreachable thumbnail host would otherwise escape the `except` entirely. By the point `load()`
  calls this, the track is already marked playing and its decoder has not started yet, so an
  uncaught exception here would crash `load()` with the panel showing "Playing" and no audio ever
  actually starting. Reproduced live under a flaky connection.
- yt-dlp writes its stderr in the console codepage, not UTF-8, so an `ERROR:` line carrying a
  typographic quote decodes to U+FFFD under `errors="replace"` ("Sign in to confirm you<?>re not a
  bot"). Left alone deliberately: guessing the codepage would mangle the real UTF-8 that track
  titles in those messages are written in, and `StateHub.log`'s print is already guarded.
- **A refusal a listener may read is `sources.Rejected`, not any convenient exception.** The three
  deliberate ones (`no results`, live, too long) are raised in `extractor.py`, re-raised untouched by
  `search()` and turned into a 400 by `web/public.py`, so the type is the contract between them. It
  lives in `sources/__init__.py` rather than the yt-dlp package because the web layer consumes it.
  It used to be `LookupError`, which also catches `KeyError` - so `info["url"]` missing reached a
  listener as `{"error": "'url'"}` with nothing at all in the host's log.
- **`cache.download` puts ffmpeg's last stderr line in the exception.** A stale signed URL comes back
  as a CDN 403 and looks nothing like a codec failure, but the exit code cannot tell them apart -
  measured, a bad URL now reports `Server returned 404 Not Found` where it used to report only
  `exited 3419392776`. It drains stdout and stderr concurrently into `bytearray`s rather than calling
  `communicate()`, which joins a chunk list at the end and so holds a whole track's PCM twice.
- `cookiesFile` is one Netscape-format `cookies.txt` for both sites, handed to yt-dlp's `--cookies`,
  which filters by domain on its own - there is no per-source cookie setting to keep in sync.
  **yt-dlp gets a copy, never the file itself.** `--cookies` is also where it dumps its cookie jar
  as it exits, failed runs included - measured, a 73-byte file came back as 971 bytes "generated by
  yt-dlp" after one resolve - so the host's export was rewritten by every run, by a prefetch and a
  listener's search at once, and by a process that may be ended mid-write. `extract()` copies it
  into a temporary folder per run and the folder goes with the run. The cost is that nothing
  yt-dlp is sent back persists to the next run, the same as `--cookies-from-browser`.
- **`load()` queues rather than replaces**, so a song request lines up behind whatever the panel
  already started instead of needing a queue of its own. It always appends to `self._queue` and
  spawns the advance - so "play" and "add to queue" are the same call, and which one it looks like
  depends only on whether something was already playing. `control("next")` and a natural end (the
  decoder's `on_finished`) both advance the same way: stop (or notice it already stopped), pop the
  next query, resolve and play it. `self._gate` (an `asyncio.Lock`) serializes every path that can
  start or stop a decoder - `control()`, `stop()`, and the detached advance - because a track ending
  on its own at the same moment as a manual "next" could otherwise each see the decoder as free and
  start one of their own. `_advance_locked()` assumes the caller already holds the gate;
  `_advance()` is the gate-acquiring wrapper, only ever run detached.
- **`load()` returns once the query is queued, not once it plays.** It used to take the gate and
  await the whole resolve-and-decode under it when nothing was playing, so the panel's POST and a
  listener's `request` hung for the length of a download, and a second load behind them. Now it
  only appends and calls `_spawn_advance`, measured at 0 ms against a 1 s resolve.
  **There is never more than one detached advance.** An advance still in flight reads the queue
  again after its last await, so it picks up anything appended meanwhile. A second one waiting on
  the gate would be handed it *ahead* of a `stop()` or "next" that had already cancelled the load in
  front - and start the following download under the gate they are waiting for, which for "next"
  ends with that track stopped too and two skipped for one press.
- **The gate alone is not enough, because `_on_finished` clears `self._decoder` off the gate.** It
  runs straight from the pacing thread's `call_soon_threadsafe` and only *schedules* the advance, so
  a "next" can take the gate in between, see no decoder and start the next track itself; the
  scheduled advance then starts a *second* decoder over that live one - both pacing into the sink,
  with the first no longer referenced by anything that could stop it. `_advance_locked` therefore
  starts nothing when a decoder already exists - which is also what a `load()` during playback
  relies on, wanting only the prefetch. Verified by removing that check: the same sequence really
  does leave two decoders running.
- **`stop()` and `control("next")` cancel the in-flight resolve *before* asking for the gate.**
  `_play` awaits `_resolve_and_cache` with the gate held, and `cache.download` gives ffmpeg
  `-reconnect` with no overall deadline, so against a stalled CDN the gate would be held for as long
  as ffmpeg kept retrying - hanging `SourceManager.select` and burning `app._shutdown`'s 10 s
  timeout, the same failure shape as the `RtspServer.stop` one above. `_cancel_loading` is what
  makes the wait bounded; `_play` treats its own task being cancelled as "nothing to play", since
  the canceller is already queued on the gate behind it.
- **`search()`/`enqueue()` (the public song-request path) reject anything URL-shaped that is not on
  an explicit `youtube.com`/`youtu.be`/`soundcloud.com` allowlist** - unlike Apple's and Spotify's
  `search()`, which always hit a fixed catalogue endpoint regardless of what a listener types,
  yt-dlp's extractors resolve whatever URL they are given, some through a generic fallback that
  fetches the page directly. Left unrestricted, an anonymous listener behind the tunnel would have
  an SSRF primitive against the host's own network. Verified against a real yt-dlp install that this
  is not just an http(s) problem: `ftp://169.254.169.254/` is actually attempted by the generic
  extractor (it only failed here for want of a reachable FTP server) - so the check is "does this
  string have a scheme prefix at all", not "is it http(s)", and anything without one is treated as
  bare text, which only ever becomes a YouTube search and is always safe. `enqueue()` re-checks the
  same allowlist independently of `search()`, since the id a listener POSTs back is never assumed to
  be one `search()` actually returned.
  **Two things make "URL-shaped" wider than it looks, and both were live bypasses.** The scheme is
  optional: `GenericIE._real_extract` promotes a bare `//host/path` to http(s) *before* it reaches
  `default_search`, so that is fetched, not searched for - hence the `//` rather than the scheme is
  what `_URL` actually keys on. And the string must be stripped *before* the check as well as after,
  because `urlsplit` drops leading whitespace itself, so `" https://..."` reads as bare text here
  and as a URL to yt-dlp. Both were confirmed by watching a real yt-dlp open the socket.
- **Every track is fully resolved and decoded to raw PCM in memory (`cache.py`) before it plays**,
  network fetch, resample and decode all in one ffmpeg pass - not decoded live off the network, and
  not even decoded lazily at playback time. A CDN reset during that one pass just costs download
  time - ffmpeg's own `-reconnect` keeps retrying against a target with no realtime deadline to miss -
  where the same reset landing mid-playback against a paced decoder used to shed audio outright (the
  "audio buffer overran" case this section used to chase). The queue's head is prefetched while the
  current track is still playing (`_ensure_next_locked`), so a normal advance is `Decoder` pacing
  bytes already sitting in RAM rather than a fresh resolve-and-decode; only ever one item is
  prefetched, since nothing here plays more than one track ahead anyway. A track's cached bytes are a
  plain `_Cached` reference, dropped the moment it stops being current - natural end, skip, or
  `stop()` - the same as any other object; there is no file to clean up. Discarding a still-in-flight
  prefetch has to await it through `asyncio.gather(task, return_exceptions=True)` rather than a bare
  `try/except Exception` - `CancelledError` has not been an `Exception` subclass since 3.8, so
  awaiting a cancelled task directly would let it escape and read as the caller itself having been
  cancelled. Doing the full decode here rather than deferring it to playback means `Decoder` never
  runs a subprocess at all - see below. Two earlier passes preceded this one: a disk-cache version
  (real-world evidence on the machine this was built on - an HLS playlist rename failing with
  "Operation not permitted", ffmpeg processes exiting with Windows' uninitialized-memory-pattern exit
  codes like `0xCCCCCCC8`, a plain `kill()` taking the full 3s timeout - pointed at antivirus
  real-time scanning interfering with the cache files, motivating a move to memory instead of asking
  every install to carve out an exclusion), then an in-memory-but-still-live-decode version (`Decoder`
  fed a Matroska remux over its own stdin, decoding on demand at playback time). Neither was wrong,
  exactly, but both left a live ffmpeg process running during the timing-critical part; decoding
  everything upfront during prefetch removes that variable entirely.
- **`Decoder` paces already-decoded PCM out to the sink on its own OS thread, not the shared asyncio
  loop** - the same pattern `spotify/pipe.py`'s `PipeReader` already uses for the identical reason:
  real-time pacing that must never be at the mercy of whatever else the loop is doing. With no
  subprocess involved anymore, its whole job is `time.sleep()`-paced chunking of a `bytes` object;
  `on_pcm`/`on_finished` cross back onto the loop through `call_soon_threadsafe`, exactly like
  `PipeReader` does. This and moving the full decode into `cache.py` were both built chasing a
  shedding bug that turned out not to be either of their fault: a direct measurement (polling
  `/api/state` every 150ms while shedding was actively occurring) showed the loop responding in
  0-2ms throughout, ruling out a loop stall, and an isolated test of the read/pace math alone showed
  zero drift over a full track. Both changes are kept anyway as independently-justified
  simplifications (matching `PipeReader`'s own established pattern, and removing a live process from
  the timing-critical path), not as the actual fix.
- **The shedding was the chunk size.** `Decoder` pushed 16384-frame chunks (372 ms) with a 200 ms
  lead, onto the 200 ms `AudioPacer` holds in reserve - a peak near 572 ms against the 600 ms shed
  cap, so ordinary timer jitter tipped it over. Spotify's pipe reads are far smaller, which is why
  only this source shed. `CHUNK_BYTES` is 2048 frames (46 ms) now; keep it small against that cap.
  Measured with the real `AudioPacer`, `Clock` and `Decoder` over a 20 s track: 372 ms chunks peak at
  600 ms and shed up to 0.9 s, 46 ms chunks peak at 280 ms and shed nothing.
  `AudioPacer.push()`'s `max_latency_ms` override and `receiver.PACER_MAX_LATENCY_MS` (30 s) are the
  earlier fix, from before the cause was known - a cap that wide absorbs the overshoot instead of
  avoiding it, and in the same simulation the buffer holds at about 600 ms under it rather than
  climbing. Both are probably redundant now and are kept only until a live run says so.
- **That wide allowance has to be given back on a skip.** Banked lead is harmless while a track
  plays, but the moment "next" is pressed it is the *skipped* track's audio, and `AudioPacer` has no
  partial flush - `reset()` belongs to the pipeline, and the receiver only ever holds a push
  callable, so reaching in to clear it would mean changing the sink contract for all three sources.
  Instead `_stop_decoder` arms `PACER_SKIP_LATENCY_MS`, and the next track's first chunk is pushed
  with that tight cap so `push()` sheds the stale lead as the overrun it now is; `_deliver` reverts
  to the wide cap immediately after. A natural end deliberately does not arm it - there the lead is
  the tail of the track that just played, and dropping it would clip the ending.

**Serialization and background tasks**
- Never put a non-finite `float` into anything serialized. `state.dumps` passes `allow_nan=False`, so
  a mistake raises here instead of emitting JSON a browser silently rejects (see `LevelMeter.read`,
  which returns `None` rather than `-inf`).
- The hub is mutated from receiver threads as well as the loop, so broadcasts are scheduled with
  `call_soon_threadsafe` and each client has its own queue - a mutation never blocks on a slow socket.
- Detached tasks must log their exceptions. Anything swallowed here is invisible and presents as a
  frozen UI.
- **An `except aiohttp.ClientError` around a request needs `TimeoutError` beside it.** The session's
  own timeout raises the builtin, which is not a `ClientError`, so a slow server escapes the handler
  that a refused connection lands in - measured against a server that accepts and never answers, in
  the iTunes lookup (a 500 for the listener), DACP, and every go-librespot call, where a slow cover
  fetch took the event socket down with it. It also stringifies to `""`, hence `timed out`.
- Logging must never throw. The Windows console cannot encode every track title, so the print is
  guarded; prefer plain quotes over typographic ones in log and status strings.
- Nothing in `app._shutdown` may throw. It runs after the window has gone and there is no handler
  above it. Each step is attempted independently, because skipping the rest would leave ffmpeg,
  cloudflared or go-librespot running and the stream publicly live.
- Every child process is adopted into the job object in `jobs.py`, whose handle must stay open for the
  life of the app - closing it is what kills them. That is the only cover for an End task or a crash,
  where no teardown of ours runs; measured, a killed app otherwise leaves cloudflared serving the
  tunnel indefinitely.

**Encoder**
- `-analyzeduration 0` with a small `-probesize` is load-bearing. ffmpeg's default 5 s probe does not
  drain the audio input; the paced writer fills the buffer, blocks, and sheds seconds of audio. The
  video input's probesize must still admit one whole JPEG.
- The HLS muxer reports `bitrate=N/A`, so the delivered bitrate is measured from segment file sizes in
  `hls.measure_bitrate_kbps`.
- Output targets AVPro/VRChat: muxed mpegts, 1 s segments, one keyframe per second, `main` profile,
  limited colour range. Separated audio/video tracks are a known VRChat failure mode. JPEG input is
  full-range and will otherwise leak out as `yuvj420p`. libx264 warns that 1280x720 exceeds the level
  3.1 limits it is given; that is the tuned combination and the warning is expected.
- The cover renderer caches the composed ground (blurred backdrop plus art) per artwork version and
  redraws only the text over it. The blur is most of the frame cost - 77 ms against 6 ms for a text
  redraw - and only the progress and track fields change between frames.
- Pillow draws glyphs in raw codepoint order with no bidi algorithm or Arabic joining, so RTL text
  (Hebrew, Arabic) came out backwards - "יום אחד" as "דחא םוי". `frames._rtl` reshapes with
  arabic-reshaper and reorders with python-bidi right before drawing; `_ellipsize` still runs first,
  on the reshaped *logical* string, since trimming already-reordered text cuts from the wrong end.
- Pillow does no font substitution of its own - a codepoint missing from a font draws as that font's
  `.notdef` tofu box, silently. Segoe UI covers only a fraction of Unicode, so track metadata in
  Japanese, Ethiopic, Canadian Aboriginal Syllabics, Indic scripts, Thai/Lao, Tibetan, Yi or Phags-pa
  drew as boxes. `frames._FontStack` checks real glyph coverage per character via `fontTools`' cmap
  and splits the string into runs, each drawn by the first font in `_TITLE_FONTS`/`_BODY_FONTS` that
  has the character - so Latin mixed into a title keeps Segoe UI's weight rather than a fallback's.
  **Measure through the same stack.** `_ellipsize` asks it for the width, because a character that
  needs a fallback measures as Segoe UI's `.notdef` advance in the primary font - 420 px against
  Yu Gothic's 640 px for twenty kana at 32 px - and a title trimmed on that number runs off the frame.
- The fallback list is exactly the specialty fonts Windows itself ships and falls back to for the
  same reason, so adding an entry when a new script turns up broken costs nothing. They are optional
  Windows components, though, and absent on N/LTSC images or where a language feature was removed:
  `_FontStack` skips a name whose file is missing rather than letting `ImageFont.truetype` raise,
  which would take the whole video path down. Watch the file names - Nirmala UI ships only as the
  collection `Nirmala.ttc`, whose face 1 is the bold, and there is no `NirmalaB.ttf`; hence the
  `(name, index)` pairs. Only Yu Gothic and Nirmala UI have a bold face at all; the rest are used at
  regular weight even in the title.

## Control panel (`wwwroot/`)

`styles.css` is the Industry design system, vendored **verbatim** from a Claude Design export - do
not edit it. Put every override in `app.css`. Its "blueprint" pass at the end of the file overrides
earlier rules, which causes two traps worth knowing:

- `.card, .dialog { background: transparent }` makes a modal see-through; the settings dialog paints
  its own ground.
- `.field > label` (the field caption rule) outranks `.radio`, so a checkbox nested in a `.field`
  loses its flex layout and its dot collapses to its borders.

`app.js` binds declaratively: every `[data-bind="x"]` element receives `view.x`. A binding value may
carry a `style` that is either a declaration *string* or a property object - `apply()` handles both,
because passing a string to `Object.assign` throws and silently aborts the remaining bindings.

**Theming.** The system ships one light palette, so dark mode restates its tokens in `app.css`. The
accent ramps are reversed there - `--color-accent-100` is the darkest step - because every pairing in
the system reads one end as a ground and the other as ink (`.tag-accent`, the status tags in
`buildView`), and only reversing keeps those legible. The neutral ramp is left alone: its only uses
are the modal scrim and the inactive dots, which want the same value either way. A theme of `Auto`
sets no `data-theme` at all, so the `prefers-color-scheme` block paints it - which is also what makes
the first frame right before `app.js` has read the setting, and why that block is the token list a
second time.

The window is an ordinary framed window, so the page owns none of the chrome. `ui.ground` paints what
shows before the first paint, resolving `Auto` against the registry's `AppsUseLightTheme`. The page no
longer posts anything to the host, and the body scrolls rather than growing the window when the
details drawer opens.

## Not yet verified

The **Spotify** path now has run end to end, against a hand-built go-librespot already sitting in
`BIN_DIR` (`resolve()` finds a local copy before ever trying a download) - real Connect pairing,
real audio, `search()` hitting the real `api.spotify.com` and getting back genuine responses,
`429` included. The auto-download itself is still unconfirmed: the workflow that publishes
go-librespot to this repo's own release has not been run, so whether `deps.GO_LIBRESPOT_URL` names
an asset that actually exists remains untested - a machine without a local copy already in place is
the one case this has not covered. `add_to_queue` is the one piece of song requests still
unconfirmed - every real search so far has come back `429` before there was a result to enqueue.
The **Apple** half is verified against the real app: search and lookup resolve real tracks, and the
`music:` handoff was measured doing exactly what the code now assumes.

`/token` and `/player/add_to_queue` are on go-librespot v0.9.0, which is what `GO_LIBRESPOT_REF` and
the workflow's `LIBRESPOT_REF` pin,
and both survive into master - so the bump `vendor/go-librespot/README.md` anticipates keeps them.
Its `/web-api/` proxy does **not**: it exists only in v0.9.0 and was gone by v0.9.1, which is why
search goes through `/token` and calls Spotify itself rather than proxying.

The **AirPlay** path is verified end to end against a synthetic RAOP sender that performs the real
handshake and streams real AES-encrypted ALAC: challenge signing, key unwrap, SETUP, RECORD, DMAP
metadata, JPEG artwork, progress, and decoded PCM all check out. It has **not** yet been driven by
Apple Music itself; the service is confirmed discoverable over mDNS on the development machine, but
whether Apple Music for Windows lists it, and whether it advertises `_dacp._tcp` so the transport
buttons work, are unconfirmed. The panel keeps working without DACP - only the transport buttons go
quiet.

The pipeline, both web surfaces, the tunnel, the frozen exe and the panel have been verified by
running them.

The **yt-dlp** path is verified end to end against real YouTube and SoundCloud, including a search
query, resolving a real signed URL, ffmpeg decoding it with the required headers, real-time pacing,
pause/resume back-pressuring the decode, and both a bad URL and a bad cookies file failing cleanly
without taking the receiver down. The read-ahead queue is verified too: mid-track it measurably holds
several seconds banked ahead of real-time consumption, and pause/resume/next/natural-finish all still
behave the same as before it was added; so is the "unclosed transport" fix, confirmed gone across a
real multi-track-transition run. Not yet exercised: a cookies file that actually unlocks
age-restricted or private content, since verifying that needs a real account's exported cookies; and
a real multi-second CDN stall specifically, since nothing here can inject one to order - the read-ahead
fix is verified by what it measurably does (bank read-ahead) rather than by forcing the stall it is
meant to absorb.

**Its move from a pip dependency to a downloaded exe** is verified by the real thing, not just by
imports: the pip package was uninstalled, `ensure_all` downloaded `yt-dlp.exe` from upstream's
latest-release URL into `BIN_DIR` and reported it alongside the other three, and a full live run
against real SoundCloud came back identical to the pip-API run before it - same tracks, same byte
counts, prefetch, skip and clean stop. `-J` was checked against both JSON shapes (search and direct
URL) for the fields `extract()` reads, and the three failure paths were driven for real: a 404 URL
surfaces yt-dlp's own `ERROR:` line, an empty search raises `Rejected`, and a dash-leading query is
searched for rather than parsed as a flag. Unlike `deps.GO_LIBRESPOT_URL`, this download URL is
confirmed to exist - it is upstream's own, not one this repo has to publish. Not yet exercised: a
release build with the dependency gone, though nothing in `streamuse.spec` named it.

**yt-dlp song requests** are verified end to end against real YouTube: a bare-text request resolves
and plays immediately when idle, a second request while the first is playing queues behind it
without interrupting, and the request-side URL allowlist is verified both ways - allowed hosts
resolve normally, and a disallowed scheme (`ftp://169.254.169.254/`, chosen because it is a real
attempt by yt-dlp's own generic extractor, not merely a syntax rejection) is refused by both
`search()` and `enqueue()` independently. The two wider bypasses of that allowlist - a bare
`//host/path` and a leading-whitespace URL - are verified the same way, and additionally by driving a
real yt-dlp with each: before the fix it opened the socket for both, after it neither resolves nor
reaches the network from `search()` or `enqueue()`. Not yet exercised: the same request flow through
the actual public HTTP endpoint end to end (verified so far at the receiver level, not through
`web/public.py`'s cooldowns and JSON handling), and SoundCloud specifically for this path (the
existing yt-dlp verification already covers SoundCloud for direct panel playback).

**The queue's concurrency fixes** are verified against real SoundCloud with real ffmpeg, driving
`YtDlpReceiver` directly: a track resolves and paces at 1x (6.3s of audio delivered in 6s of wall
clock), a second queues and prefetches behind it, `control("next")` picks it up with the skip cap
armed and reverting after one chunk, a failed resolve advances to the next item instead of stalling
the queue, and `stop()` leaves `_decoder`/`_next`/`_loading`/`_queue` all empty. The two-decoder race
is verified by removing the guard and watching the same sequence really start a third decoder over a
live one; `stop()` against a stalled resolve returns immediately rather than waiting on the gate.
Both were driven with the network stubbed, since neither race needs a real CDN to reproduce.

**yt-dlp's cache-before-play path** is verified end to end against real YouTube, driving `YtDlpReceiver`
directly rather than through the panel: the first track of a session resolves and fully decodes with
no prewarm; a second queued track is prefetched (resolved and decoded to PCM) while the first is
still playing, its exact byte count checked against `duration × sample_rate × frame_bytes` from the
source's own precise duration; `control("next")` picks up that prefetch and switches without
repeating the resolve, with real PCM measured flowing to the sink afterward; `stop()` leaves nothing
behind (`_decoder`/`_next` both `None` - there is no file or subprocess to check for anymore either
way). Two earlier versions of this path were verified the same way before being replaced: a
disk-cache version (file present while prefetching, discarded on transition, cache directory empty
after `stop()`), then an in-memory version that still decoded live at playback time over `Decoder`'s
own stdin (stdin-write/stdout-read concurrency checked against a real multi-MB track with no
deadlock) - both correctly implemented, but real-world antivirus-shaped symptoms and an unexplained
shedding bug (see below) motivated moving to the current fully-upfront design instead of continuing
to debug either. Not yet exercised: this path through the real app (panel and public request
endpoint) rather than the receiver driven directly, and the antivirus diagnosis from the disk-cache
era was never confirmed against Defender's own logs, only inferred from ffmpeg's own abnormal exit
codes and slow process kills - now moot for this path either way, since nothing here touches disk.

**The "audio buffer overran" shedding has its root cause**: 372 ms chunks left 28 ms of headroom
under the 600 ms cap - see the chunk-size bullet in the yt-dlp section. Reproduced and fixed in a
simulation driving the real `AudioPacer`, `Clock` and `Decoder`, not yet re-checked in a live
stream; the 30 s cap and the skip cap that predate the finding are still in place for that reason.
What was ruled out on the way stays ruled out: a loop stall, CPU or memory contention, the shared
pipeline (Spotify plays cleanly through the same pacer), resample drift, and the read/pace math. The
"~25ms/s climb" this file used to record did not reproduce - the buffer holds steady.
